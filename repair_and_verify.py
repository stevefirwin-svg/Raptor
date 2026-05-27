"""
repair_and_verify.py
====================
One-time cleanup script. Run once, then delete.

Fixes:
  1. Closed trade pnl_pct — converts raw decimal to percentage for all historical records
  2. Adds missing high_water to open positions that were backfilled without it
  3. Prints full data integrity summary after fixes

Usage:
    python repair_and_verify.py
"""
import json, os
from datetime import datetime, date

LEDGER_FILE = "position_ledger.json"

print("=== RAPTOR DATA REPAIR ===")
print()

# ── Load ──────────────────────────────────────────────────────────────────────
ledger = json.load(open(LEDGER_FILE, encoding="utf-8"))
open_pos = ledger.get("positions", {})
closed   = ledger.get("closed", [])

# ── Fix 1: Closed pnl_pct decimal → percentage ────────────────────────────────
print("Fix 1: Closed trade pnl_pct (decimal → percentage)")
fixed_pnl = 0
for trade in closed:
    pnl = trade.get("pnl_pct")
    if pnl is not None:
        pnl_f = float(pnl)
        # Threshold: if |pnl| < 1.5, it's stored as decimal (0.42) not percent (42.0)
        # Swing trades: typical range -15% to +65%. Raw decimal range: -0.15 to +0.65.
        if abs(pnl_f) < 1.5:
            old = pnl_f
            trade["pnl_pct"] = round(pnl_f * 100, 4)
            fixed_pnl += 1
print(f"  Fixed {fixed_pnl} records")
if fixed_pnl > 0:
    print("  Sample (first 5):")
    for t in closed[:5]:
        print(f"    {t.get('symbol','?'):6s}  pnl_pct={t['pnl_pct']:+.2f}%")

# ── Fix 2: Missing high_water on open positions ───────────────────────────────
print()
print("Fix 2: Missing high_water on open positions")
fixed_hw = 0
hh = {}
if os.path.exists("hold_health.json"):
    hh = json.load(open("hold_health.json", encoding="utf-8"))

for key, pos in open_pos.items():
    sym  = pos.get("symbol", key)
    meta = pos.get("metadata", {})
    if not meta.get("high_water"):
        # Try to get current price from hold_health snapshot
        snap = hh.get(sym, {}).get("snapshot", {})
        current = float(snap.get("current_price") or snap.get("price") or 0)
        entry   = float(pos.get("entry_price", pos.get("avg_entry", 0)))
        hw = max(current, entry) if current > 0 else entry
        if hw > 0:
            ledger["positions"][key]["metadata"]["high_water"] = round(hw, 4)
            fixed_hw += 1
            print(f"  {sym}: high_water set to ${hw:.2f}")

if fixed_hw == 0:
    print("  None needed — all open positions have high_water")

# ── Save ──────────────────────────────────────────────────────────────────────
print()
tmp = LEDGER_FILE + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(ledger, f, indent=2, default=str)
os.replace(tmp, LEDGER_FILE)
print(f"Ledger saved — {fixed_pnl} pnl fixes, {fixed_hw} high_water fixes")

# ── Verify ────────────────────────────────────────────────────────────────────
print()
print("=== POST-FIX VERIFICATION ===")
print()

ledger2  = json.load(open(LEDGER_FILE, encoding="utf-8"))
open2    = ledger2.get("positions", {})
closed2  = ledger2.get("closed", [])
outcome  = json.load(open("outcome_log.json", encoding="utf-8"))
hh2      = json.load(open("hold_health.json", encoding="utf-8")) if os.path.exists("hold_health.json") else {}

issues = []

# Check open positions
for key, pos in open2.items():
    sym  = pos.get("symbol", key)
    meta = pos.get("metadata", {})
    if not meta.get("stop"):        issues.append(f"{sym}: missing stop")
    if not meta.get("high_water"):  issues.append(f"{sym}: missing high_water")
    if not pos.get("entry_date"):   issues.append(f"{sym}: missing entry_date")
    if (pos.get("shares") or 0) <= 0: issues.append(f"{sym}: invalid shares")

# Check closed pnl units
bad_pnl = [t for t in closed2 if t.get("pnl_pct") is not None and abs(float(t["pnl_pct"])) < 0.5]
if bad_pnl:
    issues.append(f"{len(bad_pnl)} closed trades still have suspiciously small pnl_pct")

# Check outcome log
unknowns = [r for r in outcome if r.get("actual_exit_path") == "unknown"]
if unknowns:
    issues.append(f"{len(unknowns)} outcome_log records with exit_path=unknown")

null_pnl = [r for r in outcome if r.get("actual_pnl_pct") is None
            and r.get("actual_exit_path") not in ("pre_label", "crypto")]
if null_pnl:
    issues.append(f"{len(null_pnl)} outcome_log records missing pnl")

# Counts
terminal = [r for r in outcome if r.get("actual_exit_path") in
            ("trailing_stop","math_exit","hard_stop","thesis_invalid","time_decay")]
trims    = [r for r in outcome if r.get("actual_exit_path") == "math_trim"]
pre      = [r for r in outcome if r.get("actual_exit_path") == "pre_label"]
crypto   = [r for r in outcome if "/" in r.get("symbol","")]

print(f"Ledger open:        {len(open2)} positions")
for key, pos in open2.items():
    sym  = pos.get("symbol",key)
    meta = pos.get("metadata",{})
    stop = meta.get("stop","MISSING")
    hw   = meta.get("high_water","MISSING")
    sh   = pos.get("shares","?")
    entry= float(pos.get("entry_price", pos.get("avg_entry",0)))
    date_str = pos.get("entry_date","?")
    stop_str = f"${float(stop):.2f}" if stop != "MISSING" else "MISSING"
    hw_str   = f"${float(hw):.2f}"   if hw   != "MISSING" else "MISSING"
    print(f"  {sym:6s}  {sh}sh  entry=${entry:.2f}  stop={stop_str}  hw={hw_str}  date={date_str}")

print()
print(f"Ledger closed:      {len(closed2)} trades")
pnl_vals = [float(t["pnl_pct"]) for t in closed2 if t.get("pnl_pct") is not None]
if pnl_vals:
    import statistics
    print(f"  pnl range: {min(pnl_vals):+.2f}%  to  {max(pnl_vals):+.2f}%  (mean {statistics.mean(pnl_vals):+.2f}%)")

print()
print(f"Outcome log:        {len(outcome)} total")
print(f"  IC-valid exits:   {len(terminal)}  (need 60 to unlock IC gates)")
print(f"  math_trim:        {len(trims)}  (excluded from IC — partial exits)")
print(f"  pre_label:        {len(pre)}  (historical, excluded)")
print(f"  crypto:           {len(crypto)}  (separate system)")

print()
if issues:
    print(f"REMAINING ISSUES ({len(issues)}):")
    for i in issues:
        print(f"  ! {i}")
else:
    print("All checks PASS — data is clean.")

print()
print("Delete this script after running: Remove-Item repair_and_verify.py")
