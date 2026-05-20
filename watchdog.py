"""
Raptor Watchdog v1.0 — Intraday Position Monitor
==================================================
Runs every 15 minutes during market hours.
Checks all open positions against real-time prices.
Executes exits when math triggers fire.

NOT a replacement for the daily scan. This catches
intraday moves that the morning scan misses:
  - Flash crash protection (hard stop)
  - Intraday trail ratchet
  - SPY circuit breaker (halt new entries if SPY drops 3%+)
  - Momentum break on intraday data

Usage:
  python watchdog.py              # Run once
  python watchdog.py --dry-run    # Show what would happen
  Start_Watchdog.bat              # Loop every 15 min

DO NOT DEPLOY until live trading validates the backtest.
"""

import logging
import os
import sys
from datetime import datetime, time
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
        logging.FileHandler(os.path.join(CONFIG.log.log_dir, f"watchdog_{datetime.now():%Y%m%d}.log")),
    ],
)
logger = logging.getLogger("raptor.watchdog")


def is_market_hours():
    now = datetime.now().time()
    return time(9, 30) <= now <= time(16, 0)


def run_watchdog(dry_run=False):
    logger.info("WATCHDOG %s", datetime.now().strftime("%H:%M:%S"))

    if not is_market_hours():
        logger.info("Market closed. Sleeping.")
        return

    dm = DataManager(CONFIG)
    f = Factors()
    positions = dm.alpaca.get_positions()
    account = dm.alpaca.get_account()
    equity = float(account["equity"])

    if not positions:
        logger.info("No positions. Nothing to watch.")
        return

    logger.info("Equity: $%.2f | Positions: %d", equity, len(positions))

    # Load ledger for persisted high_water (HOLE 11 fix)
    try:
        from ledger import Ledger
        _ledger = Ledger()
    except Exception:
        _ledger = None

    # Portfolio drawdown — needed for kill switch Tier 2
    total_pnl    = sum(p.get("unrealized_pnl", 0) for p in positions)
    portfolio_dd = total_pnl / equity if equity > 0 else 0

    # ══════════════════════════════════════════════════════════════════════════
    # KILL SWITCH — Watchdog instance (HOLE 12 fix)
    # Fires between scheduled exit_monitor runs (every 15 min during market hours).
    # Same two-tier logic as exit_monitor — crash can happen at any time of day.
    # ══════════════════════════════════════════════════════════════════════════
    try:
        _kill_reason = None
        _kill_tier   = None

        # Tier 1: SPY intraday ≤ -5% AND VIX ≥ 35
        try:
            import yfinance as _yf
            _spy_hist = _yf.Ticker("SPY").history(period="3d", interval="1d")
            _spy_move = 0.0
            if len(_spy_hist) >= 2:
                _spy_move = float(_spy_hist["Close"].iloc[-1] / _spy_hist["Close"].iloc[-2]) - 1.0
            _vix_live = 0.0
            try:
                _vix_hist = _yf.Ticker("^VIX").history(period="1d", interval="1m")
                if not _vix_hist.empty:
                    _vix_live = float(_vix_hist["Close"].iloc[-1])
            except Exception:
                pass
            logger.info("KILL CHECK: SPY=%.2f%%  VIX=%.1f", _spy_move * 100, _vix_live)
            if _spy_move <= -0.05 and _vix_live >= 35:
                _kill_reason = f"TIER 1 CRISIS: SPY {_spy_move*100:.1f}% AND VIX {_vix_live:.1f}"
                _kill_tier = 1
        except Exception as _ke:
            logger.warning("Kill switch Tier 1 check failed (%s)", _ke)

        # Tier 2: Portfolio drawdown ≤ -15%
        if _kill_tier is None and portfolio_dd <= -0.15:
            _kill_reason = f"TIER 2 DRAWDOWN: portfolio_dd={portfolio_dd*100:.1f}%"
            _kill_tier = 2

        if _kill_tier is not None:
            logger.critical("=" * 60)
            logger.critical("  ██ WATCHDOG KILL SWITCH — %s", _kill_reason)
            logger.critical("  ██ LIQUIDATING ALL %d POSITIONS AT MARKET", len(positions))
            logger.critical("=" * 60)
            _k_sold = 0
            _k_fail = 0
            for _kpos in positions:
                _ksym   = _kpos["symbol"]
                _kqty   = _kpos["qty"]
                _kprice = _kpos["current_price"]
                _kpnl   = _kpos.get("unrealized_pnl_pct", 0)
                logger.critical("  KILL SELL %s: %s shares @ ~$%.2f  pnl=%.1f%%",
                               _ksym, _kqty, _kprice, _kpnl * 100)
                if not dry_run:
                    _kr = dm.alpaca.submit_order(
                        _ksym, _kqty, "SELL", "market",
                        client_order_id=f"wdog_kill_t{_kill_tier}_{_ksym}_{datetime.now():%Y%m%d%H%M%S}"
                    )
                    if "error" not in _kr:
                        _k_sold += 1
                        if _ledger:
                            try:
                                _ledger.record_exit("v5.4", _ksym, float(_kprice),
                                                   datetime.now().strftime("%Y-%m-%d"),
                                                   f"watchdog_kill_tier{_kill_tier}")
                            except Exception:
                                pass
                        # Write cooldown
                        try:
                            import json as _kcj
                            from pathlib import Path as _kcP
                            from datetime import date as _kcd
                            _kcp = _kcP("cooldown_log.json")
                            _kcc = {}
                            if _kcp.exists():
                                try: _kcc = _kcj.loads(_kcp.read_text())
                                except Exception: pass
                            _kcc[_ksym] = str(_kcd.today())
                            _kcp.write_text(_kcj.dumps(_kcc, indent=2))
                        except Exception:
                            pass
                    else:
                        _k_fail += 1
                        logger.error("  KILL SELL FAILED %s: %s", _ksym, _kr["error"])
                else:
                    logger.critical("  DRY RUN — would sell %s %s shares", _ksym, _kqty)
                    _k_sold += 1
            logger.critical("  KILL COMPLETE: %d sold, %d failed", _k_sold, _k_fail)
            logger.critical("=" * 60)
            # Write kill switch state for market_agent to read (HOLE 17)
            try:
                import json as _ksj
                from pathlib import Path as _ksP
                _ksP("kill_switch_state.json").write_text(_ksj.dumps({
                    "active": True, "tier": _kill_tier, "reason": _kill_reason,
                    "triggered_at": datetime.now().isoformat(), "cleared": False,
                }, indent=2))
            except Exception:
                pass
            return  # Skip all normal watchdog logic

    except Exception as _kse:
        logger.error("Kill switch failed (%s) — proceeding with normal logic", _kse)
    # ══════════════════════════════════════════════════════════════════════════

    # SPY circuit breaker — logs warning, doesn't exit positions
    spy_bars = None
    spy_change = 0.0
    try:
        spy_data = dm.alpaca.get_daily_bars(["SPY"], lookback_days=5)
        if "SPY" in spy_data:
            spy_bars = spy_data["SPY"]
            spy_today = float(spy_bars["close"].iloc[-1])
            spy_prev  = float(spy_bars["close"].iloc[-2]) if len(spy_bars) >= 2 else spy_today
            spy_change = (spy_today / spy_prev) - 1.0
            if spy_change < -0.03:
                logger.warning("SPY CIRCUIT BREAKER: %.1f%% drop. No new entries.", spy_change * 100)
    except Exception:
        spy_change = 0.0

    exits = []
    holds = []

    for pos in positions:
        sym     = pos["symbol"]
        entry   = float(pos["avg_entry"])
        price   = float(pos["current_price"])
        qty     = pos["qty"]
        pnl_pct = float(pos.get("unrealized_pnl_pct", 0))

        # Get daily bars for ATR calculation
        try:
            bars_data = dm.alpaca.get_daily_bars([sym], lookback_days=30)
            if sym not in bars_data or len(bars_data[sym]) < 15:
                holds.append({"symbol": sym, "reason": "no_data"})
                continue
            bars = bars_data[sym]
        except Exception:
            holds.append({"symbol": sym, "reason": "fetch_error"})
            continue

        # HOLE 16 FIX: Daily ATR understates intraday volatility.
        # Scale daily ATR by intraday volatility factor derived from
        # today's range vs average daily range over last 5 days.
        # On a crash day: today's range = 3× normal → ATR scalar = 1.5 (capped)
        # On a normal day: scalar ≈ 1.0 → no change
        daily_atr = f.atr(bars, CONFIG.risk.atr_period)
        if daily_atr <= 0:
            daily_atr = abs(price * 0.02)
        try:
            avg_range_5d = float((bars["high"] - bars["low"]).iloc[-5:].mean())
            today_range  = float(bars["high"].iloc[-1] - bars["low"].iloc[-1])
            intraday_scalar = float(np.clip(today_range / (avg_range_5d + 1e-9), 0.5, 2.0))
        except Exception:
            intraday_scalar = 1.0
        atr = daily_atr * intraday_scalar

        reason = None

        # HARD STOP — uses scaled ATR
        hard_stop = entry - CONFIG.risk.initial_stop_atr_mult * atr
        if price <= hard_stop:
            reason = "hard_stop"
            logger.info("EXIT [HARD STOP] %s $%.2f <= $%.2f (ATR_scalar=%.2f)",
                       sym, price, hard_stop, intraday_scalar)

        # TRAIL — HOLE 11 FIX: load persisted high_water from ledger
        if reason is None:
            if _ledger is not None:
                high_water = _ledger.update_high_water("v5.4", sym, price)
                high_water = max(high_water, entry)
            else:
                high_water = max(price, entry)

            profit_atr = (high_water - entry) / atr if atr > 0 else 0
            # Watchdog uses trail_mid_atr (2.0×) — wider than exit_monitor's
            # time-decaying trail. Intraday noise is higher; trail must breathe.
            # trail_mult does not apply signal-quality modifier here —
            # composite/health not available without running full signal engine.
            trail = high_water - CONFIG.risk.trail_mid_atr * atr
            if trail > hard_stop and price <= trail:
                reason = "trail_profit" if price > entry else "trail_loss"
                logger.info("EXIT [TRAIL] %s $%.2f <= $%.2f hw=$%.2f ATR_scalar=%.2f",
                           sym, price, trail, high_water, intraday_scalar)

        # MOMENTUM BREAK — 2 consecutive closes below 8-EMA while profitable
        if reason is None and pnl_pct > 0.01:
            ema8 = bars["close"].ewm(span=8, adjust=False).mean()
            if (bars["close"].iloc[-1] < ema8.iloc[-1] and
                    bars["close"].iloc[-2] < ema8.iloc[-2]):
                reason = "momentum_break"
                logger.info("EXIT [MOMENTUM BREAK] %s $%.2f below 8-EMA", sym, price)

        if reason:
            exits.append({
                "symbol": sym, "qty": qty, "price": price,
                "entry": entry, "pnl_pct": pnl_pct, "reason": reason,
            })
        else:
            holds.append({
                "symbol": sym, "pnl_pct": round(pnl_pct * 100, 1),
                "price": price,
            })

    # Report
    logger.info("Exits: %d | Holds: %d", len(exits), len(holds))
    for h in holds:
        if h.get("reason") != "no_data":
            logger.info("  HOLD %s $%.2f pnl=%+.1f%%",
                       h.get("symbol", "?"), h.get("price", 0), h.get("pnl_pct", 0))

    # Execute
    if exits and not dry_run:
        for ex in exits:
            result = dm.alpaca.submit_order(
                ex["symbol"], ex["qty"], "SELL", "market",
                client_order_id=f"{ex['reason']}_{ex['symbol']}_{datetime.now():%Y%m%d%H%M%S}"
            )
            if "error" not in result:
                logger.info("  SOLD %s [%s]", ex["symbol"], ex["reason"])
                if _ledger:
                    try:
                        _ledger.record_exit("v5.4", ex["symbol"], float(ex["price"]),
                                           datetime.now().strftime("%Y-%m-%d"), ex["reason"])
                    except Exception:
                        pass
            else:
                logger.error("  FAILED %s: %s", ex["symbol"], result["error"])
    elif dry_run and exits:
        logger.info("DRY RUN — no orders submitted")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_watchdog(dry_run=args.dry_run)
