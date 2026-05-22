"""
reconcile_positions.py — Live Position Reconciliation
======================================================
Compares Alpaca live positions against all local JSON files and
identifies every discrepancy. Run when position files may be stale.

Output:
  1. Live Alpaca positions (source of truth)
  2. What's in position_ledger.json (should match)
  3. What's in hold_health.json (should match)
  4. What's in hold_history.json (should match)
  5. Discrepancy report — what's missing, extra, or mismatched
  6. Recommended actions

Usage:
  python reconcile_positions.py
  python reconcile_positions.py --fix   # auto-runs backfill_ledger if discrepancies found
"""

import json
import os
import sys
import argparse
from datetime import datetime
from pathlib import Path

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true", help="Run backfill_ledger if discrepancies found")
    args = parser.parse_args()

    print("=" * 70)
    print("  RAPTOR POSITION RECONCILIATION")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # ── 1. Live Alpaca positions (source of truth) ─────────────────────────
    try:
        from config import CONFIG
        from data_feeds import DataManager
        dm = DataManager(CONFIG)
        alpaca_positions = dm.alpaca.get_positions()
        account = dm.alpaca.get_account()
        equity = float(account.get("equity", 0))
    except Exception as e:
        print(f"\nERROR: Cannot connect to Alpaca — {e}")
        sys.exit(1)

    alpaca_syms = {p["symbol"] for p in alpaca_positions}
    print(f"\n  ALPACA LIVE (source of truth): {len(alpaca_positions)} positions")
    print(f"  Account equity: ${equity:,.2f}")
    print()

    total_unrealized = 0
    for p in sorted(alpaca_positions, key=lambda x: float(x.get("unrealized_pnl_pct", 0) or 0), reverse=True):
        sym     = p["symbol"]
        qty     = float(p.get("qty", 0))
        entry   = float(p.get("avg_entry", 0))
        price   = float(p.get("current_price", 0))
        pnl_pct = float(p.get("unrealized_pnl_pct", 0)) * 100
        pnl_usd = float(p.get("unrealized_pnl", 0))
        total_unrealized += pnl_usd
        print(f"    {sym:8s}  qty={qty:6.0f}  entry=${entry:7.2f}  now=${price:7.2f}  pnl={pnl_pct:+6.2f}%  ${pnl_usd:+8.2f}")

    print(f"\n    Total unrealized P&L: ${total_unrealized:+,.2f}")

    # ── 2. position_ledger.json ────────────────────────────────────────────
    ledger_data = load_json("position_ledger.json")
    if ledger_data:
        ledger_open  = ledger_data.get("positions", {})
        ledger_syms  = {v.get("symbol", k.split(":")[-1]) for k, v in ledger_open.items()}
        ledger_closed = ledger_data.get("closed", [])
        print(f"\n  POSITION_LEDGER.JSON: {len(ledger_open)} open, {len(ledger_closed)} closed")
        # Find latest timestamp
        timestamps = [v.get("entry_date", "") for v in ledger_open.values()]
        latest = max((t for t in timestamps if t), default="unknown")
        print(f"  Latest entry_date: {latest[:19]}")
    else:
        ledger_syms = set()
        ledger_closed = []
        print(f"\n  POSITION_LEDGER.JSON: NOT FOUND or unreadable")

    # ── 3. hold_health.json ────────────────────────────────────────────────
    hh_data = load_json("hold_health.json")
    if hh_data:
        hh_syms = set(hh_data.keys())
        hh_timestamps = [v.get("timestamp", "") for v in hh_data.values() if isinstance(v, dict)]
        hh_latest = max((t for t in hh_timestamps if t), default="unknown")
        print(f"\n  HOLD_HEALTH.JSON: {len(hh_syms)} symbols, last updated {hh_latest[:19]}")
    else:
        hh_syms = set()
        print(f"\n  HOLD_HEALTH.JSON: NOT FOUND")

    # ── 4. hold_history.json ───────────────────────────────────────────────
    hist_data = load_json("hold_history.json")
    if hist_data:
        hist_syms = set(hist_data.get("positions", {}).keys())
        hist_snaps = {s: hist_data["positions"][s].get("snapshots", []) for s in hist_syms}
        hist_latest_per_sym = {s: snaps[-1].get("timestamp", "unknown")[:19]
                               for s, snaps in hist_snaps.items() if snaps}
        overall_latest = max(hist_latest_per_sym.values(), default="unknown") if hist_latest_per_sym else "unknown"
        print(f"\n  HOLD_HISTORY.JSON: {len(hist_syms)} symbols, last updated {overall_latest}")
    else:
        hist_syms = set()
        print(f"\n  HOLD_HISTORY.JSON: NOT FOUND")

    # ── 5. Discrepancy report ──────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  DISCREPANCY REPORT")
    print(f"{'='*70}")

    # In Alpaca but not in ledger
    missing_from_ledger = alpaca_syms - ledger_syms
    # In ledger but not in Alpaca (already exited but ledger not updated)
    ghost_in_ledger = ledger_syms - alpaca_syms
    # In Alpaca but not in hold_health
    missing_from_health = alpaca_syms - hh_syms
    # In hold_health but not in Alpaca (stale)
    stale_in_health = hh_syms - alpaca_syms
    # In Alpaca but not in hold_history
    missing_from_history = alpaca_syms - hist_syms
    # In hold_history but not in Alpaca
    stale_in_history = hist_syms - alpaca_syms

    all_clean = True

    if missing_from_ledger:
        all_clean = False
        print(f"\n  ❌ In Alpaca but NOT in position_ledger (need backfill):")
        for s in sorted(missing_from_ledger):
            print(f"     {s}")

    if ghost_in_ledger:
        all_clean = False
        print(f"\n  ❌ In position_ledger but NOT in Alpaca (already exited, ledger stale):")
        for s in sorted(ghost_in_ledger):
            print(f"     {s}")

    if missing_from_health:
        all_clean = False
        print(f"\n  ⚠  In Alpaca but NOT in hold_health (hold_monitor hasn't run or stale):")
        for s in sorted(missing_from_health):
            print(f"     {s}")

    if stale_in_health:
        all_clean = False
        print(f"\n  ⚠  In hold_health but NOT in Alpaca (exited positions still in health file):")
        for s in sorted(stale_in_health):
            hh_rec = hh_data.get(s, {})
            pnl = hh_rec.get("pnl_pct", "?")
            ts  = hh_rec.get("timestamp", "?")[:19]
            print(f"     {s}  (last health: {ts}, pnl={pnl}%)")

    if stale_in_history:
        all_clean = False
        print(f"\n  ⚠  In hold_history but NOT in Alpaca (exited, history not cleaned):")
        for s in sorted(stale_in_history):
            last_snap_ts = hist_latest_per_sym.get(s, "?")
            n_snaps = len(hist_snaps.get(s, []))
            print(f"     {s}  ({n_snaps} snapshots, last: {last_snap_ts})")

    if all_clean:
        print(f"\n  ✓ All files are consistent with Alpaca live positions.")
    else:
        print(f"\n  Summary of live Alpaca positions: {sorted(alpaca_syms)}")
        print(f"  Summary of ledger positions:      {sorted(ledger_syms)}")
        print(f"  Summary of hold_health symbols:   {sorted(hh_syms)}")

    # ── 6. Recommended actions ─────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  RECOMMENDED ACTIONS")
    print(f"{'='*70}")

    if missing_from_ledger or ghost_in_ledger:
        print(f"\n  1. SYNC LEDGER with Alpaca:")
        print(f"     python backfill_ledger.py --write")
        print(f"     This will add missing positions and close exited ones.")

    if missing_from_health or stale_in_health:
        print(f"\n  2. RE-RUN HOLD MONITOR to rebuild health scores:")
        print(f"     python hold_monitor.py")

    if not missing_from_ledger and not ghost_in_ledger and not stale_in_health and not missing_from_health:
        print(f"\n  ✓ No action needed — all files are current.")

    # ── 7. Closed trades summary ───────────────────────────────────────────
    if ledger_closed:
        print(f"\n{'='*70}")
        print(f"  CLOSED TRADES (position_ledger.json) — {len(ledger_closed)} total")
        print(f"{'='*70}")
        # Show last 10
        recent = sorted(ledger_closed, key=lambda x: x.get("exit_date",""), reverse=True)[:10]
        print(f"\n  {'Symbol':8s}  {'Entry':10s}  {'Exit':10s}  {'PnL%':>7s}  {'Reason':20s}")
        print(f"  {'-'*65}")
        for t in recent:
            sym   = t.get("symbol", "?")
            ed    = t.get("entry_date", "?")[:10]
            xd    = t.get("exit_date",  "?")[:10]
            ep    = float(t.get("entry_price", 0) or 0)
            xp    = float(t.get("exit_price",  0) or 0)
            pnl   = ((xp - ep) / ep * 100) if ep > 0 else 0
            reason = t.get("exit_reason", "?")[:20]
            print(f"  {sym:8s}  {ed:10s}  {xd:10s}  {pnl:+7.2f}%  {reason:20s}")

    print(f"\n{'='*70}\n")

if __name__ == "__main__":
    main()
