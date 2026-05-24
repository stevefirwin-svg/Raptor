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

    # Signal-quality modifier — calibrated 2026-05-22 from backtest parameter sweep.
    # 1565 trades, avg_hold=7.9d, trail exits: 561 profit / 834 loss.
    #
    # Sweep across 180 combinations found:
    #   threshold 0.2 → 0.3: lower threshold captures more strong signals (42% vs 38%)
    #   wide_mult  1.6 → 1.3: 1.6× gives meaningful room vs 1.3× marginal effect
    #   tight_mult 0.80→ 0.75: 0.80 less aggressive tightening (losses already small)
    #
    # Expected effect: convert ~9 trail_loss → trail_profit per 1565 trades (+9.1% Sharpe)
    # Net trail width vs baseline: 1.6×42% + 1.0×16% + 0.8×42% = 1.176× (was 1.019×)
    #
    # Previous values: threshold=0.3, wide=1.3, tight=0.75 (round numbers, uncalibrated)
    signal_strength = (composite + health) / 2.0
    if signal_strength > 0.2:
        modifier = 1.6   # Strong — give significant room (calibrated from backtest)
    elif signal_strength < -0.2:
        modifier = 0.80  # Weak — tighten trail (calibrated from backtest)
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
    # get their real composite score instead of the -1.0 default.
    # generate_signals() sets this attribute on the engine instance after scoring all symbols.
    full_map = getattr(engine, "_last_full_signals", {s.symbol: s for s in signals})
    scores = {sym: full_map[sym].composite_score if sym in full_map else s.composite_score
              for s in signals for sym in [s.symbol]}
    scores.update({sym: full_map[sym].composite_score for sym in held if sym in full_map})
    for sym in held:
        if sym not in scores:
            scores[sym] = 0.0   # Not scored today — unknown, not weak.
                                    # Entry gates may have filtered this symbol
                                    # (extended, pulling back, not a fresh entry).
                                    # 0.0 = neutral; real decay shows in hold_monitor
                                    # health score over multiple days, not a single miss.

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
            # Cannot compute ATR from bar data — skip rather than fake
            logger.warning("[ATR] %s: cannot compute ATR from bar data — skipping exit check", sym)
            holds.append({"symbol": sym, "reason": "no_atr", "pnl_pct": pnl_pct})
            continue

        # Days held from ledger entry_date; fallback 7 if missing
        _entry = _ledger_map.get(sym)
        try:
            days_held = (_today - datetime.strptime(_entry["entry_date"], "%Y-%m-%d").date()).days                         if _entry and "entry_date" in _entry else None
        except Exception:
            days_held = None

        if days_held is None:
            # Entry date unknown — use conservative assumption (day 1 = widest trail)
            # Better to give too much room than to fake a mid-hold tighter trail
            logger.warning("[DaysHeld] %s not in ledger — assuming day 1 (widest trail). "
                           "Run backfill_ledger.py --write to fix.", sym)
            days_held = 1

        high_water = max(price, entry)
        profit_atr = (high_water - entry) / atr if atr > 0 else 0

        reason = None

        # EXIT 1: HARD STOP — volatility-regime aware (GAP 3)
        # Fixed 3.0×ATR regardless of vol regime causes two failure modes:
        #   Low vol: 3 ATR is too wide — takes excessive loss before stopping out.
        #   High vol: 3 ATR is too tight — whipsaws out of valid positions on noise.
        # ATR percentile (rolling 60d) scales the multiplier:
        #   Low vol  (ATR < 25th pctile) → 2.5× ATR — tighter, less room for loss
        #   Normal   (25th–75th pctile)  → 3.0× ATR — unchanged baseline
        #   High vol (ATR > 75th pctile) → 3.5× ATR — wider, survives normal noise
        atr_pctile = 0.5  # neutral default if we can't compute
        try:
            if len(bar_data) >= 60:
                rolling_atr = bar_data["close"].diff().abs().rolling(14).mean()
                valid_atrs = rolling_atr.dropna().tail(60)
                if len(valid_atrs) >= 20:
                    atr_pctile = float((valid_atrs < atr).mean())
        except Exception:
            pass

        if atr_pctile < 0.25:
            stop_atr_mult = 2.5   # low vol — tighter stop
        elif atr_pctile > 0.75:
            stop_atr_mult = 3.5   # high vol — wider stop, avoid whipsaw
        else:
            stop_atr_mult = CONFIG.risk.initial_stop_atr_mult  # normal — baseline

        hard_stop = entry - stop_atr_mult * atr
        if price <= hard_stop:
            reason = "hard_stop"
            logger.info("EXIT 1 [HARD STOP] %s $%.2f <= $%.2f (%.1fx ATR, vol_pctile=%.2f)",
                       sym, price, hard_stop, stop_atr_mult, atr_pctile)

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

        # EXIT 3: THESIS INVALIDATION — regime-scaled threshold (GAP 4)
        # Fixed -1.5 composite fails in two directions:
        #   BULLISH regime: -1.5 is genuinely weak — threshold is appropriate.
        #   RISK_OFF regime: universe compresses, median composite drops. A -1.5
        #     composite may be average weakness, not thesis failure — mass exits
        #     occur exactly when regime-wide drawdown is already happening.
        # Threshold scales with regime to prevent panic exits during drawdowns.
        if reason is None:
            comp = scores.get(sym, 0.0)
            _macro_regime = _pre_health.get(sym, {}).get("regime", "") or ""
            # Read macro regime from macro_context.json if available
            _mc_regime = "NEUTRAL"
            try:
                import json as _mj
                from pathlib import Path as _mp
                _mcf = _mp("macro_context.json")
                if _mcf.exists():
                    _mc_regime = _mj.loads(_mcf.read_text()).get("macro_regime", "NEUTRAL")
            except Exception:
                pass

            thesis_threshold = {
                "RISK_ON":  -2.0,   # generous — strong market, give more room
                "NEUTRAL":  -1.5,   # baseline — unchanged from prior behavior
                "RISK_OFF": -2.0,   # universe compressed — require stronger signal to exit
                "CRISIS":   -2.5,   # extreme compression — only exit truly broken positions
            }.get(_mc_regime, -1.5)

            if comp < thesis_threshold and pnl_pct < -0.05:
                reason = "thesis_invalid"
                logger.info(
                    "EXIT 3 [THESIS] %s composite=%.4f < %.1f (regime=%s) pnl=%.1f%%",
                    sym, comp, thesis_threshold, _mc_regime, pnl_pct * 100
                )

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

    # EXIT 4: PORTFOLIO HEAT — proportional trim-all (GAP 7) ──────────────────
    # Previous behavior: find weakest composite, full exit. Binary and blunt.
    #   Problem 1: Fully exits one position — arbitrary concentration of pain.
    #   Problem 2: Same response to -8.1% dd and -15% dd — doesn't scale.
    #   Problem 3: Exits the WEAKEST composite, but a health<0 position that's
    #              down 15% may have higher composite than a position down 3%.
    #
    # New behavior: trim ALL positions with health<0 by a percentage that
    # scales continuously with the excess drawdown beyond the threshold.
    #
    # heat_trim_pct = clip(excess_dd / threshold, 0.10, 0.50)
    #   excess_dd = |portfolio_dd| - threshold
    #   threshold = max_portfolio_drawdown (config)
    #
    # Examples:
    #   portfolio_dd=-8.1%, threshold=8% → excess=0.1% → trim 1.25% of health<0 positions (floor 10%)
    #   portfolio_dd=-10%,  threshold=8% → excess=2.0% → trim 25% of health<0 positions
    #   portfolio_dd=-16%,  threshold=8% → excess=8.0% → trim 100%... capped at 50%
    #
    # Only health<0 positions are trimmed — strengthening positions are untouched.
    # Each trim: min(heat_trim_shares, qty-1) — never a full exit via this path.
    # Full exits only happen via the mechanical exit conditions (EXIT 1-5) or math trim EXIT.
    if portfolio_dd < -CONFIG.risk.max_portfolio_drawdown and holds:
        threshold   = CONFIG.risk.max_portfolio_drawdown  # e.g. 0.08
        excess_dd   = abs(portfolio_dd) - threshold       # how far beyond threshold
        heat_trim_pct = float(np.clip(excess_dd / threshold, 0.10, 0.50))

        # Load health scores to identify health<0 positions
        heat_trimmed = []
        try:
            import json as _jheat
            from pathlib import Path as _Pheat
            _hf = _Pheat("hold_health.json")
            _health_scores = _jheat.loads(_hf.read_text()) if _hf.exists() else {}
        except Exception:
            _health_scores = {}

        for h in holds:
            sym = h["symbol"]
            if sym in {e["symbol"] for e in exits}:
                continue  # already being exited this run

            # Only trim positions with negative health
            health_rec  = _health_scores.get(sym, {})
            health_score = float(health_rec.get("health_score", 0.0))
            if health_score >= 0:
                continue  # strengthening/stable — leave it alone

            alp = next((p for p in positions if p["symbol"] == sym), None)
            if not alp:
                continue

            qty = int(float(alp.get("qty", 0)))
            if qty <= 1:
                continue

            heat_shares = max(1, int(qty * heat_trim_pct))
            heat_shares = min(heat_shares, qty - 1)  # never full exit via heat path

            exits.append({
                "symbol":      sym,
                "qty":         heat_shares,
                "price":       alp["current_price"],
                "entry":       alp["avg_entry"],
                "pnl_pct":     alp.get("unrealized_pnl_pct", 0),
                "reason":      "portfolio_heat",
                "trim_pct":    round(heat_trim_pct * 100, 1),
                "health_score": health_score,
                "composite":   h.get("composite", 0),
            })
            heat_trimmed.append(sym)

        if heat_trimmed:
            logger.info(
                "EXIT 4 [HEAT] portfolio_dd=%.2f%% (%.2f%% excess) — trimming %.0f%% "
                "of %d health<0 positions: %s",
                portfolio_dd * 100, excess_dd * 100,
                heat_trim_pct * 100, len(heat_trimmed), heat_trimmed
            )
        elif holds:
            # All positions are health>=0 — log and skip (don't force-exit a healthy position)
            logger.warning(
                "EXIT 4 [HEAT] portfolio_dd=%.2f%% but all %d positions have health>=0 — no trim",
                portfolio_dd * 100, len(holds)
            )

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
                # Update ledger — moves position to closed list for analytics
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
                _tlog_tmp = str(_tlog_path) + ".tmp"
                with open(_tlog_tmp, "w") as _tf:
                    _tjson.dump(_tlog, _tf, indent=2)
                import os as _os; _os.replace(_tlog_tmp, _tlog_path)
                logger.info("[TrimLog] Logged %d trim(s) → trim_log.json", len(_trim_exits))
        except Exception as _tle:
            logger.warning("[TrimLog] Non-fatal error: %s", _tle)
    # ─────────────────────────────────────────────────────────────────────────

    # ── GAP 9: AFTERNOON COMPOSITE RESCORE ────────────────────────────────────
    # Signals computed once at 9:35 AM. By 3:50 PM they're 6+ hours stale.
    # The signal engine already ran above to get current composites for exit checks.
    # Use those fresh scores to:
    #   1. Update composite field in hold_health.json for all held positions
    #   2. Flag positions where composite decayed significantly since morning
    #      (delta < -0.5 = notable deterioration) for next-morning exit priority
    #   3. Log an actionable warning for positions approaching thesis invalidation
    #
    # Zero new API calls — uses full_map already computed above.
    try:
        import json as _rj
        from pathlib import Path as _rp
        _hh_path = _rp("hold_health.json")
        if _hh_path.exists() and full_map:
            _hh = _rj.loads(_hh_path.read_text())
            _rescore_log = []

            for sym in held:
                if sym not in full_map:
                    continue
                fresh_comp = full_map[sym].composite_score
                if sym not in _hh:
                    continue

                morning_comp = float(_hh[sym].get("composite", fresh_comp))
                comp_delta   = fresh_comp - morning_comp

                # Update composite to afternoon value
                _hh[sym]["composite"]           = round(fresh_comp, 4)
                _hh[sym]["composite_morning"]   = round(morning_comp, 4)
                _hh[sym]["composite_delta"]     = round(comp_delta, 4)
                _hh[sym]["rescore_timestamp"]   = datetime.now().isoformat()

                # Flag meaningful decay — thesis deteriorating intraday
                if comp_delta < -0.5:
                    _hh[sym]["afternoon_flag"] = "COMPOSITE_DECAY"
                    logger.warning(
                        "GAP9 [DECAY] %s composite %.3f → %.3f (Δ%.3f) — "
                        "monitor for thesis invalidation at open tomorrow",
                        sym, morning_comp, fresh_comp, comp_delta
                    )
                    _rescore_log.append({"symbol": sym, "morning": morning_comp,
                                         "afternoon": fresh_comp, "delta": comp_delta})
                elif comp_delta > 0.3:
                    _hh[sym]["afternoon_flag"] = "COMPOSITE_STRENGTH"
                    logger.info("GAP9 [STRENGTH] %s composite %.3f → %.3f (Δ+%.3f)",
                                sym, morning_comp, fresh_comp, comp_delta)
                else:
                    _hh[sym]["afternoon_flag"] = "STABLE"

            _hh_tmp = str(_hh_path) + ".tmp"
            with open(_hh_tmp, "w") as _hf:
                _rj.dump(_hh, _hf, indent=2)
            import os as _rhOS; _rhOS.replace(_hh_tmp, _hh_path)
            if _rescore_log:
                logger.warning("GAP9: %d position(s) with meaningful composite decay: %s",
                               len(_rescore_log), [r["symbol"] for r in _rescore_log])
            else:
                logger.info("GAP9: Afternoon rescore complete — all composites stable")
    except Exception as _rse:
        logger.warning("GAP9 rescore non-fatal error: %s", _rse)
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
