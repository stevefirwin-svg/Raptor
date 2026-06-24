"""
repair_ledger_20260624.py
=========================
Two fixes:
1. KRE: ghost in positions{} — was exited 2026-06-19 (hard_stop). Move to closed[].
2. HOOD: ledger says 117 shares, Alpaca has 57. 60 shares oversold during manual
   margin cleanup on 2026-06-24. Record the partial exit and update qty to 57.

Usage:
  python repair_ledger_20260624.py          # dry run
  python repair_ledger_20260624.py --write  # apply
"""
import json, os, sys
from datetime import datetime

LEDGER_PATH = "position_ledger.json"
write_mode  = "--write" in sys.argv

print(f"\n{'='*60}")
print(f"  LEDGER REPAIR 2026-06-24  ({'WRITE' if write_mode else 'DRY RUN'})")
print(f"{'='*60}\n")

with open(LEDGER_PATH) as f:
    ledger = json.load(f)

positions = ledger.get("positions", {})
closed    = ledger.get("closed", [])

# ── 1. Close KRE ─────────────────────────────────────────────────────────────
kre_key = "v5.4:KRE"
if kre_key not in positions:
    print("KRE: not in positions{} — already closed, nothing to do.")
else:
    kre        = positions[kre_key]
    kre_stop   = kre["metadata"].get("stop", 72.083)
    kre_entry  = kre["entry_price"]
    kre_shares = kre["shares"]
    kre_pnl    = round((kre_stop - kre_entry) * kre_shares, 2)
    kre_pct    = round((kre_stop - kre_entry) / kre_entry * 100, 4)

    print(f"KRE: {kre_shares} shares @ ${kre_entry:.3f} entry, stop=${kre_stop:.3f}")
    print(f"     Exit P&L: ${kre_pnl:+.2f} ({kre_pct:+.2f}%)")
    print(f"     Action: move to closed[] as hard_stop on 2026-06-19")

    kre_closed = {
        "model":       kre.get("model", "v5.4"),
        "symbol":      "KRE",
        "shares":      kre_shares,
        "entry_price": kre_entry,
        "entry_date":  kre.get("entry_date", "2026-06-05"),
        "metadata":    kre.get("metadata", {}),
        "trims":       kre.get("trims", []),
        "exit_price":  kre_stop,
        "exit_date":   "2026-06-19",
        "exit_reason": "hard_stop",
        "exit_path":   "hard_stop",
        "pnl":         kre_pnl,
        "pnl_pct":     kre_pct,
        "repair_note": (
            "Repaired 2026-06-24 — KRE exited 2026-06-19 per exits_20260619.log "
            "(hard_stop confirmed). Ledger not updated due to stale pending_exit=True. "
            "Exit price approximated from stop price in metadata."
        ),
    }

    if write_mode:
        del positions[kre_key]
        closed.append(kre_closed)
        print("     DONE: KRE moved to closed[]\n")
    else:
        print("     (dry run)\n")

# ── 2. Update HOOD qty 117 → 57 ──────────────────────────────────────────────
hood_key = "v5.4:HOOD"
if hood_key not in positions:
    print("HOOD: not in positions{} — unexpected, check ledger manually.")
else:
    hood       = positions[hood_key]
    hood_entry = hood["entry_price"]   # 109.00
    hood_old   = hood["shares"]        # 117
    hood_new   = 57
    oversold   = hood_old - hood_new   # 60
    exit_price = 100.50                # approximate fill from Alpaca at time of manual sell
    trim_pnl   = round((exit_price - hood_entry) * oversold, 2)
    trim_pct   = round((exit_price - hood_entry) / hood_entry * 100, 4)

    print(f"HOOD: ledger={hood_old} shares, Alpaca=57 shares — oversold {oversold} during margin cleanup")
    print(f"      Recording {oversold}sh partial exit @ ~${exit_price:.2f}  P&L: ${trim_pnl:+.2f} ({trim_pct:+.2f}%)")
    print(f"      Updating qty: {hood_old} → {hood_new}")

    trim_record = {
        "date":          "2026-06-24",
        "reason":        "manual_margin_cleanup",
        "shares_sold":   oversold,
        "trim_price":    exit_price,
        "pnl_pct":       trim_pct,
        "pnl_abs":       trim_pnl,
        "shares_before": hood_old,
        "shares_after":  hood_new,
        "repair_note":   (
            "60 shares oversold during 2026-06-24 manual margin cleanup "
            "(intended to sell 228 untracked duplicate shares, sold 288). "
            "Accepted as-is per Steve's decision. Price approximated."
        ),
    }

    if write_mode:
        positions[hood_key]["shares"] = hood_new
        if "trims" not in positions[hood_key]:
            positions[hood_key]["trims"] = []
        positions[hood_key]["trims"].append(trim_record)
        positions[hood_key]["metadata"]["last_trim_ts"] = datetime.now().isoformat()
        print("     DONE: HOOD shares updated, trim recorded\n")
    else:
        print("     (dry run)\n")

# ── Summary ───────────────────────────────────────────────────────────────────
print("=== POST-REPAIR STATE ===")
open_syms = [v["symbol"] for v in positions.values() if v["symbol"] != "KRE"]
print(f"Open positions ({len(positions) - (1 if kre_key in positions else 0)}): "
      f"{', '.join(sorted(open_syms))}")

# Verify all remaining positions match Alpaca
expected = {
    "AAL": 688, "BAC": 78, "CPNG": 711, "CRWV": 101,
    "GOOGL": 29, "HOOD": 57, "MRVL": 2, "UBER": 176, "WULF": 319,
}
print("\nFinal reconciliation check:")
all_ok = True
for sym, exp_qty in expected.items():
    key   = f"v5.4:{sym}"
    l_qty = positions.get(key, {}).get("shares", "MISSING")
    # apply the write in memory for dry-run display
    if not write_mode and sym == "HOOD":
        l_qty = 57
    match = "✓" if l_qty == exp_qty else f"✗ MISMATCH (ledger={l_qty})"
    if l_qty != exp_qty:
        all_ok = False
    print(f"  {sym}: Alpaca={exp_qty}  Ledger={l_qty}  {match}")

print()
if all_ok:
    print("All positions reconciled. Ledger matches Alpaca exactly.")
else:
    print("WARNING: mismatches remain — investigate before next scan.")

# ── Write ─────────────────────────────────────────────────────────────────────
if write_mode:
    ledger["positions"] = positions
    ledger["closed"]    = closed
    tmp = LEDGER_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2)
    os.replace(tmp, LEDGER_PATH)
    print(f"\nWritten to {LEDGER_PATH}")
    print(f"  Open:   {len(positions)}")
    print(f"  Closed: {len(closed)}")
else:
    print("\nDRY RUN — run with --write to apply.")
print()
