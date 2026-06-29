"""
slippage_tracker.py — Implementation Shortfall Analytics
=========================================================
Records and analyses the difference between the decision-time price (the
price the signal or stop was evaluated at) and the actual fill price from
Alpaca.  This is the Perold (1988) implementation shortfall decomposition
applied to a systematic equity book.

Mathematical basis
------------------
Implementation shortfall (IS) per share:

    IS = side * (fill_price - decision_price)

where side = +1 for BUY, -1 for SELL.  A positive IS is slippage AGAINST
us (paid more to buy, received less on sell).  Expressed as basis points:

    IS_bps = IS / decision_price * 10_000

For a market order the IS decomposition is:
    - Timing cost : price drift between signal and order submission
      (not directly observable here — market orders submit ~seconds after
       signal, so this is small for daily-frequency strategies)
    - Bid-ask spread cost : half-spread captured at fill
    - Market impact : large orders moving price against us (negligible at
      current share sizes, but logged for future monitoring)

Reference: Perold (1988), "The Implementation Shortfall: Paper vs Reality",
           Journal of Portfolio Management.
           Almgren & Chriss (2000) for the impact term at larger sizes.

Log schema (slippage_log.json)
-------------------------------
Each record:
{
  "ts":             ISO timestamp of the fill (from Alpaca)
  "symbol":         str
  "side":           "BUY" | "SELL"
  "qty":            float
  "decision_price": float   — price used by signal / stop evaluation
  "fill_price":     float   — filled_avg_price from Alpaca order
  "is_bps":         float   — implementation shortfall in basis points
  "is_dollars":     float   — IS * qty (total cost of this execution)
  "order_id":       str
  "exit_reason":    str | None  — for sells: hard_stop, trailing_stop, etc.
  "notional":       float   — qty * decision_price
}

Usage
-----
Called from exit_monitor.py (sells) and main.py (buys) immediately after a
confirmed fill.  The order result dict from AlpacaDataFeed.submit_order()
must contain "filled_avg_price"; if Alpaca returns the order before fill
confirmation (status=pending_new) we write a deferred record with
fill_price=None and backfill it on the next outcome_tracker run.

Analytics (run standalone)
--------------------------
    python slippage_tracker.py --report
"""

import json
import os
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("raptor.slippage")

SLIPPAGE_LOG_PATH = Path("slippage_log.json")


# ── Atomic write ──────────────────────────────────────────────────────────────

def _atomic_write(path: Path, data: list) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _load() -> list:
    if not SLIPPAGE_LOG_PATH.exists():
        return []
    try:
        return json.loads(SLIPPAGE_LOG_PATH.read_text())
    except Exception as e:
        # FIX (2026-06-29, audit P2): previously swallowed silently and
        # returned [] — every caller (record_fill, backfill_slippage, report)
        # would then silently start from an empty log, meaning a corrupted
        # file looks identical to "no slippage history yet" and the next
        # _atomic_write() permanently discards everything recorded before
        # the corruption. Log loudly so this gets noticed and the raw file
        # can be recovered before it's overwritten.
        logger.warning("Failed to load %s: %s — treating as empty. "
                        "Check this file for corruption before further writes "
                        "overwrite it.", SLIPPAGE_LOG_PATH, e)
        return []


# ── Core record builder ───────────────────────────────────────────────────────

