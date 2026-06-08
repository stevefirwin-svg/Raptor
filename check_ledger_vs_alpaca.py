"""
check_ledger_vs_alpaca.py — Reconcile position_ledger.json against live Alpaca positions.

Checks:
  1. Every live Alpaca position has a matching ledger entry
  2. Every active ledger entry exists on Alpaca
  3. Share counts match
  4. Ledger stop is not above current price (would fire immediately)
  5. Flags any stop within 1% of current price as a warning

Usage:
    python check_ledger_vs_alpaca.py
"""

import json
import os
from config import CONFIG
from data_feeds import AlpacaDataFeed

LEDGER_FILE = "position_ledger.json"

# ── Load data ──────────────────────────────────────────────────────────────────
dm = AlpacaDataFeed(CONFIG)
alpaca_positions = {p["symbol"]: p for p in dm.get_positions()}

with open(LEDGER_FILE) as f:
    ledger_data = json.load(f)
ledger_positions = ledger_data.get("positions", {})

# Build symbol -> ledger entry map
ledger_by_symbol = {}
for key, entry in ledger_positions.items():
    sym = entry["symbol"]
    ledger_by_symbol[sym] = entry

print("\n=== RECONCILIATION: LEDGER vs ALPACA ===\n")
print(f"  Alpaca positions : {len(alpaca_positions)}")
print(f"  Ledger positions : {len(ledger_by_symbol)}")

issues = []
warnings = []

# ── Check 1: Alpaca positions not in ledger ────────────────────────────────────
print("\n--- Alpaca positions ---")
for sym, pos in sorted(alpaca_positions.items()):
    qty      = float(pos["qty"])
    price    = float(pos["current_price"])
    pnl_pct  = float(pos["unrealized_pnl_pct"]) * 100

    if sym not in ledger_by_symbol:
        issues.append(f"MISSING FROM LEDGER: {sym} (Alpaca has {qty} shares @ ${price:.2f})")
        print(f"  {sym:8s}  qty={qty:>8.0f}  price=${price:>8.2f}  pnl={pnl_pct:>+6.1f}%  !! NOT IN LEDGER !!")
        continue

    entry    = ledger_by_symbol[sym]
    l_shares = entry["shares"]
    stop     = entry["metadata"].get("stop", None)
    hw       = entry["metadata"].get("high_water", None)

    stop_str = f"stop=${stop:.3f}" if stop else "stop=NONE"
    hw_str   = f"hw=${hw:.3f}" if hw else ""
    match    = "OK" if abs(l_shares - qty) < 0.5 else f"MISMATCH (ledger={l_shares})"

    # Stop above price = fires immediately
    if stop and stop > price:
        issues.append(f"STOP ABOVE PRICE: {sym} stop=${stop:.3f} > price=${price:.2f} — EXIT 1 fires on next run")
        stop_str += " !! STOP ABOVE PRICE !!"
    elif stop and (stop / price) > 0.99:
        warnings.append(f"STOP WITHIN 1% of price: {sym} stop=${stop:.3f}  price=${price:.2f}")
        stop_str += " (within 1%)"

    print(f"  {sym:8s}  qty={qty:>8.0f}  price=${price:>8.2f}  pnl={pnl_pct:>+6.1f}%  {stop_str}  {hw_str}  [{match}]")

# ── Check 2: Ledger positions not on Alpaca ────────────────────────────────────
print("\n--- Ledger positions not on Alpaca ---")
orphans = [sym for sym in ledger_by_symbol if sym not in alpaca_positions]
if not orphans:
    print("  (none — ledger matches Alpaca)")
else:
    for sym in orphans:
        entry = ledger_by_symbol[sym]
        issues.append(f"LEDGER ORPHAN: {sym} (ledger has {entry['shares']} shares but not on Alpaca)")
        print(f"  !! {sym:8s}  ledger shares={entry['shares']}  NOT FOUND ON ALPACA")

# ── Summary ────────────────────────────────────────────────────────────────────
print("\n=== SUMMARY ===\n")
if not issues and not warnings:
    print("  All clear. Ledger and Alpaca are in sync. No stop issues.")
else:
    if issues:
        print(f"  ISSUES ({len(issues)}) — must fix before market open:")
        for i in issues:
            print(f"    !! {i}")
    if warnings:
        print(f"\n  WARNINGS ({len(warnings)}) — review but not blocking:")
        for w in warnings:
            print(f"    ~~ {w}")

print()
