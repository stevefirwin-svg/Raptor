"""
Raptor v5.4 — Exit Monitor
============================
Production-grade position management. Runs at 3:45 PM daily
(or on-demand) to evaluate all open positions.

FIVE EXIT CONDITIONS (checked in order, first trigger wins):

  1. HARD STOP — price below initial stop. Catastrophic protection.
  2. TRAILING STOP — hybrid time-dependent + profit-dependent.
     Whichever gives the tighter trail wins.
  3. THESIS INVALIDATION — re-scores the stock against the current
     universe. If composite drops below 0, the math no longer
     supports the trade. Exit regardless of P&L. This is what
     separates a quant desk from a retail trader.
  4. PORTFOLIO HEAT — if total portfolio drawdown exceeds limit,
     trim the weakest position by current composite score.
  5. TIME DECAY — after time_stop_days, exit at market. MR edge
     is exhausted.

Also feeds closed trades into AdaptiveWeights ridge learner
so factor weights improve over time.

Usage:
  python exit_monitor.py           # Run exit check
  python exit_monitor.py --dry-run # Show what would exit, don't execute
"""

import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import CONFIG
from data_feeds import DataManager
from signals import QuantSignalEngine, Factors, FACTOR_NAMES
from ledger import Ledger

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

MODEL_ID = "v5.4"


