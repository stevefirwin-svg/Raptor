"""
backfill_positions.py
=====================
One-time script to restore 8 positions that exist in Alpaca but were
incorrectly removed from position_ledger.json by record_exit on math_trim.

Run ONCE from your Raptor directory, then delete.
Verifies against Alpaca before writing — will not overwrite existing entries.

Usage:
    python backfill_positions.py
"""
import json
from datetime import datetime
from data_feeds import DataManager, AlpacaDataFeed
from config import CONFIG
from ledger import Ledger

# Symbols that need restoring — confirmed open in Alpaca, missing from ledger
ORPHANS = ["AMD", "CSX", "CVE", "DKNG", "INTC", "KDP", "PLTD", "SMCI"]

def main():
    print("=== LEDGER BACKFILL ===")
    print(f"Restoring {len(ORPHANS)} orphaned positions to ledger")
    print()

    # Load Alpaca ground truth
    alpaca = AlpacaDataFeed(CONFIG)
    positions = alpaca.get_positions()
    alpaca_map = {p["symbol"]: p for p in positions}

    # Load ledger history to recover original entry data from closed records
    ledger = Ledger()
    closed = ledger.data.get("closed", [])
    current_open = {v["symbol"] for v in ledger.data.get("positions", {}).values()}

    restored = 0
    skipped  = 0

    for sym in ORPHANS:
        # Safety: don't overwrite if already in ledger open
        if sym in current_open:
            print(f"  SKIP {sym} — already in ledger open positions")
            skipped += 1
            continue

        # Must be confirmed open in Alpaca
        if sym not in alpaca_map:
            print(f"  SKIP {sym} — not found in Alpaca (may have been closed today)")
            skipped += 1
            continue

        alp = alpaca_map[sym]
        current_qty = int(float(alp["qty"]))

        # Recover original entry data from most recent ledger closed record
        # These were incorrectly closed by math_trim calls to record_exit
        closed_records = sorted(
            [t for t in closed if t.get("symbol") == sym],
            key=lambda x: x.get("exit_date", ""),
            reverse=True
        )

        if closed_records:
            # Use original entry price from the closed record (was correct when entered)
            original = closed_records[0]
            entry_price = float(original.get("entry_price", alp.get("avg_entry", 0)))
            entry_date  = original.get("entry_date", "2026-05-01")
            stop        = original.get("metadata", {}).get("stop")
            regime      = original.get("metadata", {}).get("regime", "BACKFILL_RESTORED")
        else:
            # No closed history — use Alpaca avg_entry as best available
            entry_price = float(alp.get("avg_entry", 0))
            entry_date  = "2026-05-01"  # approximate
            stop        = None
            regime      = "BACKFILL_RESTORED"

        # Compute stop if missing: use most recent closed record stop if available
        # Do NOT fabricate via price * 0.02
        metadata = {
            "stop":              stop,
            "regime":            regime,
            "t_stat":            None,
            "kelly_fraction":    None,
            "composite_score":   None,
            "note":              f"Restored by backfill_positions.py {datetime.now().strftime('%Y-%m-%d')} — "
                                 f"position was incorrectly closed in ledger by math_trim routing bug",
        }

        # Write to ledger
        ledger.record_entry(
            model       = "v5.4",
            symbol      = sym,
            shares      = current_qty,
            entry_price = entry_price,
            date        = entry_date,
            metadata    = metadata,
        )

        pnl = (float(alp["current_price"]) / entry_price - 1) * 100
        print(f"  RESTORED {sym:6s}  qty={current_qty:4d}  "
              f"entry=${entry_price:.2f}  current=${alp['current_price']:.2f}  "
              f"pnl={pnl:+.1f}%  stop={stop}")
        restored += 1

    print()
    print(f"Done. Restored: {restored}  Skipped: {skipped}")
    print()

    # Verify final state
    ledger2 = Ledger()
    open_syms = sorted(v["symbol"] for v in ledger2.data["positions"].values())
    print(f"Ledger open positions ({len(open_syms)}): {open_syms}")

    # Cross-check against Alpaca
    alpaca_syms = sorted(alpaca_map.keys())
    missing = set(alpaca_syms) - set(open_syms)
    extra   = set(open_syms) - set(alpaca_syms)
    if missing:
        print(f"WARNING — still missing from ledger: {sorted(missing)}")
    if extra:
        print(f"WARNING — in ledger but not in Alpaca: {sorted(extra)}")
    if not missing and not extra:
        print("Ledger and Alpaca are now in sync.")

if __name__ == "__main__":
    main()