def record_fill(
    symbol:         str,
    side:           str,           # "BUY" | "SELL"
    qty:            float,
    decision_price: float,         # price at signal / stop evaluation time
    order_result:   dict,          # dict returned by AlpacaDataFeed.submit_order()
    exit_reason:    Optional[str] = None,
) -> Optional[dict]:
    """
    Compute and persist one implementation-shortfall record.

    Returns the record dict (for callers that want to log it inline),
    or None if fill price is not yet available (order still pending).
    """
    if not order_result or "error" in order_result:
        return None

    fill_price_raw = order_result.get("filled_avg_price")
    order_id       = order_result.get("id", "")
    fill_ts        = order_result.get("filled_at") or datetime.now(timezone.utc).isoformat()

    # Alpaca sometimes returns pending_new before fill confirmation.
    # We write a deferred record and backfill in backfill_slippage().
    if fill_price_raw is None or float(fill_price_raw or 0) == 0:
        fill_price = None
        is_bps     = None
        is_dollars = None
    else:
        fill_price = float(fill_price_raw)
        # IS = side_sign * (fill - decision) / decision * 10_000
        # Positive = cost against us.
        side_sign  = 1.0 if side.upper() == "BUY" else -1.0
        is_bps     = round(side_sign * (fill_price - decision_price) / decision_price * 10_000, 2)
        is_dollars = round(side_sign * (fill_price - decision_price) * qty, 4)

    notional = round(decision_price * qty, 2)

    record = {
        "ts":             fill_ts,
        "symbol":         symbol,
        "side":           side.upper(),
        "qty":            qty,
        "decision_price": round(decision_price, 4),
        "fill_price":     round(fill_price, 4) if fill_price else None,
        "is_bps":         is_bps,
        "is_dollars":     is_dollars,
        "order_id":       order_id,
        "exit_reason":    exit_reason,
        "notional":       notional,
        "status":         order_result.get("status", "unknown"),
        "backfilled":     False,
    }

    existing = _load()
    existing.append(record)
    _atomic_write(SLIPPAGE_LOG_PATH, existing)

    if is_bps is not None:
        logger.info(
            "SLIPPAGE %s %s %s: decision=$%.4f fill=$%.4f IS=%+.1f bps ($%+.2f)",
            side.upper(), qty, symbol,
            decision_price, fill_price, is_bps, is_dollars
        )
    else:
        logger.info(
            "SLIPPAGE %s %s %s: decision=$%.4f fill=PENDING (order %s)",
            side.upper(), qty, symbol, decision_price, order_id[:8]
        )

    return record


# ── Backfill pending fills ────────────────────────────────────────────────────

def backfill_slippage(alpaca_headers_fn, base_url: str) -> int:
    """
    Fetch fill prices for any records where fill_price=None (pending at log
    time).  Called from outcome_tracker.run_tracker() at end of day.
    Returns number of records backfilled.
    """
    import requests

    records  = _load()
    pending  = [r for r in records if r.get("fill_price") is None and r.get("order_id")]
    if not pending:
        return 0

    filled = 0
    for r in pending:
        try:
            resp = requests.get(
                f"{base_url}/v2/orders/{r['order_id']}",
                headers=alpaca_headers_fn(),
                timeout=10,
            )
            if resp.status_code != 200:
                continue
            order = resp.json()
            fp = float(order.get("filled_avg_price") or 0)
            if fp == 0:
                continue

            side_sign  = 1.0 if r["side"] == "BUY" else -1.0
            dp         = r["decision_price"]
            qty        = r["qty"]
            r["fill_price"]  = round(fp, 4)
            r["is_bps"]      = round(side_sign * (fp - dp) / dp * 10_000, 2)
            r["is_dollars"]  = round(side_sign * (fp - dp) * qty, 4)
            r["backfilled"]  = True
            filled += 1
            logger.info(
                "SLIPPAGE BACKFILL %s %s: IS=%+.1f bps",
                r["symbol"], r["order_id"][:8], r["is_bps"]
            )
        except Exception as e:
            logger.warning("Slippage backfill failed for order %s: %s", r.get("order_id","?")[:8], e)

    if filled:
        _atomic_write(SLIPPAGE_LOG_PATH, records)
    return filled


# ── Analytics report ──────────────────────────────────────────────────────────