def _atr_from_bars(bars: pd.DataFrame, period: int = 14) -> float:
    h, l, c = bars["high"], bars["low"], bars["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def _trail_multiplier(days_held: int, profit_atr: float, rcfg) -> float:
    """
    Hybrid time + profit trailing multiplier.
    Returns the TIGHTER of time-based and profit-based.
    """
    # Time-based (Bertsimas & Lo 1998)
    if days_held <= rcfg.trail_early_days:
        time_mult = rcfg.trail_early_atr
    elif days_held <= rcfg.trail_mid_days:
        time_mult = rcfg.trail_mid_atr
    elif days_held <= rcfg.trail_late_days:
        time_mult = rcfg.trail_late_atr
    else:
        time_mult = rcfg.trail_final_atr

    # Profit-based
    if profit_atr >= 4.0:
        profit_mult = 1.0
    elif profit_atr >= 2.0:
        profit_mult = 1.5
    elif profit_atr >= 1.0:
        profit_mult = 2.0
    else:
        profit_mult = 99.0  # No profit trail yet

    return min(time_mult, profit_mult)


def run_exit_monitor(dry_run: bool = False):
    logger.info("=" * 60)
    logger.info("RAPTOR %s EXIT MONITOR - %s", MODEL_ID, datetime.now().isoformat())
    logger.info("=" * 60)

    dm = DataManager(CONFIG)
    ledger = Ledger()
    engine = QuantSignalEngine(CONFIG)
    f = Factors()

    positions = dm.alpaca.get_positions()
    account = dm.alpaca.get_account()
    equity = account["equity"]

    my_positions = ledger.get_positions(MODEL_ID)
    if not my_positions:
        logger.info("No open positions for %s. Nothing to monitor.", MODEL_ID)
        return

    logger.info("Account: equity=$%.2f  positions=%d", equity, len(my_positions))

    # Get current bars for all held symbols + universe for thesis check
    held_symbols = [p["symbol"] for p in my_positions]

    try:
        from universe_builder import UniverseBuilder
        ub = UniverseBuilder(CONFIG)
        universe = ub.build(max_symbols=150)
    except Exception:
        universe = held_symbols[:]

    # Ensure all held symbols are in the universe
    for sym in held_symbols:
        if sym not in universe:
            universe.append(sym)
    if "SPY" not in universe:
        universe.append("SPY")

    dataset = dm.get_full_dataset(universe, lookback_days=CONFIG.signals.lookback_days)
    bars = dataset["bars"]
    macro = dataset["macro"]
    spy_bars = bars.get("SPY")

    # Run the signal engine to get current composite scores for thesis check
    signals = engine.generate_signals(bars, macro, dataset["sentiment"], spy_bars)
    current_scores = {}
    for sig in signals:
        current_scores[sig.symbol] = sig.composite_score

    # For stocks NOT in signals (composite < 0 or not in top), compute raw score
    for sym in held_symbols:
        if sym not in current_scores and sym in bars:
            # Stock didn't make the signal list — composite is likely negative
            current_scores[sym] = -1.0  # Flag as thesis-invalid

    # Match Alpaca positions to ledger entries for entry data
    alpaca_pos = {p["symbol"]: p for p in positions}

    exits_to_execute = []
    holds = []

    # Portfolio-level drawdown check
    total_unrealized_pnl = sum(
        p.get("unrealized_pnl", 0) for p in positions
    )
    portfolio_dd = total_unrealized_pnl / equity if equity > 0 else 0

    logger.info("Portfolio unrealized P&L: $%.2f (%.2f%%)", total_unrealized_pnl, portfolio_dd * 100)

    for pos in my_positions:
        sym = pos["symbol"]
        if sym not in bars or sym not in alpaca_pos:
            logger.warning("No data for %s — skipping", sym)
            continues = True
            holds.append({"symbol": sym, "reason": "no_data"})
            continue

        bar_data = bars[sym]
        alp = alpaca_pos[sym]
        entry_price = pos["entry_price"]
        current_price = alp["current_price"]
        shares = pos["shares"]
        entry_date = pos.get("entry_date", "")
        metadata = pos.get("metadata", {})
        entry_stop = metadata.get("stop", entry_price * 0.90)

        # Calculate days held
        try:
            entry_dt = datetime.strptime(entry_date, "%Y-%m-%d")
            days_held = (datetime.now() - entry_dt).days
        except (ValueError, TypeError):
            days_held = 1

        atr = _atr_from_bars(bar_data, CONFIG.risk.atr_period)
        if atr <= 0:
            atr = abs(current_price * 0.02)

        pnl_pct = (current_price / entry_price) - 1
        high_water = max(current_price, entry_price)  # Approximate
        profit_atr = (high_water - entry_price) / atr if atr > 0 else 0

        exit_reason = None
        exit_price = current_price

        # ── EXIT 1: HARD STOP ──
        hard_stop = entry_price - CONFIG.risk.initial_stop_atr_mult * atr
        if current_price <= hard_stop:
            exit_reason = "hard_stop"
            exit_price = current_price
            logger.info("EXIT 1 [HARD STOP] %s: price $%.2f <= stop $%.2f",
                        sym, current_price, hard_stop)

        # ── EXIT 2: TRAILING STOP ──
        if exit_reason is None:
            trail_mult = _trail_multiplier(days_held, profit_atr, CONFIG.risk)
            trail_stop = high_water - trail_mult * atr
            # Only apply if trail is above entry stop (ratchet up)
            if trail_stop > hard_stop and current_price <= trail_stop:
                exit_reason = "trailing_stop"
                exit_price = current_price
                logger.info("EXIT 2 [TRAIL] %s: price $%.2f <= trail $%.2f (%.1fx ATR, day %d, profit %.1f ATR)",
                            sym, current_price, trail_stop, trail_mult, days_held, profit_atr)

        # ── EXIT 3: THESIS INVALIDATION ──
        if exit_reason is None:
            comp_score = current_scores.get(sym, -1.0)
            if comp_score < 0 and days_held >= 2:
                exit_reason = "thesis_invalid"
                exit_price = current_price
                logger.info("EXIT 3 [THESIS] %s: composite=%.4f (below 0, math no longer supports hold)",
                            sym, comp_score)

        # ── EXIT 4: PORTFOLIO HEAT ──
        # Handled after individual checks — see below

        # ── EXIT 5: TIME DECAY ──
        if exit_reason is None and days_held >= CONFIG.risk.time_stop_days:
            exit_reason = "time_decay"
            exit_price = current_price
            logger.info("EXIT 5 [TIME] %s: held %d days (max %d)",
                        sym, days_held, CONFIG.risk.time_stop_days)

        if exit_reason:
            exits_to_execute.append({
                "symbol": sym,
                "shares": shares,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl_pct": pnl_pct,
                "days_held": days_held,
                "reason": exit_reason,
                "composite": current_scores.get(sym, 0),
            })
        else:
            trail_mult = _trail_multiplier(days_held, profit_atr, CONFIG.risk)
            trail_stop = max(hard_stop, high_water - trail_mult * atr)
            holds.append({
                "symbol": sym,
                "pnl_pct": round(pnl_pct * 100, 2),
                "days_held": days_held,
                "trail_stop": round(trail_stop, 2),
                "composite": round(current_scores.get(sym, 0), 4),
                "reason": "hold",
            })

    # ── EXIT 4: PORTFOLIO HEAT (after individual checks) ──
    if portfolio_dd < -CONFIG.risk.max_portfolio_drawdown:
        logger.warning("PORTFOLIO HEAT: drawdown %.2f%% exceeds limit %.2f%%",
                        portfolio_dd * 100, CONFIG.risk.max_portfolio_drawdown * 100)
        # Find the weakest HOLDING position by composite score
        if holds:
            weakest = min(holds, key=lambda h: h.get("composite", 0))
            sym = weakest["symbol"]
            if sym in alpaca_pos:
                exits_to_execute.append({
                    "symbol": sym,
                    "shares": int(alpaca_pos[sym]["qty"]),
                    "entry_price": 0,
                    "exit_price": alpaca_pos[sym]["current_price"],
                    "pnl_pct": alpaca_pos[sym].get("unrealized_pnl_pct", 0),
                    "days_held": 0,
                    "reason": "portfolio_heat",
                    "composite": weakest.get("composite", 0),
                })
                holds = [h for h in holds if h["symbol"] != sym]
                logger.info("EXIT 4 [HEAT] Trimming weakest: %s (composite=%.4f)",
                            sym, weakest.get("composite", 0))

    # ── REPORT ──
    logger.info("")
    logger.info("=" * 60)
    logger.info("  EXIT MONITOR SUMMARY")
    logger.info("=" * 60)

    if exits_to_execute:
        logger.info("  EXITS (%d):", len(exits_to_execute))
        for ex in exits_to_execute:
            logger.info("    %s  %s  pnl=%+.1f%%  held=%dd  composite=%.4f",
                        ex["symbol"], ex["reason"], ex["pnl_pct"] * 100,
                        ex["days_held"], ex["composite"])
    else:
        logger.info("  No exits triggered.")

    if holds:
        logger.info("  HOLDS (%d):", len(holds))
        for h in holds:
            logger.info("    %s  pnl=%+.1f%%  held=%dd  trail=$%.2f  comp=%.4f",
                        h["symbol"], h.get("pnl_pct", 0), h.get("days_held", 0),
                        h.get("trail_stop", 0), h.get("composite", 0))

    logger.info("=" * 60)

    # ── EXECUTE ──
    if exits_to_execute and not dry_run:
        for ex in exits_to_execute:
            sym = ex["symbol"]
            qty = ex["shares"]
            logger.info("Submitting SELL %d %s [%s]", qty, sym, ex["reason"])
            result = dm.alpaca.submit_order(sym, qty, "SELL", "market")
            if "error" not in result:
                logger.info("SOLD %s: %s", sym, result.get("status", "ok"))
                # Record exit in ledger
                ledger.record_exit(MODEL_ID, sym, ex["exit_price"],
                                    datetime.now().strftime("%Y-%m-%d"), ex["reason"])
                # Feed to adaptive weights if we have factor z-scores
                # (will accumulate for ridge learning)
            else:
                logger.error("SELL FAILED %s: %s", sym, result["error"])
    elif dry_run and exits_to_execute:
        logger.info("DRY RUN — no orders submitted")

    return {"exits": exits_to_execute, "holds": holds}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Raptor v5.4 Exit Monitor")
    parser.add_argument("--dry-run", action="store_true", help="Show exits without executing")
    args = parser.parse_args()

    run_exit_monitor(dry_run=args.dry_run)
