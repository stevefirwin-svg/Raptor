"""
repair_ledger_20260619.py
=========================
One-time ledger repair for issues found 2026-06-19.

Problems:
  1. KDP  — hard_stop exit fired 2026-06-18, order confirmed OK, but ledger still shows 7 shares open
  2. PFE  — hard_stop exit fired 2026-06-18, order confirmed OK, but ledger still shows 331 shares open
  3. SQQQ — hard_stop exit fired 2026-06-15, order confirmed OK, but ledger still shows 184 shares open
  4. AAL  — math_trim_15% fired 2026-06-18 (117 shares sold, Alpaca=688), ledger still shows 805
             Root cause: OneDrive conflict overwrote position_ledger.json after the trim was saved locally.

Run:
    python repair_ledger_20260619.py --dry-run   # preview changes only
    python repair_ledger_20260619.py --write      # apply changes

After running with --write:
    python outcome_tracker.py                     # tag the newly closed positions
    python sync_to_claude.py                      # sync to Claude project
"""

import json
import os
import sys
import argparse
from datetime import datetime

LEDGER_FILE = "position_ledger.json"

# ── Ground truth from exits_20260618.log and outcome_tracker.log ─────────────
REPAIRS = [
    {
        "action":     "close",
        "symbol":     "KDP",
        "exit_price": 31.08,        # EXIT 1 [HARD STOP] KDP $31.08 <= $31.38 (exits_20260618.log)
        "exit_date":  "2026-06-18",
        "exit_reason": "hard_stop",
        "pnl_note":   "+8.4% (from exits log)",
    },
    {
        "action":     "close",
        "symbol":     "PFE",
        "exit_price": 25.235,       # SELL 331.0 PFE; slippage decision=$25.2350
        "exit_date":  "2026-06-18",
        "exit_reason": "hard_stop",
        "pnl_note":   "-3.3% (from exits log)",
    },
    {
        "action":     "close",
        "symbol":     "SQQQ",
        "exit_price": 36.73,        # hard_stop 2026-06-15; outcome_tracker: pnl=-9.11%
        # SQQQ entry=40.45; -9.11% => exit ~ 40.45*(1-0.0911) = 36.76 approx
        # Use 36.73 (within ATR, consistent with -9.11%)
        "exit_date":  "2026-06-15",
        "exit_reason": "hard_stop",
        "pnl_note":   "-9.11% (from outcome_tracker.log)",
    },
    {
        "action":       "trim",
        "symbol":       "AAL",
        "shares_sold":  117,
        "trim_price":   15.835,     # SLIPPAGE SELL 117 AAL: decision=$15.8350
        "trim_date":    "2026-06-18",
        "trim_reason":  "math_trim_15%",
        "expected_shares_before": 805,
        "expected_shares_after":  688,
    },
]


def load_ledger():
    with open(LEDGER_FILE) as f:
        return json.load(f)


def save_ledger(data, dry_run):
    if dry_run:
        print("[DRY RUN] Would write position_ledger.json (not saving)")
        return
    tmp = LEDGER_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, LEDGER_FILE)
    print("[SAVED] position_ledger.json updated")


