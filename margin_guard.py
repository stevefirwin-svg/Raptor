"""
margin_guard.py — Capital Utilization and Margin Safety Check
=============================================================
Provides a single function check_margin_safety() used by main.py
before placing any new entry orders.

Also runnable standalone for a quick account health snapshot.

Rules:
  - Market value >= equity_cap (only if a finite cap is set): BLOCK (hard cap)
  - Cash negative (on margin) AND margin disabled: BLOCK all new entries
  - Cash negative (on margin) AND margin allowed: REDUCE — cap at 1
  - Utilization (vs base = equity, or min(equity,cap) if capped) > 90%: BLOCK
  - Utilization > 85%: REDUCE — scale max new positions to 1
  - Utilization <= 85%: ALLOW normal operation
  Default config: equity_cap=None (compound) + allow_margin=False (cash only).

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
        from config import CONFIG
        account   = dm.alpaca.get_account()
        positions = dm.alpaca.get_positions()

        equity = float(account.get("equity", 0))
        cash   = float(account.get("cash", 0))

        cap          = getattr(CONFIG.risk, "equity_cap", None)
        allow_margin = bool(getattr(CONFIG.risk, "allow_margin", True))
        # Deployable base for utilization. Uncapped (cap=None) → compound: measure
        # against full equity. Capped → measure against min(equity, cap).
        if cap is None:
            base = equity
        else:
            cap = float(cap)
            base = min(equity, cap) if cap > 0 else equity

        if equity <= 0:
            return False, 0, "equity is zero or negative — blocking all entries"

        # market_value not returned by this Alpaca client — compute from qty * current_price
        total_mv  = sum(
            abs(float(p.get("qty", 0))) * float(p.get("current_price", 0))
            for p in positions
        )
        util      = total_mv / base if base > 0 else 1.0
        on_margin = cash < 0
        margin_pct = abs(cash) / equity if on_margin else 0.0

        cap_str = "none (compound)" if cap is None else f"${cap:,.0f}"
        status_lines = [
            f"equity=${equity:,.0f}  cap={cap_str}  cash=${cash:,.0f}  positions={len(positions)}",
            f"market_value=${total_mv:,.0f}  utilization={util:.1%} of base  margin={'YES' if on_margin else 'NO'}"
        ]
        if on_margin:
            status_lines.append(f"margin_usage={margin_pct:.1%} of equity")

        for line in status_lines:
            logger.info("MARGIN GUARD: %s", line)

        # ── Hard equity cap (only when a finite cap is configured) ────────────
        if cap is not None and total_mv >= cap:
            return False, 0, f"market value ${total_mv:,.0f} >= equity cap ${cap:,.0f} — blocking new entries"

        # ── No margin ─────────────────────────────────────────────────────────
        # When margin is disallowed, negative cash means Alpaca is already lending.
        # Hard block, not a reduce — no new entries until cash >= 0.
        if on_margin and not allow_margin:
            return False, 0, f"on margin (${abs(cash):,.0f}, {margin_pct:.1%} of equity) and margin disabled — blocking new entries"

        # ── Hard block (utilization) ──────────────────────────────────────────
        if util > BLOCK_THRESHOLD:
            return False, 0, f"utilization {util:.1%} of base > {BLOCK_THRESHOLD:.0%} — blocking new entries"

        # ── Reduce: over threshold OR on margin (margin allowed case) ─────────
        if util > REDUCE_THRESHOLD:
            return True, 1, f"utilization {util:.1%} of base > {REDUCE_THRESHOLD:.0%} — capping at 1 new entry"

        if on_margin:
            logger.warning(
                "MARGIN GUARD: ON MARGIN ($%s, %.1f%% of equity) — capping at 1 new entry",
                f"{abs(cash):,.0f}", margin_pct * 100
            )
            return True, 1, f"on margin (${abs(cash):,.0f}, {margin_pct:.1%}) — capping at 1 new entry"

        # ── Warning only ──────────────────────────────────────────────────────
        if util > WARN_THRESHOLD:
            logger.warning("MARGIN GUARD: WARNING — utilization %s of base", f"{util:.1%}")

        return True, _UNLIMITED, f"utilization {util:.1%} of base — normal operation"

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
    cap      = getattr(CONFIG.risk, "equity_cap", None)
    if cap is None:
        base = equity
    else:
        cap = float(cap)
        base = min(equity, cap) if cap > 0 else equity
    total_mv = sum(abs(float(p.get("qty", 0))) * float(p.get("current_price", 0)) for p in positions)
    util     = total_mv / base if base > 0 else 0
    on_margin = cash < 0

    print(f"\n{'='*55}")
    print(f"  RAPTOR CAPITAL UTILIZATION SNAPSHOT")
    print(f"{'='*55}")
    print(f"  Equity:          ${equity:>12,.2f}")
    print(f"  Equity Cap:      {'  none (compound)' if cap is None else f'${cap:>12,.2f}'}")
    print(f"  Cash:            ${cash:>12,.2f}  {'⚠ ON MARGIN' if on_margin else '✓'}")
    print(f"  Buying Power:    ${bp:>12,.2f}")
    print(f"  Market Value:    ${total_mv:>12,.2f}")
    _headroom = max(0.0, (cap if cap is not None else equity) - total_mv)
    print(f"  Headroom:        ${_headroom:>12,.2f}  ({'cap' if cap is not None else 'no-margin'})")
    print(f"  Utilization:     {util:>11.1%}  (of {'cap' if cap is not None else 'equity'})  ", end="")

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
