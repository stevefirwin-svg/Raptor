"""
backfill_ledger.py — One-time ledger population from live Alpaca positions
==========================================================================
Run this ONCE to populate position_ledger.json from your current Alpaca
positions. Use when the ledger is empty but Alpaca has open positions
(e.g. after a manual clear, fresh install, or positions entered outside the bot).

What it does:
  - Reads all open positions from Alpaca
  - Writes each into position_ledger.json under model "v5.4"
  - Sets entry_date = today (best available without history)
  - Estimates stop = entry_price - (initial_stop_atr_mult * 2% of price) as placeholder
  - Does NOT overwrite positions already in the ledger

Usage:
  python backfill_ledger.py          # Preview what would be written
  python backfill_ledger.py --write  # Actually write to ledger
"""

import argparse
import json
import os
from datetime import date

from config import CONFIG
from data_feeds import DataManager
from ledger import Ledger


def backfill(write: bool = False):
    dm = DataManager(CONFIG)
    positions = dm.alpaca.get_positions()

    if not positions:
        print("No open positions in Alpaca. Nothing to backfill.")
        return

    ledger = Ledger()
    existing_symbols = ledger.get_all_held_symbols()

    to_write = []
    skipped  = []

    for p in positions:
        sym = p["symbol"]
        if sym in existing_symbols:
            skipped.append(sym)
            continue

        entry_price   = float(p.get("avg_entry", 0))
        current_price = float(p.get("current_price", entry_price))
        qty           = int(float(p.get("qty", 0)))
        pnl_pct       = float(p.get("unrealized_pnl_pct", 0)) * 100

        # ATR proxy: 2% of current price — placeholder until real ATR available
        atr_proxy  = current_price * 0.02
        stop_price = round(entry_price - CONFIG.risk.initial_stop_atr_mult * atr_proxy, 4)

        metadata = {
            "stop":             stop_price,
            "regime":           "BACKFILL",   # Signals real stop/regime unknown
            "t_stat":           None,
            "kelly_fraction":   None,
            "composite_score":  None,
            "note":             "Backfilled from Alpaca — entry_date approximate",
        }

        to_write.append({
            "model":        "v5.4",
            "symbol":       sym,
            "shares":       qty,
            "entry_price":  entry_price,
            "entry_date":   str(date.today()),
            "metadata":     metadata,
            "pnl_pct":      round(pnl_pct, 2),
        })

    print(f"\n{'='*55}")
    print(f"  LEDGER BACKFILL {'(DRY RUN)' if not write else '(WRITING)'}")
    print(f"{'='*55}")

    if skipped:
        print(f"\n  Already in ledger (skipped): {', '.join(skipped)}")

    if not to_write:
        print("\n  Nothing new to write.")
        return

    print(f"\n  Will write {len(to_write)} position(s):\n")
    for r in to_write:
        stop = r["metadata"]["stop"]
        print(f"    {r['symbol']:6s}  {r['shares']} shares @ ${r['entry_price']:.2f}"
              f"  pnl={r['pnl_pct']:+.1f}%  est_stop=${stop:.2f}"
              f"  entry_date={r['entry_date']}")

    if not write:
        print("\n  Run with --write to commit these to position_ledger.json")
        return

    for r in to_write:
        ledger.record_entry(
            model      = r["model"],
            symbol     = r["symbol"],
            shares     = r["shares"],
            entry_price= r["entry_price"],
            date       = r["entry_date"],
            metadata   = r["metadata"],
        )
        print(f"  Written: {r['symbol']}")

    print(f"\n  Done. {len(to_write)} position(s) written to position_ledger.json")
    print(f"  Note: entry_date is today ({date.today()}). Stop prices are ATR estimates.")
    print(f"  Regime shows 'BACKFILL' until hold_monitor runs and updates hold_health.json.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true",
                        help="Commit backfill to ledger (default is dry run)")
    args = parser.parse_args()
    backfill(write=args.write)