def report() -> dict:
    """
    Compute summary statistics over all filled slippage records.

    Returns a dict suitable for daily_recap.py and the dashboard.

    Metrics:
    - mean_is_bps       : average implementation shortfall across all fills
    - mean_buy_is_bps   : average IS on entry (BUY) orders
    - mean_sell_is_bps  : average IS on exit (SELL) orders
    - total_is_dollars  : total dollar cost of slippage (all fills)
    - worst_fill_bps    : single worst fill (most negative for us)
    - by_exit_reason    : mean IS per exit reason (hard_stop vs trail etc.)
    - n_fills           : number of filled records in the analysis
    - p95_is_bps        : 95th percentile IS (tail risk of bad fills)
    - round_trip_bps    : mean_buy_is_bps + mean_sell_is_bps (total per trade)
    """
    records = [r for r in _load() if r.get("is_bps") is not None]

    if not records:
        return {"n_fills": 0, "note": "no filled slippage records yet"}

    import statistics

    buys  = [r for r in records if r["side"] == "BUY"]
    sells = [r for r in records if r["side"] == "SELL"]

    all_bps  = [r["is_bps"] for r in records]
    buy_bps  = [r["is_bps"] for r in buys]
    sell_bps = [r["is_bps"] for r in sells]

    # by exit reason
    from collections import defaultdict
    by_reason: dict = defaultdict(list)
    for r in sells:
        reason = r.get("exit_reason") or "unknown"
        by_reason[reason].append(r["is_bps"])

    by_reason_mean = {
        k: round(statistics.mean(v), 2)
        for k, v in by_reason.items()
    }

    sorted_bps = sorted(all_bps)
    p95_idx    = int(len(sorted_bps) * 0.95)

    result = {
        "n_fills":          len(records),
        "mean_is_bps":      round(statistics.mean(all_bps), 2),
        "mean_buy_is_bps":  round(statistics.mean(buy_bps),  2) if buy_bps  else None,
        "mean_sell_is_bps": round(statistics.mean(sell_bps), 2) if sell_bps else None,
        "round_trip_bps":   None,
        "total_is_dollars": round(sum(r["is_dollars"] for r in records), 2),
        "worst_fill_bps":   round(max(all_bps), 2),
        "p95_is_bps":       round(sorted_bps[min(p95_idx, len(sorted_bps)-1)], 2),
        "by_exit_reason":   by_reason_mean,
        "stdev_is_bps":     round(statistics.stdev(all_bps), 2) if len(all_bps) > 1 else None,
    }

    if result["mean_buy_is_bps"] is not None and result["mean_sell_is_bps"] is not None:
        result["round_trip_bps"] = round(
            result["mean_buy_is_bps"] + result["mean_sell_is_bps"], 2
        )

    return result


def print_report() -> None:
    r = report()
    print("\n══════════════════════════════════════════════")
    print("  IMPLEMENTATION SHORTFALL REPORT")
    print("══════════════════════════════════════════════")
    if r.get("n_fills", 0) == 0:
        print("  No filled records yet.")
        return

    print(f"  Fills analysed      : {r['n_fills']}")
    print(f"  Mean IS             : {r['mean_is_bps']:+.1f} bps")
    print(f"  Mean BUY IS         : {r['mean_buy_is_bps']:+.1f} bps")
    print(f"  Mean SELL IS        : {r['mean_sell_is_bps']:+.1f} bps")
    if r["round_trip_bps"] is not None:
        print(f"  Round-trip cost     : {r['round_trip_bps']:+.1f} bps per trade")
    print(f"  StDev IS            : {r['stdev_is_bps']:+.1f} bps")
    print(f"  P95 IS (tail)       : {r['p95_is_bps']:+.1f} bps")
    print(f"  Worst single fill   : {r['worst_fill_bps']:+.1f} bps")
    print(f"  Total IS cost ($)   : ${r['total_is_dollars']:+,.2f}")
    print(f"\n  IS by exit reason:")
    for reason, bps in sorted(r["by_exit_reason"].items(), key=lambda x: -x[1]):
        print(f"    {reason:<25} {bps:+.1f} bps")
    print("══════════════════════════════════════════════\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")
    parser = argparse.ArgumentParser(description="Raptor slippage analytics")
    parser.add_argument("--report", action="store_true", help="Print IS summary report")
    args = parser.parse_args()
    if args.report:
        print_report()
