"""
Raptor v5.4 — Exit Monitor
Manages ALL Alpaca positions. Source of truth is the broker, not the ledger.

Usage:
  python exit_monitor.py           # Execute exits
  python exit_monitor.py --dry-run # Show what would exit
"""

import logging
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from config import CONFIG
from data_feeds import DataManager
from signals import QuantSignalEngine, Factors, FACTOR_NAMES

os.makedirs(CONFIG.log.log_dir, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, CONFIG.log.log_level),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(CONFIG.log.log_dir, f"exits_{datetime.now():%Y%m%d}.log")),
    ],
)
logger = logging.getLogger("raptor.exits")


def _atr(bars, period=14):
    h, l, c = bars["high"], bars["low"], bars["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def _trail_mult(days_held, profit_atr, rcfg, composite=0.0, health=0.0):
    # Time-based base multiplier
    if days_held <= rcfg.trail_early_days:
        t = rcfg.trail_early_atr
    elif days_held <= rcfg.trail_mid_days:
        t = rcfg.trail_mid_atr
    elif days_held <= rcfg.trail_late_days:
        t = rcfg.trail_late_atr
    else:
        t = rcfg.trail_final_atr

    # Profit-based multiplier
    if profit_atr >= 4.0:
        p = 1.0
    elif profit_atr >= 2.0:
        p = 1.5
    elif profit_atr >= 1.0:
        p = 2.0
    else:
        p = 99.0

    base = min(t, p)

    # Signal-quality modifier — math drives trail width.
    # Strong signal + healthy position = wider trail (let winners run).
    # Weak signal + decaying health = tighter trail (protect profits faster).
    # TODO:DERIVE — thresholds ±0.3 and modifiers 1.3/0.75 are round numbers.
    # Derivation: backtest trail width sensitivity vs realized exit quality
    # (Bertsimas & Lo 1998). Calibrate once 60+ full-exit outcomes are tagged.
    signal_strength = (composite + health) / 2.0
    if signal_strength > 0.3:
        modifier = 1.3   # Strong — give room
    elif signal_strength < -0.3:
        modifier = 0.75  # Weak — tighten
    else:
        modifier = 1.0   # Neutral — no change

    return base * modifier


def run_exit_monitor(dry_run=False):
    logger.info("=" * 60)
    logger.info("RAPTOR v5.4 EXIT MONITOR - %s", datetime.now().isoformat())
    logger.info("=" * 60)

    dm = DataManager(CONFIG)
    engine = QuantSignalEngine(CONFIG)

    positions = dm.alpaca.get_positions()
    account = dm.alpaca.get_account()
    equity = account["equity"]

    if not positions:
        logger.info("No open positions. Nothing to monitor.")
        return

    logger.info("Account: equity=$%.2f  cash=$%.2f  positions=%d",
                equity, account["cash"], len(positions))

    # Collect all held symbols
    held = [p["symbol"] for p in positions]

    # Load ledger entry dates for accurate days_held (trail multiplier depends on it)
    try:
        from ledger import Ledger
        _ledger = Ledger()
        _ledger_map = {v["symbol"]: v for v in _ledger.data["positions"].values()}
    except Exception:
        _ledger_map = {}
    _today = datetime.now().date()

    # Build universe: current screen + all held symbols
    try:
        from universe_builder import UniverseBuilder
        ub = UniverseBuilder(CONFIG)
        universe = ub.build(max_symbols=150)
    except Exception:
        universe = held[:]

    for sym in held:
        if sym not in universe:
            universe.append(sym)
    if "SPY" not in universe:
        universe.append("SPY")

    # Fetch data
    dataset = dm.get_full_dataset(universe, lookback_days=CONFIG.signals.lookback_days)
    bars = dataset["bars"]
    macro = dataset["macro"]
    spy_bars = bars.get("SPY")

    # Run signal engine for thesis check (current composite scores)
    signals = engine.generate_signals(bars, macro, dataset["sentiment"], spy_bars)
    # Use _last_full_signals so held symbols that decayed out of the top-N
    # get their real composite score instead of missing entirely.
    # generate_signals() sets this attribute on the engine instance after scoring all symbols.
    full_map = getattr(engine, "_last_full_signals", {s.symbol: s for s in signals})

    # Build scores for all held symbols from full_map (authoritative).
    # Default = 0.0 (neutral/unknown), NOT -1.0.
    # A data gap is not a thesis judgment. -1.0 would poison trail modifier
    # and time-decay thesis checks on positions where we simply have no data.
    scores = {}
    for sym in held:
        if sym in full_map:
            scores[sym] = full_map[sym].composite_score
        else:
            scores[sym] = 0.0  # No score available — treat as neutral, not weak
            logger.warning("No composite score for held %s — defaulting 0.0 (data gap, not thesis judgment)", sym)

    # Portfolio drawdown
    total_pnl = sum(p.get("unrealized_pnl", 0) for p in positions)
    portfolio_dd = total_pnl / equity if equity > 0 else 0
    logger.info("Portfolio unrealized: $%.2f (%.2f%%)", total_pnl, portfolio_dd * 100)

    exits = []
    holds = []

    # Pre-load hold_health.json for use in time_decay thesis check
    _pre_health = {}
    try:
        import json as _phjson
        from pathlib import Path as _phpath
        _ph = _phpath("hold_health.json")
        if _ph.exists():
            _pre_health = _phjson.loads(_ph.read_text())
    except Exception:
        pass

    for pos in positions:
        sym = pos["symbol"]
        entry = pos["avg_entry"]
        price = pos["current_price"]
        qty = pos["qty"]
        pnl_pct = pos.get("unrealized_pnl_pct", 0)

        if sym not in bars:
            logger.warning("No bar data for %s - skipping", sym)
            holds.append({"symbol": sym, "reason": "no_data", "pnl_pct": pnl_pct})
            continue

        bar_data = bars[sym]
        atr = _atr(bar_data, CONFIG.risk.atr_period)
        if atr <= 0:
            # Read ATR from hold_health.json (stop_dist_atr is ATR in dollars).
            # Never fabricate price * 0.02 — that invents a value that looks real.
            _hh_atr = _pre_health.get(sym, {}).get("stop_dist_atr")
            if _hh_atr and _hh_atr > 0:
                atr = float(_hh_atr)
                logger.warning("ATR from bars <= 0 for %s — using hold_health stop_dist_atr=%.4f", sym, atr)
            else:
                logger.warning("ATR unavailable for %s (bars=0, no hold_health entry) — skipping position", sym)
                holds.append({"symbol": sym, "reason": "no_atr", "pnl_pct": pnl_pct})
                continue

        # Days held from ledger entry_date. Conservative fallback = 1 (not 7).
        # Using 7 could trigger tighter trail multiplier on a position that may be new.
        # Using 1 errs toward giving the position more room — the safer direction.
        _entry = _ledger_map.get(sym)
        try:
            days_held = (_today - datetime.strptime(_entry["entry_date"], "%Y-%m-%d").date()).days \
                        if _entry and "entry_date" in _entry else None
        except Exception:
            days_held = None
        if days_held is None:
            days_held = 1
            logger.warning("days_held missing for %s — using conservative fallback=1", sym)

        high_water = max(price, entry)
        profit_atr = (high_water - entry) / atr if atr > 0 else 0

        reason = None

        # EXIT 1: HARD STOP
        hard_stop = entry - CONFIG.risk.initial_stop_atr_mult * atr
        if price <= hard_stop:
            reason = "hard_stop"
            logger.info("EXIT 1 [HARD STOP] %s $%.2f <= $%.2f", sym, price, hard_stop)
            # Record cooldown — symbol blocked from re-entry for 5 trading days
            try:
                from main import _record_stopout_cooldown
                _record_stopout_cooldown(sym)
            except Exception as _ce:
                logger.debug("Cooldown record failed: %s", _ce)

        # EXIT 2: TRAILING STOP
        if reason is None:
            _comp  = scores.get(sym, 0.0)
            _hlth  = _pre_health.get(sym, {}).get("health", 0.0)
            mult = _trail_mult(days_held, profit_atr, CONFIG.risk, composite=_comp, health=_hlth)
            trail = high_water - mult * atr
            if trail > hard_stop and price <= trail:
                reason = "trailing_stop"
                logger.info("EXIT 2 [TRAIL] %s $%.2f <= $%.2f (%.1fx ATR) comp=%.2f health=%.2f",
                           sym, price, trail, mult, _comp, _hlth)

        # EXIT 3: THESIS INVALIDATION
        # Only exit if composite is genuinely negative (not just out of top-N)
        # AND position is meaningfully losing. Threshold -1.5 requires real
        # cross-sectional weakness, not just rank dropout.
        if reason is None:
            comp = scores.get(sym, 0.0)  # Default 0.0 not -1.0 — unknown != weak
            if comp < -1.5 and pnl_pct < -0.05:
                reason = "thesis_invalid"
                logger.info("EXIT 3 [THESIS] %s composite=%.4f pnl=%.1f%% (confirmed weak thesis + losing)",
                           sym, comp, pnl_pct * 100)

        # EXIT 4B: LEVERAGED ETF HOLD CAP
        # 3x ETFs: max 3 days. 2x ETFs: max 10 days. Volatility decay kills multi-day holds.
        # Uses actual days_held from ledger — prior version used price proxy which fired daily.
        if reason is None:
            LEVERAGED_3X = {"SOXL","SOXS","TQQQ","SQQQ","SPXL","SPXS","UPRO","SPXU",
                           "TECL","TECS","LABU","LABD","FNGU","FNGD","TNA","TZA","FAS","FAZ"}
            LEVERAGED_2X = {"TSLL","TSLS","NVDL","NVDS","UDOW","SDOW","SSO","SDS","QLD","QID"}
            if sym in LEVERAGED_3X and days_held > 3:
                reason = "leveraged_3x_cap"
                logger.info("EXIT 4B [LEV CAP] %s 3x ETF held %d days (max 3)", sym, days_held)
            elif sym in LEVERAGED_2X and days_held > 10:
                reason = "leveraged_2x_cap"
                logger.info("EXIT 4B [LEV CAP] %s 2x ETF held %d days (max 10)", sym, days_held)

# EXIT 5: TIME DECAY
        # Exit only when thesis is genuinely dead — not just flat.
        # Flatness at support/accumulation with good signals is fine to hold.
        # Requires: losing + held long enough + flat + signal deteriorating + health decaying.
        if reason is None and pnl_pct < -0.01 and days_held >= 12:
            ret_20d = (bar_data["close"].iloc[-1] / bar_data["close"].iloc[-20]) - 1 if len(bar_data) >= 20 else None
            ret_5d  = (bar_data["close"].iloc[-1] / bar_data["close"].iloc[-5])  - 1 if len(bar_data) >= 5  else None
            flat_20 = ret_20d is not None and abs(ret_20d) < 0.02
            flat_5  = ret_5d  is not None and abs(ret_5d)  < 0.02
            if flat_20 or flat_5:
                # Check thesis — flat + losing is only an exit if signals are also deteriorating.
                # If composite > 0 or health > 0, stock may be basing — give it room.
                _hrec      = _pre_health.get(sym, {})
                _composite = _hrec.get("composite", 0.0)
                _health    = _hrec.get("health", 0.0)
                if _composite < 0.0 and _health < 0.0:
                    window  = "20d" if flat_20 else "5d"
                    ret_ref = ret_20d if flat_20 else ret_5d
                    reason  = "time_decay"
                    logger.info(
                        "EXIT 5 [TIME] %s flat (%s ret=%.1f%%) losing (%.1f%%) health=%.3f comp=%.3f after %d days",
                        sym, window, ret_ref * 100, pnl_pct * 100, _health, _composite, days_held)
                else:
                    logger.info(
                        "EXIT 5 [TIME] %s flat but thesis intact (health=%.3f comp=%.3f) — holding",
                        sym, _health, _composite)

        if reason:
            exits.append({
                "symbol": sym, "qty": qty, "price": price,
                "entry": entry, "pnl_pct": pnl_pct,
                "reason": reason, "composite": scores.get(sym, 0),
            })
        else:
            _comp  = scores.get(sym, 0.0)
            _hlth  = _pre_health.get(sym, {}).get("health", 0.0)
            mult = _trail_mult(days_held, profit_atr, CONFIG.risk, composite=_comp, health=_hlth)
            trail = max(hard_stop, high_water - mult * atr)
            holds.append({
                "symbol": sym, "pnl_pct": round(pnl_pct * 100, 1),
                "trail": round(trail, 2), "composite": round(_comp, 4),
                "reason": "hold",
            })

    # EXIT 4: PORTFOLIO HEAT — proportional trim across deteriorating positions
    # Old: fully exit single weakest composite position (too blunt — kills one position
    # while leaving others untouched, may exit a position that was about to recover).
    # New: trim ALL held positions where health < 0 by 25%, spreading risk reduction
    # proportionally. Preserves exposure to recovering positions, reduces exposure to
    # confirmed deteriorators. Math-consistent: health < 0 means the hold_monitor's
    # 10-layer score has confirmed thesis decay, not just rank dropout.
    if portfolio_dd < -CONFIG.risk.max_portfolio_drawdown and holds:
        heat_targets = [
            h for h in holds
            if _pre_health.get(h["symbol"], {}).get("health", 0.0) < 0.0
        ]
        if not heat_targets:
            # Fallback: if no position has negative health, trim the single weakest composite
            heat_targets = [min(holds, key=lambda h: h.get("composite", 0))]

        trimmed_syms = set()
        for _ht in heat_targets:
            sym = _ht["symbol"]
            alp = next((p for p in positions if p["symbol"] == sym), None)
            if not alp or sym in trimmed_syms:
                continue
            full_qty = float(alp["qty"])
            trim_shares = max(1, int(full_qty * 0.25))  # 25% trim
            trim_shares = min(trim_shares, int(full_qty) - 1)  # keep at least 1 share
            if trim_shares <= 0:
                continue
            _hlth = _pre_health.get(sym, {}).get("health", 0.0)
            exits.append({
                "symbol": sym, "qty": trim_shares,
                "price": alp["current_price"], "entry": alp["avg_entry"],
                "pnl_pct": alp.get("unrealized_pnl_pct", 0),
                "reason": "portfolio_heat",
                "composite": _ht.get("composite", 0),
            })
            trimmed_syms.add(sym)
            holds = [h for h in holds if h["symbol"] != sym]
            logger.info("EXIT 4 [HEAT TRIM 25%%] %s health=%.3f comp=%.4f — %d of %d shares",
                       sym, _hlth, _ht.get("composite", 0), trim_shares, int(full_qty))

        if trimmed_syms:
            logger.info("EXIT 4 [HEAT] portfolio_dd=%.2f%% — trimmed %d positions: %s",
                       portfolio_dd * 100, len(trimmed_syms), sorted(trimmed_syms))

    # ── MATH TRIM EXECUTION — driven by hold_health.json 8-layer score ──────────
    # compute_trim() in hold_monitor produces a continuous trim% from:
    # severity, stop proximity, FAR penalty, composite slope, P&L context.
    # This is the authoritative trim signal — more precise than agent approximation.
    # Agent TRIM decisions are demoted to advisory logging only (calibration data).
    try:
        import json as _jmath
        from pathlib import Path as _Pmath
        _health_path = _Pmath("hold_health.json")
        if _health_path.exists():
            _health_data = _jmath.loads(_health_path.read_text())
            hold_syms = {h["symbol"] for h in holds}
            for _sym, _hrec in _health_data.items():
                if _sym not in hold_syms:
                    continue
                _trim = _hrec.get("trim", {})
                _action = _trim.get("action", "HOLD")
                if _action == "HOLD":
                    continue
                _trim_shares = int(_trim.get("trim_shares", 0))
                _trim_pct    = float(_trim.get("trim_pct", 0))
                _reasoning   = _trim.get("reasoning", "")
                _label       = _trim.get("action_label", _action)
                if _trim_shares <= 0:
                    continue
                # Full EXIT from math: add as full exit to exits list
                if _action == "EXIT":
                    alp = next((p for p in positions if p["symbol"] == _sym), None)
                    if alp and _sym not in {e["symbol"] for e in exits}:
                        exits.append({
                            "symbol":    _sym,
                            "qty":       alp["qty"],
                            "price":     alp["current_price"],
                            "entry":     alp["avg_entry"],
                            "pnl_pct":   alp.get("unrealized_pnl_pct", 0),
                            "reason":    "math_exit",
                            "composite": scores.get(_sym, 0),
                            "trim_detail": _reasoning,
                        })
                        holds = [h for h in holds if h["symbol"] != _sym]
                        logger.warning("MATH EXIT [%s] %s — %s", _action, _sym, _label)
                # Partial TRIM from math: submit partial sell
                else:
                    alp = next((p for p in positions if p["symbol"] == _sym), None)
                    if alp and _sym not in {e["symbol"] for e in exits}:
                        full_qty = float(alp["qty"])
                        # Cap trim at full_qty-1 — full exits go through EXIT path
                        safe_trim = min(_trim_shares, int(full_qty) - 1) if full_qty > 1 else 0
                        if safe_trim > 0:
                            exits.append({
                                "symbol":    _sym,
                                "qty":       safe_trim,
                                "price":     alp["current_price"],
                                "entry":     alp["avg_entry"],
                                "pnl_pct":   alp.get("unrealized_pnl_pct", 0),
                                "reason":    f"math_trim_{_trim_pct:.0%}",
                                "composite": scores.get(_sym, 0),
                                "trim_detail": _reasoning,
                            })
                            holds = [h for h in holds if h["symbol"] != _sym]
                            logger.warning("MATH TRIM [%s] %s — %d of %d shares (%.0f%%) — %s",
                                         _action, _sym, safe_trim, int(full_qty),
                                         _trim_pct * 100, _label)
    except Exception as _me:
        logger.warning("Math trim block failed (%s) — skipping", _me)
    # ─────────────────────────────────────────────────────────────────────────

    # REPORT
    logger.info("")
    logger.info("=" * 60)
    logger.info("  EXIT SUMMARY")
    logger.info("=" * 60)

    if exits:
        logger.info("  EXITS (%d):", len(exits))
        for ex in exits:
            logger.info("    %s  %s  pnl=%+.1f%%  comp=%.4f",
                       ex["symbol"], ex["reason"], ex["pnl_pct"] * 100, ex.get("composite", 0))
    else:
        logger.info("  No exits triggered.")

    if holds:
        logger.info("  HOLDS (%d):", len(holds))
        for h in holds:
            logger.info("    %s  pnl=%+.1f%%  trail=$%s  comp=%s",
                       h["symbol"], h.get("pnl_pct", 0),
                       h.get("trail", "?"), h.get("composite", "?"))

    logger.info("=" * 60)

    # ── HoldAgent — ADVISORY ONLY — logs for calibration, does not execute ──────
    # Math trim (hold_health.json) is the execution trigger.
    # Agent decisions are logged here for outcome tagging and Layer 3 calibration.
    # When prompt_calibrator.py runs (Layer 3), it will compare agent decisions
    # against actual outcomes to tune prompts. All data preserved.
    try:
        import json as _json
        from pathlib import Path as _Path

        _dec_path = _Path("hold_decisions.json")
        _raw_decisions = []
        if _dec_path.exists():
            try:
                _raw_decisions = _json.loads(_dec_path.read_text())
            except Exception:
                _raw_decisions = []

        # Most recent decision per symbol
        _latest = {}
        for d in _raw_decisions:
            sym = d.get("symbol")
            if sym:
                _latest[sym] = d

        for sym, dec in _latest.items():
            conf      = dec.get("confidence", 0)
            decision  = dec.get("decision", "HOLD")
            reasoning = dec.get("reasoning", "")
            ts        = dec.get("timestamp", "unknown time")
            # Log all agent decisions for calibration — no execution
            if decision == "EXIT":
                logger.info("AGENT [advisory] EXIT %s — %s (conf=%.2f, from %s) [not executed — math trim governs]",
                           sym, reasoning, conf, ts)
            elif decision == "TRIM":
                logger.info("AGENT [advisory] TRIM %s — %s (conf=%.2f, from %s) [not executed — math trim governs]",
                           sym, reasoning, conf, ts)
            else:
                logger.info("AGENT [advisory] HOLD %s — %s (conf=%.2f, from %s)",
                           sym, reasoning, conf, ts)
    except Exception as e:
        logger.warning("HoldAgent advisory log failed (%s) — skipping.", e)
    # ─────────────────────────────────────────────────────────────────────────

    # EXECUTE
    if exits and not dry_run:
        for ex in exits:
            logger.info("SELL %s %s [%s]", ex["qty"], ex["symbol"], ex["reason"])
            result = dm.alpaca.submit_order(
                ex["symbol"], ex["qty"], "SELL", "market",
                client_order_id=f"{ex['reason']}_{ex['symbol']}_{datetime.now():%Y%m%d%H%M%S}"
            )
            if "error" not in result:
                logger.info("  OK: %s", result.get("status", "submitted"))
                # Update ledger — route partial trims to record_trim, full exits to record_exit.
                # Previously all paths called record_exit, which closed the full position in the
                # ledger even when only a fraction of shares were sold. This caused 8 positions
                # to disappear from the ledger while remaining open in Alpaca.
                try:
                    from ledger import Ledger as _Ledger
                    _l = _Ledger()
                    _price  = float(ex.get("price", 0))
                    _date   = datetime.now().strftime("%Y-%m-%d")
                    _reason = ex["reason"]
                    _qty    = int(ex.get("qty", 0))
                    if "trim" in _reason:
                        # Partial trim — keep position open, reduce share count
                        _l.record_trim("v5.4", ex["symbol"], _qty, _price, _date, _reason)
                    else:
                        # Full exit — close position in ledger
                        _l.record_exit("v5.4", ex["symbol"], _price, _date, _reason)
                except Exception as _le:
                    logger.warning("Ledger record failed for %s: %s", ex["symbol"], _le)
            else:
                logger.error("  FAILED: %s", result["error"])
    elif dry_run and exits:
        logger.info("DRY RUN - no orders submitted")

    # ── TRIM LOG — records partial sells for calibration ─────────────────────
    # Full exits are tagged by outcome_tracker via Alpaca order history.
    # Partial trims don't appear as closed trades so we log them separately.
    # trim_log.json feeds into Layer 3 prompt calibration alongside outcome_log.json.
    if not dry_run:
        try:
            import json as _tjson
            from pathlib import Path as _tPath
            _trim_exits = [e for e in exits if "trim" in e.get("reason", "")]
            if _trim_exits:
                _tlog_path = _tPath("trim_log.json")
                _tlog = []
                if _tlog_path.exists():
                    try:
                        _tlog = _tjson.loads(_tlog_path.read_text())
                    except Exception:
                        _tlog = []
                for _te in _trim_exits:
                    # Load agent decision for this symbol if available
                    _agent_dec = _latest.get(_te["symbol"], {}) if "_latest" in dir() else {}
                    _tlog.append({
                        "timestamp":      datetime.now().isoformat(),
                        "symbol":         _te["symbol"],
                        "trim_qty":       _te["qty"],
                        "price":          _te["price"],
                        "pnl_pct":        round(float(_te.get("pnl_pct", 0)) * 100, 2),
                        "reason":         _te["reason"],
                        "composite":      _te.get("composite", 0),
                        "trim_detail":    _te.get("trim_detail", ""),
                        "agent_decision": _agent_dec.get("decision", "no_record"),
                        "agent_conf":     _agent_dec.get("confidence", None),
                        "agent_reasoning":_agent_dec.get("reasoning", ""),
                    })
                _tlog_path.write_text(_tjson.dumps(_tlog, indent=2))
                logger.info("[TrimLog] Logged %d trim(s) → trim_log.json", len(_trim_exits))
        except Exception as _tle:
            logger.warning("[TrimLog] Non-fatal error: %s", _tle)
    # ─────────────────────────────────────────────────────────────────────────

    # ── OUTCOME TAGGING — Layer 1 ──────────────────────────────────────────
    # Runs after every execution cycle. Tags closed trades with agent decisions.
    # Writes to outcome_log.json — the labeled dataset for prompt calibration.
    if not dry_run:
        try:
            import outcome_tracker
            n = outcome_tracker.run_tracker(verbose=False)
            if n > 0:
                logger.info("[OutcomeTracker] Tagged %d new closed trade(s) → outcome_log.json", n)
        except Exception as e:
            logger.warning("[OutcomeTracker] Non-fatal error: %s", e)
    # ───────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_exit_monitor(dry_run=args.dry_run)
