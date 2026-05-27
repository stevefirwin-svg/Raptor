"""
Raptor Watchdog v1.1 — Intraday Position Monitor
==================================================
Runs every 15 minutes during market hours.
Checks all open positions against LIVE prices from Alpaca positions endpoint.
Executes exits when math triggers fire intraday.

NOT a replacement for the daily scan or exit_monitor. This catches
intraday moves that the morning scan misses:
  - Flash crash protection (hard stop fires intraday)
  - SPY circuit breaker (live SPY price, not yesterday's close)
  - Intraday trail ratchet against live high-water from hold_health.json

What this does NOT do (leave to exit_monitor.py):
  - Thesis invalidation (requires composite re-score — daily only)
  - Momentum break (8-day EMA — multi-day signal, not intraday)
  - Math trims (requires hold_health.json 8-layer scores — daily only)
  - Time decay (multi-day signal)

Bugs fixed v1.1 (2026-05-22):
  - SPY circuit breaker now uses live Alpaca position price, not daily bars
  - High water mark now reads hold_health.json (accumulated) not max(price, entry)
  - Momentum break removed — was checking 8-day EMA on daily bars (not intraday)
  - Hard stop ATR kept from daily bars (correct — intraday ATR is too noisy)
  - ATR fallback improved: reads hold_health.json atr field before 2% proxy

Usage:
  python watchdog.py              # Run once
  python watchdog.py --dry-run    # Show what would happen
  Start_Watchdog.bat              # Loop every 15 min
"""

import json
import logging
import os
import sys
from datetime import datetime, time
from pathlib import Path

import numpy as np
import pandas as pd

from config import CONFIG
from data_feeds import DataManager
from signals import Factors

os.makedirs(CONFIG.log.log_dir, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, CONFIG.log.log_level),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(CONFIG.log.log_dir, f"watchdog_{datetime.now():%Y%m%d}.log")
        ),
    ],
)
logger = logging.getLogger("raptor.watchdog")


def is_market_hours() -> bool:
    now = datetime.now().time()
    return time(9, 30) <= now <= time(16, 0)


def _load_hold_health() -> dict:
    """Load hold_health.json — written by hold_monitor, contains ATR and high_water per symbol."""
    try:
        p = Path("hold_health.json")
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {}


def _get_spy_live_price(dm) -> float:
    """
    Get live SPY price from Alpaca.
    Uses get_bars with lookback_days=2 to get today's latest bar.
    Falls back to 0.0 on error (circuit breaker disabled on error = safe).

    Note: Alpaca's positions endpoint only returns held symbols, so SPY
    requires a separate bars call when SPY is not in the portfolio.
    Using lookback_days=2 returns today's partial bar with latest close.
    """
    try:
        spy_data = dm.get_bars(["SPY"], lookback_days=2)
        if "SPY" in spy_data and len(spy_data["SPY"]) >= 1:
            return float(spy_data["SPY"]["close"].iloc[-1])
    except Exception:
        pass
    return 0.0


def _get_spy_prev_close(dm) -> float:
    """Get SPY previous day close for intraday change calculation."""
    try:
        spy_data = dm.get_bars(["SPY"], lookback_days=5)
        if "SPY" in spy_data and len(spy_data["SPY"]) >= 2:
            return float(spy_data["SPY"]["close"].iloc[-2])
    except Exception:
        pass
    return 0.0


