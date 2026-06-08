"""
cancel_stale_orders.py — Check for and cancel stale open orders on Alpaca.

Background:
  During June 1-4, exit_monitor correctly queued exits but submit_order was broken
  (missing def line). Some positions also had insufficient_qty rejections on May 24-25
  due to the double-trim guard reading a stale snapshot. Shares were locked by ghost
  orders on CVE, PLTD, DKNG, CSX, KDP.

  This script:
    1. Lists ALL open orders on Alpaca
    2. Shows age, symbol, side, qty for each
    3. Flags any order older than 1 trading day as stale
    4. Asks for confirmation before cancelling stale orders
    5. Prints a clean summary of what was cancelled

Usage:
    python cancel_stale_orders.py           # interactive — prompts before cancel
    python cancel_stale_orders.py --dry-run # show only, cancel nothing
"""

import sys
from datetime import datetime, timezone, timedelta
from config import CONFIG
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

DRY_RUN = "--dry-run" in sys.argv

# ── Auth ───────────────────────────────────────────────────────────────────────
client = TradingClient(
    api_key=CONFIG.alpaca.api_key,
    secret_key=CONFIG.alpaca.secret_key,
    paper=True
)

# ── Fetch all open orders ──────────────────────────────────────────────────────
print("\n=== ALPACA OPEN ORDERS ===\n")

req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
orders = client.get_orders(req)

if not orders:
    print("No open orders found. Nothing to cancel.")
    sys.exit(0)

now = datetime.now(timezone.utc)
stale_cutoff = now - timedelta(hours=8)  # anything older than 8 hours is stale

stale = []
fresh = []

for o in orders:
    created = o.created_at
    age_hrs = (now - created).total_seconds() / 3600
    age_str = f"{age_hrs:.1f}h ago"
    flag = " <-- STALE" if created < stale_cutoff else ""
    print(f"  {o.symbol:8s}  {str(o.side):10s}  qty={o.qty:>8}  status={str(o.status):12s}  created={created.strftime('%Y-%m-%d %H:%M')} ({age_str}){flag}")
    if created < stale_cutoff:
        stale.append(o)
    else:
        fresh.append(o)

print(f"\nTotal open: {len(orders)}  |  Stale (>8h): {len(stale)}  |  Fresh: {len(fresh)}")

if not stale:
    print("\nNo stale orders to cancel.")
    sys.exit(0)

# ── Cancel stale ──────────────────────────────────────────────────────────────
print(f"\nStale orders to cancel:")
for o in stale:
    print(f"  {o.id}  {o.symbol}  {o.side}  qty={o.qty}")

if DRY_RUN:
    print("\n[DRY RUN] No cancellations made.")
    sys.exit(0)

confirm = input(f"\nCancel {len(stale)} stale order(s)? [y/N]: ").strip().lower()
if confirm != "y":
    print("Aborted. No changes made.")
    sys.exit(0)

cancelled = 0
failed = 0
for o in stale:
    try:
        client.cancel_order_by_id(o.id)
        print(f"  CANCELLED: {o.symbol} {o.side} qty={o.qty}  id={o.id}")
        cancelled += 1
    except Exception as e:
        print(f"  FAILED to cancel {o.symbol} {o.id}: {e}")
        failed += 1

print(f"\nDone. Cancelled: {cancelled}  Failed: {failed}")

# ── Final state ───────────────────────────────────────────────────────────────
print("\n=== REMAINING OPEN ORDERS ===")
remaining = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
if not remaining:
    print("  (none)")
else:
    for o in remaining:
        print(f"  {o.symbol:8s}  {str(o.side):10s}  qty={o.qty}  status={o.status}")
