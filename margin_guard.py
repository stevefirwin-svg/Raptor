"""
margin_guard.py — Capital Utilization and Margin Safety Check
=============================================================
Provides a single function check_margin_safety() used by main.py
before placing any new entry orders.

Also runnable standalone for a quick account health snapshot.

Rules:
  - Cash negative (on margin): WARN, allow entries only if utilization < 85%
  - Utilization > 90%: BLOCK all new entries
  - Utilization > 85%: REDUCE — scale max new positions to 1
  - Utilization <= 85%: ALLOW normal operation

Usage:
  from margin_guard import check_margin_safety
  allowed, max_new, reason = check_margin_safety(dm)
  if not allowed:
      logger.warning(reason)
      return

  python margin_guard.py   # standalone snapshot
"""

import logging
from typing import Tuple

logger = logging.getLogger("raptor.margin_guard")

# Thresholds
BLOCK_THRESHOLD  = 0.90   # >90% utilization: no new entries
REDUCE_THRESHOLD = 0.85   # >85%: max 1 new position
WARN_THRESHOLD   = 0.75   # >75%: log warning


def check_margin_safety(dm) -> Tuple[bool, int, str]:
    """
    Check account margin and capital utilization.

    Returns:
        allowed  (bool)  — True if new entries are permitted
        max_new  (int)   — max new positions allowed this scan (0 = blocked)
        reason   (str)   — explanation for log

    Usage in main.py (add near top of run_daily_scan, after account fetch):
        allowed, max_new, reason = check_margin_safety(dm)
        if not allowed:
            logger.warning("MARGIN GUARD: %s", reason)
            return
        if max_new < CONFIG.execution.max_orders_per_scan:
            logger.warning("MARGIN GUARD: capping new entries at %d — %s", max_new, reason)
    """
    try:
        account   = dm.alpaca.get_account()
        positions = dm.alpaca.get_positions()

        equity    = float(account.get("equity", 0))
        cash      = float(account.get("cash", 0))
        portfolio_value = float(account.get("portfolio_value", equity))

        if equity <= 0:
            return False, 0, "equity is zero or negative — blocking all entries"

        # market_value not returned by this Alpaca client — compute from qty * current_price
        total_mv  = sum(abs(float(p.get("qty", 0))) * float(p.get("current_price", 0)) for p in positions)
        util      = total_mv / equity
        on_margin = cash < 0
        margin_pct = abs(cash) / equity if on_margin else 0.0

        status_lines = [
            f"equity=${equity:,.0f}  cash=${cash:,.0f}  positions={len(positions)}",
            f"market_value=${total_mv:,.0f}  utilization={util:.1%}  margin={'YES' if on_margin else 'NO'}"
        ]
        if on_margin:
            status_lines.append(f"margin_usage={margin_pct:.1%} of equity")

        for line in status_lines:
            logger.info("MARGIN GUARD: %s", line)

        if util > BLOCK_THRESHOLD:
            return False, 0, f"utilization {util:.1%} > {BLOCK_THRESHOLD:.0%} — blocking new entries"

        if util > REDUCE_THRESHOLD:
            return True, 1, f"utilization {util:.1%} > {REDUCE_THRESHOLD:.0%} — capping at 1 new entry"

        if util > WARN_THRESHOLD or on_margin:
            msg = f"utilization {util:.1%}"
            if on_margin:
                msg += f", on margin (${abs(cash):,.0f})"
            logger.warning("MARGIN GUARD: WARNING — %s", msg)

        return True, 99, f"utilization {util:.1%} — normal operation"

    except Exception as e:
        logger.warning("MARGIN GUARD: check failed (%s) — allowing entries (fail-safe)", e)
        return True, 99, f"guard error: {e}"


def print_snapshot():
    """Standalone account health snapshot."""
    from config import CONFIG
    from data_feeds import DataManager

    dm        = DataManager(CONFIG)
    account   = dm.alpaca.get_account()
    positions = dm.alpaca.get_positions()

    equity   = float(account.get("equity", 0))
    cash     = float(account.get("cash", 0))
    bp       = float(account.get("buying_power", 0))
    total_mv = sum(abs(float(p.get("qty", 0))) * float(p.get("current_price", 0)) for p in positions)
    util     = total_mv / equity if equity > 0 else 0
    on_margin = cash < 0

    print(f"\n{'='*55}")
    print(f"  RAPTOR CAPITAL UTILIZATION SNAPSHOT")
    print(f"{'='*55}")
    print(f"  Equity:          ${equity:>12,.2f}")
    print(f"  Cash:            ${cash:>12,.2f}  {'⚠ ON MARGIN' if on_margin else '✓'}")
    print(f"  Buying Power:    ${bp:>12,.2f}")
    print(f"  Market Value:    ${total_mv:>12,.2f}")
    print(f"  Utilization:     {util:>11.1%}  ", end="")

    if util > BLOCK_THRESHOLD:
        print("🔴 BLOCKED — no new entries")
    elif util > REDUCE_THRESHOLD:
        print("🟠 REDUCED — max 1 new entry")
    elif util > WARN_THRESHOLD:
        print("🟡 WARNING — elevated")
    else:
        print("🟢 NORMAL")

    if on_margin:
        margin_pct = abs(cash) / equity
        print(f"\n  ⚠ Margin usage:  ${abs(cash):>12,.2f}  ({margin_pct:.1%} of equity)")
        print(f"  Recommendation:  Let positions exit naturally before adding new ones.")
        print(f"                   Consider reducing max_positions in config.")

    print(f"\n  Positions ({len(positions)}):")
    sorted_pos = sorted(positions, key=lambda p: float(p.get("qty", 0)) * float(p.get("current_price", 0)), reverse=True)
    for p in sorted_pos:
        mv   = float(p.get("qty", 0)) * float(p.get("current_price", 0))
        pnl  = float(p.get("unrealized_pnl_pct", 0)) * 100
        wt   = mv / total_mv * 100 if total_mv > 0 else 0
        print(f"    {p['symbol']:6s}  ${mv:>9,.0f}  {wt:>5.1f}% of port  pnl={pnl:+.1f}%")

    print(f"{'='*55}\n")

    allowed, max_new, reason = check_margin_safety(dm)
    print(f"  Entry verdict: {'ALLOWED' if allowed else 'BLOCKED'} — {reason}")
    print(f"  Max new positions this scan: {max_new if max_new < 99 else 'unlimited'}\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    print_snapshot()