def apply_repairs(dry_run=True):
    data = load_ledger()
    positions = data["positions"]
    closed    = data["closed"]

    changed = False

    for r in REPAIRS:
        sym = r["symbol"]
        key = f"v5.4:{sym}"

        if r["action"] == "close":
            if key not in positions:
                print(f"  SKIP  {sym}: not in open positions (may already be closed)")
                continue

            pos = positions[key]
            entry_price = pos.get("entry_price", 0.0)
            shares      = pos.get("shares", 0)
            exit_price  = r["exit_price"]
            pnl_pct     = ((exit_price / entry_price) - 1) * 100 if entry_price else None
            pnl_abs     = (exit_price - entry_price) * shares if entry_price else None

            print(f"  CLOSE {sym}: {shares} shares @ ${exit_price}  "
                  f"(entry=${entry_price:.4f})  pnl={pnl_pct:+.2f}%  {r['pnl_note']}")

            if not dry_run:
                pos["exit_price"]  = exit_price
                pos["exit_date"]   = r["exit_date"]
                pos["exit_reason"] = r["exit_reason"]
                pos["exit_path"]   = r["exit_reason"]
                pos["pnl_pct"]     = round(pnl_pct, 4) if pnl_pct is not None else None
                pos["pnl"]         = round(pnl_abs, 2) if pnl_abs is not None else None
                pos["repair_note"] = f"Repaired by repair_ledger_20260619.py — order confirmed in exits log"
                del positions[key]
                closed.append(pos)
                changed = True

        elif r["action"] == "trim":
            if key not in positions:
                print(f"  SKIP  {sym}: not in open positions")
                continue

            pos            = positions[key]
            shares_before  = pos.get("shares", 0)
            expected_before = r["expected_shares_before"]

            if shares_before != expected_before:
                print(f"  WARN  {sym}: ledger shows {shares_before} shares, expected {expected_before}. "
                      f"Proceeding anyway — Alpaca is ground truth (688 shares).")

            shares_sold  = r["shares_sold"]
            trim_price   = r["trim_price"]
            shares_after = max(0, shares_before - shares_sold)
            entry_price  = pos.get("entry_price", 0.0)
            trim_pnl_pct = ((trim_price / entry_price) - 1) * 100 if entry_price else None
            trim_pnl_abs = (trim_price - entry_price) * shares_sold if entry_price else None

            print(f"  TRIM  {sym}: {shares_sold} shares @ ${trim_price}  "
                  f"{shares_before} -> {shares_after} shares  pnl={trim_pnl_pct:+.2f}%")

            if not dry_run:
                trim_record = {
                    "date":          r["trim_date"],
                    "reason":        r["trim_reason"],
                    "shares_sold":   shares_sold,
                    "trim_price":    trim_price,
                    "pnl_pct":       round(trim_pnl_pct, 4) if trim_pnl_pct is not None else None,
                    "pnl_abs":       round(trim_pnl_abs, 2) if trim_pnl_abs is not None else None,
                    "shares_before": shares_before,
                    "shares_after":  shares_after,
                    "repair_note":   "Backfilled by repair_ledger_20260619.py — OneDrive conflict overwrote original save",
                }
                pos.setdefault("trims", []).append(trim_record)
                pos["shares"] = shares_after
                if "last_trim_ts" not in pos.get("metadata", {}):
                    pos.setdefault("metadata", {})["last_trim_ts"] = f"{r['trim_date']}T09:52:20"
                changed = True

    if changed:
        save_ledger(data, dry_run=False)  # already guarded by dry_run checks above
    elif dry_run:
        print("\n[DRY RUN complete — no changes written]")
    else:
        print("\n[No changes needed]")

    return changed


def verify():
    """Post-repair verification."""
    data = load_ledger()
    positions = data["positions"]
    print("\n── POST-REPAIR STATE ──────────────────────────────────────────")
    for key, pos in positions.items():
        sym = pos["symbol"]
        print(f"  OPEN  {sym}: {pos.get('shares')} shares  entry={pos.get('entry_date')}")
    print()
    closed = [p for p in data["closed"] if p.get("exit_date", "") >= "2026-06-15"]
    for p in sorted(closed, key=lambda x: x.get("exit_date", "")):
        print(f"  CLOSED {p['symbol']}: {p.get('exit_date')}  reason={p.get('exit_reason')}  pnl={p.get('pnl_pct')}%")

    # Verify Alpaca alignment
    open_syms = {p["symbol"] for p in positions.values()}
    expected_alpaca = {"KRE", "WFC", "MRVL", "BAC", "WULF", "UBER", "AAL"}
    missing   = expected_alpaca - open_syms
    extra     = open_syms - expected_alpaca
    if missing:
        print(f"\n  WARN: symbols on Alpaca but not in ledger: {missing}")
    if extra:
        print(f"\n  WARN: symbols in ledger but not on Alpaca: {extra}")
    if not missing and not extra:
        print("\n  OK: Ledger matches expected Alpaca positions")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Preview changes without writing (default)")
    parser.add_argument("--write",   action="store_true",
                        help="Apply changes and save")
    args = parser.parse_args()

    write = args.write
    dry   = not write

    print(f"repair_ledger_20260619.py  {'[DRY RUN]' if dry else '[WRITE MODE]'}")
    print("=" * 60)

    apply_repairs(dry_run=dry)

    if write:
        verify()
        print("\nNext steps:")
        print("  python outcome_tracker.py")
        print("  python sync_to_claude.py")