def run_watchdog(dry_run: bool = False):
    logger.info("WATCHDOG v1.1 %s", datetime.now().strftime("%H:%M:%S"))

    if not is_market_hours():
        logger.info("Market closed. Sleeping.")
        return

    dm = DataManager(CONFIG)
    f  = Factors()

    positions = dm.alpaca.get_positions()
    account   = dm.alpaca.get_account()
    equity    = float(account.get("equity", 0))

    if not positions:
        logger.info("No positions. Nothing to watch.")
        return

    logger.info("Equity: $%.2f | Positions: %d", equity, len(positions))

    # ── SPY circuit breaker — live price ─────────────────────────────────────
    # Previous: spy_change compared yesterday vs day before — never caught intraday crash.
    # Fixed: spy_live vs spy_prev_close = true intraday change.
    spy_live  = _get_spy_live_price(dm)
    spy_prev  = _get_spy_prev_close(dm)
    spy_change = (spy_live / spy_prev - 1) if spy_prev > 0 and spy_live > 0 else 0.0

    circuit_breaker_active = spy_change < -0.03
    if circuit_breaker_active:
        logger.warning(
            "SPY CIRCUIT BREAKER: %.2f%% intraday drop (live=$%.2f vs prev=$%.2f) — "
            "exits only, no new entries",
            spy_change * 100, spy_live, spy_prev
        )
    else:
        logger.info("SPY: %.2f%% intraday (live=$%.2f)", spy_change * 100, spy_live)

    # ── Load hold_health for ATR ─────────────────────────────────────────────
    hold_health = _load_hold_health()

    # ── Load ledger for high_water and stop (authoritative source) ────────────
    # hold_health.json snapshot does not contain high_water — that field lives
    # in ledger metadata["high_water"], updated by exit_monitor each cycle.
    try:
        from ledger import Ledger as _WLedger
        _wledger = _WLedger()
        _wledger_map = {v["symbol"]: v for v in _wledger.data["positions"].values()}
    except Exception:
        _wledger = None
        _wledger_map = {}

    exits = []
    holds = []

    for pos in positions:
        sym      = pos["symbol"]
        entry    = float(pos.get("avg_entry", 0))
        price    = float(pos.get("current_price", 0))   # live price from Alpaca
        qty      = float(pos.get("qty", 0))
        pnl_pct  = float(pos.get("unrealized_pnl_pct", 0))

        if entry <= 0 or price <= 0 or qty <= 0:
            continue

        # ── ATR: daily bars (correct — intraday ATR is too noisy for stops) ──
        atr = 0.0
        try:
            bars_data = dm.get_bars([sym], lookback_days=30)
            if sym in bars_data and len(bars_data[sym]) >= 15:
                atr = f.atr(bars_data[sym], CONFIG.risk.atr_period)
        except Exception:
            pass

        # ATR fallback chain:
        # 1. hold_health.json atr field (updated each monitor run — most recent)
        # 2. 2% of current price (last resort proxy)
        if atr <= 0:
            health_rec = hold_health.get(sym, {})
            atr = float(health_rec.get("atr", 0) or 0)
        if atr <= 0:
            atr = price * 0.02
            logger.debug("%s: ATR fallback to 2%% proxy ($%.4f)", sym, atr)

        # ── High water mark: from ledger metadata (authoritative) ────────────
        # hold_health.json snapshot does NOT contain high_water — it lives in
        # ledger metadata["high_water"], updated by exit_monitor each 30-min cycle.
        # Reading from hold_health was always returning 0 (field missing), causing
        # watchdog to trail from max(0, entry, price) = effectively entry every run.
        _wentry = _wledger_map.get(sym, {})
        _wmeta  = _wentry.get("metadata", {})
        _stored_hw = _wmeta.get("high_water")
        stored_high_water = float(_stored_hw) if _stored_hw else 0.0
        # Take max of stored, entry, and current (current may be new intraday high)
        high_water = max(stored_high_water, entry, price)

        # Persist updated high_water back to ledger if price made a new high
        if _wledger and high_water > stored_high_water and _wentry:
            try:
                _wledger.data["positions"][f"v5.4:{sym}"]["metadata"]["high_water"] = round(high_water, 4)
                _wledger._save()
            except Exception as _hwe:
                logger.debug("watchdog high_water persist failed for %s: %s", sym, _hwe)

        profit_atr = (high_water - entry) / atr if atr > 0 else 0.0

        reason = None

        # ── HARD STOP — vol-regime aware (mirrors exit_monitor GAP 3) ────────
        # Read vol_pctile from hold_health if available, else use 0.5 (neutral)
        vol_pctile = float(health_rec.get("vol_pctile", 0.5) or 0.5)
        if vol_pctile < 0.25:
            stop_mult = 2.5
        elif vol_pctile > 0.75:
            stop_mult = 3.5
        else:
            stop_mult = CONFIG.risk.initial_stop_atr_mult

        hard_stop = entry - stop_mult * atr
        if price <= hard_stop:
            reason = "hard_stop"
            logger.info(
                "EXIT [HARD STOP] %s $%.2f <= $%.2f (%.1f× ATR, vol_pctile=%.2f)",
                sym, price, hard_stop, stop_mult, vol_pctile
            )

        # ── INTRADAY TRAIL ────────────────────────────────────────────────────
        # Use mid-range trail (trail_mid_atr) — tighter than daily trail to catch
        # intraday reversals without triggering on normal noise.
        # We do NOT apply the GAP 1 signal-quality modifier here because composite
        # scores are daily — applying them intraday would be stale.
        if reason is None:
            trail_mult = CONFIG.risk.trail_mid_atr
            if profit_atr >= 4.0:
                trail_mult = min(trail_mult, 1.0)
            elif profit_atr >= 2.0:
                trail_mult = min(trail_mult, 1.5)
            elif profit_atr >= 1.0:
                trail_mult = min(trail_mult, 2.0)

            trail_price = high_water - trail_mult * atr
            trail_price = max(trail_price, hard_stop)  # trail never below hard stop

            if price <= trail_price and trail_price > hard_stop:
                reason = "trail_profit" if price > entry else "trail_loss"
                logger.info(
                    "EXIT [TRAIL] %s $%.2f <= $%.2f (trail=%.2f× ATR, hw=$%.2f)",
                    sym, price, trail_price, trail_mult, high_water
                )

        # NOTE: Momentum break removed from watchdog v1.1
        # It was checking 8-day EMA on daily bars — not intraday.
        # Multi-day signals belong in exit_monitor.py, not the 15-min watchdog.

        if reason:
            exits.append({
                "symbol": sym, "qty": qty, "price": price,
                "entry": entry, "pnl_pct": pnl_pct, "reason": reason,
                "atr": round(atr, 4), "high_water": round(high_water, 4),
            })
        else:
            holds.append({
                "symbol": sym, "price": price,
                "pnl_pct": round(pnl_pct * 100, 2),
                "stop": round(hard_stop, 2),
                "stop_dist_pct": round((price - hard_stop) / price * 100, 1),
            })

    # ── Report ────────────────────────────────────────────────────────────────
    logger.info("WATCHDOG RESULT: %d exits | %d holds", len(exits), len(holds))
    for h in holds:
        logger.info(
            "  HOLD %-6s $%.2f  pnl=%+.2f%%  stop=$%.2f (dist=%.1f%%)",
            h["symbol"], h["price"], h["pnl_pct"], h["stop"], h["stop_dist_pct"]
        )

    # ── Execute ───────────────────────────────────────────────────────────────
    if exits and not dry_run:
        for ex in exits:
            client_id = f"{ex['reason']}_{ex['symbol']}_{datetime.now():%Y%m%d%H%M%S}_watchdog"
            result = dm.alpaca.submit_order(
                ex["symbol"], int(ex["qty"]), "SELL", "market",
                client_order_id=client_id
            )
            if "error" not in result:
                logger.info(
                    "  SOLD %-6s %d shares @ $%.2f [%s]",
                    ex["symbol"], int(ex["qty"]), ex["price"], ex["reason"]
                )
                # Write to ledger — watchdog exits are full closes (hard_stop or trail)
                # Previously: ledger was never updated, position stayed OPEN after watchdog sold
                try:
                    if _wledger:
                        _wledger.record_exit(
                            "v5.4", ex["symbol"],
                            float(ex["price"]),
                            datetime.now().strftime("%Y-%m-%d"),
                            ex["reason"]
                        )
                except Exception as _le:
                    logger.warning("Ledger record_exit failed for %s: %s", ex["symbol"], _le)
                # Add to cooldown if hard stop
                if ex["reason"] == "hard_stop":
                    try:
                        from main import _record_stopout_cooldown
                        _record_stopout_cooldown(ex["symbol"])
                    except Exception as _ce:
                        logger.debug("Cooldown record failed: %s", _ce)
            else:
                logger.error("  FAILED %-6s: %s", ex["symbol"], result["error"])

    # Tag closed trades in outcome_log via outcome_tracker
    if exits and not dry_run:
        try:
            import outcome_tracker as _ot
            n = _ot.run_tracker(verbose=False)
            if n > 0:
                logger.info("[OutcomeTracker] Tagged %d watchdog exit(s)", n)
        except Exception as _ote:
            logger.debug("OutcomeTracker failed: %s", _ote)
    elif dry_run and exits:
        for ex in exits:
            logger.info(
                "  DRY RUN — would sell %d %s @ $%.2f [%s]",
                int(ex["qty"]), ex["symbol"], ex["price"], ex["reason"]
            )
    elif not exits:
        logger.info("  No exits triggered this run.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Raptor Watchdog v1.1")
    parser.add_argument("--dry-run", action="store_true", help="Show exits without submitting")
    args = parser.parse_args()
    run_watchdog(dry_run=args.dry_run)
