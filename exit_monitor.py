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


def _atr_percentile(bars, period=14, lookback=60):
    """Return where today's ATR sits in its own 60-day distribution (0.0-1.0).
    Low vol  -> pctile < 0.25. High vol -> pctile > 0.75.
    Used to scale hard stop multiplier dynamically (P1-2).
    """
    h, l, c = bars["high"], bars["low"], bars["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr_series = tr.rolling(period).mean().dropna()
    if len(atr_series) < 2:
        return 0.5  # default: normal regime
    current = float(atr_series.iloc[-1])
    window = atr_series.iloc[-lookback:]
    pctile = float((window < current).mean())
    return pctile


def _vol_regime_stop_mult(bars, base_mult=3.0, period=14, lookback=60):
    """Vol-regime-aware hard stop multiplier (P1-2).
    Derives multiplier from ATR percentile distribution -- not hand-picked steps.
    Low vol  (pctile < 0.25): 2.5x -- statistically equivalent protection, tighter stop
    Normal   (0.25-0.75):     3.0x -- unchanged
    High vol (pctile > 0.75): 3.5x -- extra room for noise, avoids whipsaw
    Reference: audit P1-2, Kaminski & Lo 2014.
    """
    pctile = _atr_percentile(bars, period=period, lookback=lookback)
    if pctile < 0.25:
        mult = 2.5
    elif pctile > 0.75:
        mult = 3.5
    else:
        mult = base_mult
    return mult, pctile


def _ou_theta(bars, lookback=30):
    """Estimate Ornstein-Uhlenbeck mean-reversion speed (theta) per stock.
    Method: OLS regression of log-price changes on lagged log-price deviation from mean.
    dX = theta * (mu - X) * dt  ->  theta = -slope of OLS(delta_X ~ X_lagged)
    Half-life = log(2) / theta  (days to revert halfway to mean)
    Returns theta capped to [log(2)/15, log(2)/2] (half-life 2-15 days).
    Returns None if insufficient data.
    Reference: Leung & Zhang 2019, arXiv:1701.03960.
    """
    try:
        closes = bars["close"].dropna().tail(lookback)
        if len(closes) < 10:
            return None
        log_p = np.log(closes.values.astype(float))
        mu = log_p.mean()
        X = log_p[:-1] - mu          # deviation from mean, lagged
        dX = np.diff(log_p)          # changes
        # OLS: dX = alpha + slope * X  ->  theta = -slope
        slope = float(np.polyfit(X, dX, 1)[0])
        theta = -slope
        # Cap: half-life between 2 and 15 trading days
        theta = max(np.log(2) / 15, min(np.log(2) / 2, theta))
        return theta
    except Exception:
        return None


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

    # P0-8: Override macro regime from canonical macro_context.json
    try:
        import json as _mcjson
        from pathlib import Path as _mcPath
        _mc_path = _mcPath("macro_context.json")
        if _mc_path.exists():
            _mc_data = _mcjson.loads(_mc_path.read_text())
            _mc_regime = _mc_data.get("macro_regime", "")
            if _mc_regime:
                macro["regime"] = _mc_regime
                logger.info("[P0-8] Using macro_context.json regime=%s (canonical source)", _mc_regime)
    except Exception as _mce:
        logger.warning("[P0-8] Could not load macro_context.json (%s) -- using data_feeds fallback", _mce)

    # Run signal engine for thesis check (current composite scores)
    signals = engine.generate_signals(bars, macro, dataset["sentiment"], spy_bars)
    # Use _last_full_signals so held symbols that decayed out of the top-N
    # get their real composite score instead of the -1.0 default.
    full_map = getattr(engine, "_last_full_signals", {s.symbol: s for s in signals})
    scores = {sym: full_map[sym].composite_score if sym in full_map else s.composite_score
              for s in signals for sym in [s.symbol]}
    scores.update({sym: full_map[sym].composite_score for sym in held if sym in full_map})
    for sym in held:
        if sym not in scores:
            scores[sym] = -1.0  # Genuinely not scored — thesis weak

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
            atr = abs(price * 0.02)

        # Days held from ledger entry_date; fallback 7 if missing
        _entry = _ledger_map.get(sym)
        try:
            days_held = (_today - datetime.strptime(_entry["entry_date"], "%Y-%m-%d").date()).days \
                        if _entry and "entry_date" in _entry else 7
        except Exception:
            days_held = 7

        high_water = max(price, entry)
        profit_atr = (high_water - entry) / atr if atr > 0 else 0

        reason = None

        # EXIT 1: HARD STOP — P1-2: vol-regime-aware multiplier
        # Stop width scales with where current ATR sits in its 60-day distribution.
        # Low vol (pctile<0.25): 2.5x | Normal: 3.0x | High vol (pctile>0.75): 3.5x
        _stop_mult, _atr_pctile = _vol_regime_stop_mult(
            bar_data, base_mult=CONFIG.risk.initial_stop_atr_mult)
        hard_stop = entry - _stop_mult * atr
        if price <= hard_stop:
            reason = "hard_stop"
            logger.info("EXIT 1 [HARD STOP] %s $%.2f <= $%.2f (%.1fx ATR, atr_pctile=%.2f)",
                        sym, price, hard_stop, _stop_mult, _atr_pctile)

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
        if reason is None and pnl_pct < -0.01 and days_held >= 12:
            ret_20d = (bar_data["close"].iloc[-1] / bar_data["close"].iloc[-20]) - 1 if len(bar_data) >= 20 else None
            ret_5d  = (bar_data["close"].iloc[-1] / bar_data["close"].iloc[-5])  - 1 if len(bar_data) >= 5  else None
            flat_20 = ret_20d is not None and abs(ret_20d) < 0.02
            flat_5  = ret_5d  is not None and abs(ret_5d)  < 0.02
            if flat_20 or flat_5:
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

    # EXIT 4: PORTFOLIO HEAT
    if portfolio_dd < -CONFIG.risk.max_portfolio_drawdown and holds:
        weakest = min(holds, key=lambda h: h.get("composite", 0))
        sym = weakest["symbol"]
        alp = next((p for p in positions if p["symbol"] == sym), None)
        if alp:
            exits.append({
                "symbol": sym, "qty": alp["qty"], "price": alp["current_price"],
                "entry": alp["avg_entry"], "pnl_pct": alp.get("unrealized_pnl_pct", 0),
                "reason": "portfolio_heat", "composite": weakest.get("composite", 0),
            })
            holds = [h for h in holds if h["symbol"] != sym]
            logger.info("EXIT 4 [HEAT] Trimming %s (weakest composite=%.4f)",
                       sym, weakest.get("composite", 0))

    # ── MATH TRIM EXECUTION — driven by hold_health.json 8-layer score ──────────
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
                # Full EXIT from math
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
                # Partial TRIM from math
                else:
                    alp = next((p for p in positions if p["symbol"] == _sym), None)
                    if alp and _sym not in {e["symbol"] for e in exits}:
                        full_qty = float(alp["qty"])
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

    # ── HoldAgent — ADVISORY ONLY ──────────────────────────────────────────
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
                try:
                    from ledger import Ledger as _Ledger
                    _l = _Ledger()
                    exit_price = float(ex.get("price", 0))
                    _l.record_exit(
                        "v5.4", ex["symbol"], exit_price,
                        datetime.now().strftime("%Y-%m-%d"), ex["reason"]
                    )
                except Exception as _le:
                    logger.warning("Ledger record_exit failed for %s: %s", ex["symbol"], _le)

                # P0-1: Write outcome_pending.json sidecar keyed by Alpaca order ID
                try:
                    import json as _opjson
                    from pathlib import Path as _opPath
                    _order_id = result.get("id", "unknown")
                    _op_path = _opPath("outcome_pending.json")
                    _op_data = {}
                    if _op_path.exists():
                        try:
                            _op_data = _opjson.loads(_op_path.read_text())
                        except Exception:
                            _op_data = {}
                    _op_data[_order_id] = {
                        "symbol":           ex["symbol"],
                        "exit_reason":      ex["reason"],
                        "composite":        ex.get("composite", 0),
                        "trim_detail":      ex.get("trim_detail", ""),
                        "agent_decision":   _latest.get(ex["symbol"], {}).get("decision", "no_record"),
                        "agent_confidence": _latest.get(ex["symbol"], {}).get("confidence", None),
                        "agent_reasoning":  _latest.get(ex["symbol"], {}).get("reasoning", ""),
                        "submitted_at":     datetime.now().isoformat(),
                    }
                    _op_path.write_text(_opjson.dumps(_op_data, indent=2))
                    logger.info("[P0-1] outcome_pending.json updated for %s (order %s)",
                                ex["symbol"], _order_id)
                except Exception as _ope:
                    logger.warning("[P0-1] outcome_pending write failed for %s: %s", ex["symbol"], _ope)
            else:
                logger.error("  FAILED: %s", result["error"])
    elif dry_run and exits:
        logger.info("DRY RUN - no orders submitted")

    # ── TRIM LOG ─────────────────────────────────────────────────────────────
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
                logger.info("[TrimLog] Logged %d trim(s) -> trim_log.json", len(_trim_exits))
        except Exception as _tle:
            logger.warning("[TrimLog] Non-fatal error: %s", _tle)
    # ─────────────────────────────────────────────────────────────────────────

    # ── OUTCOME TAGGING ───────────────────────────────────────────────────────
    if not dry_run:
        try:
            import outcome_tracker
            n = outcome_tracker.run_tracker(verbose=False)
            if n > 0:
                logger.info("[OutcomeTracker] Tagged %d new closed trade(s) -> outcome_log.json", n)
        except Exception as e:
            logger.warning("[OutcomeTracker] Non-fatal error: %s", e)
    # ─────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_exit_monitor(dry_run=args.dry_run)
