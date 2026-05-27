"""
margin_guard.py — Capital Utilization and Margin Safety Check
=============================================================
Provides a single function check_margin_safety() used by main.py
before placing any new entry orders.

Also runnable standalone for a quick account health snapshot.

Rules:
  - Cash negative (on margin): REDUCE — cap new positions at 1
  - Utilization > 90%: BLOCK all new entries
  - Utilization > 85%: REDUCE — scale max new positions to 1
  - Utilization <= 85%: ALLOW normal operation

Fail-safe: API errors return BLOCKED (fail closed, not open).
Any exception during the check blocks entries to protect capital.

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

# Thresholds derived from position limits:
# max_positions=10 × max_position_pct=0.08 = 80% theoretical max utilization
# at full deployment. Thresholds set relative to that ceiling:
#   WARN at 0.75: 5% below theoretical max — first signal of approaching limits
#   REDUCE at 0.85: 5% above theoretical max — one oversized position or margin creep
#   BLOCK at 0.90: 10% above theoretical max — clear breach, fail closed
# TODO:DERIVE — WARN_THRESHOLD 0.75 should be calibrated from empirical margin
# call distance analysis once sufficient equity curve data is available.
BLOCK_THRESHOLD  = 0.90
REDUCE_THRESHOLD = 0.85
WARN_THRESHOLD   = 0.75

# Sentinel for unlimited entries — large enough to never cap normal operation
# but explicit rather than magic number 99.
_UNLIMITED = 10_000


def check_margin_safety(dm) -> Tuple[bool, int, str]:
    """
    Check account margin and capital utilization.

    Returns:
        allowed  (bool)  — True if new entries are permitted
        max_new  (int)   — max new positions allowed this scan (0 = blocked)
        reason   (str)   — explanation for log

    Fail-safe: returns BLOCKED on any API error (fail closed, not open).
    Previously returned (True, 99, ...) on error — silently bypassed all checks.

    Usage in main.py:
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

        equity = float(account.get("equity", 0))
        cash   = float(account.get("cash", 0))
        # portfolio_value intentionally not used — equity (net liquidation value)
        # is the correct denominator for utilization. portfolio_value == equity
        # in paper trading; keeping equity for consistency with live semantics.

        if equity <= 0:
            return False, 0, "equity is zero or negative — blocking all entries"

        # market_value not returned by this Alpaca client — compute from qty * current_price
        total_mv  = sum(
            abs(float(p.get("qty", 0))) * float(p.get("current_price", 0))
            for p in positions
        )
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

        # ── Hard block ────────────────────────────────────────────────────────
        if util > BLOCK_THRESHOLD:
            return False, 0, f"utilization {util:.1%} > {BLOCK_THRESHOLD:.0%} — blocking new entries"

        # ── Reduce: over threshold OR on margin ───────────────────────────────
        # On-margin means Alpaca is lending capital — adding positions increases
        # leverage. Cap at 1 new position the same as the REDUCE threshold.
        if util > REDUCE_THRESHOLD:
            return True, 1, f"utilization {util:.1%} > {REDUCE_THRESHOLD:.0%} — capping at 1 new entry"

        if on_margin:
            logger.warning(
                "MARGIN GUARD: ON MARGIN ($%s, %.1f%% of equity) — capping at 1 new entry",
                f"{abs(cash):,.0f}", margin_pct * 100
            )
            return True, 1, f"on margin (${abs(cash):,.0f}, {margin_pct:.1%}) — capping at 1 new entry"

        # ── Warning only ──────────────────────────────────────────────────────
        if util > WARN_THRESHOLD:
            logger.warning("MARGIN GUARD: WARNING — utilization %s", f"{util:.1%}")

        return True, _UNLIMITED, f"utilization {util:.1%} — normal operation"

    except Exception as e:
        # Fail CLOSED — if we can't verify capital state, don't risk new entries.
        # Previous behavior (fail open, return True) was a silent capital risk.
        logger.error("MARGIN GUARD: check failed (%s) — BLOCKING entries (fail closed)", e)
        return False, 0, f"guard error (fail closed): {e}"


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
    print(f"  Max new positions this scan: {max_new if max_new < _UNLIMITED else 'unlimited'}\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    print_snapshot()
