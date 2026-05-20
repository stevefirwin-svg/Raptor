"""
outcome_tracker.py — Layer 1: Outcome Tagging
Raptor Autonomous Agent Roadmap

Pulls closed trades from Alpaca, matches them to the last EntryAgent + HoldAgent
decisions before exit, and writes tagged records to outcome_log.json.

This is the labeled dataset that feeds Layer 3 (Prompt Calibration).

Run:
    python outcome_tracker.py              # tag all untagged closed trades
    python outcome_tracker.py --summary    # print outcome_log.json summary

Called automatically at the end of exit_monitor.py after any execution.
"""

import os
import json
import argparse
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

ALPACA_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY")
BASE_URL      = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

ENTRY_VETOES_PATH   = "entry_vetoes.json"
HOLD_DECISIONS_PATH = "hold_decisions.json"
OUTCOME_LOG_PATH    = "outcome_log.json"

# ── Alpaca helpers ────────────────────────────────────────────────────────────

def alpaca_headers():
    return {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }


def fetch_closed_orders(limit: int = 500) -> list[dict]:
    """
    Fetch filled orders from Alpaca. Returns list of sell-side fills
    (status=filled, side=sell), sorted oldest-first.
    """
    url = f"{BASE_URL}/v2/orders"
    params = {
        "status": "filled",
        "limit":  limit,
        "direction": "asc",
    }
    resp = requests.get(url, headers=alpaca_headers(), params=params, timeout=10)
    resp.raise_for_status()
    orders = resp.json()

    sells = [o for o in orders if o.get("side") == "sell"]
    return sells


def fetch_buy_for_symbol(symbol: str, before_ts: str) -> dict | None:
    """
    Find the most recent filled buy order for a symbol before a given timestamp.
    Used to determine entry price and entry date.
    """
    url = f"{BASE_URL}/v2/orders"
    params = {
        "status":    "filled",
        "symbols":   symbol,
        "limit":     50,
        "direction": "desc",
    }
    resp = requests.get(url, headers=alpaca_headers(), params=params, timeout=10)
    resp.raise_for_status()
    orders = resp.json()

    before_dt = datetime.fromisoformat(before_ts.replace("Z", "+00:00"))
    buys = [
        o for o in orders
        if o.get("side") == "buy"
        and datetime.fromisoformat(o["filled_at"].replace("Z", "+00:00")) < before_dt
    ]
    if not buys:
        return None
    # Return the most recent buy before the sell
    buys.sort(key=lambda o: o["filled_at"], reverse=True)
    return buys[0]

# ── Decision log helpers ──────────────────────────────────────────────────────

def load_json_list(path: str) -> list[dict]:
    """Load a JSON array file. Returns [] if missing or corrupt."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def last_decision_before(decisions: list[dict], symbol: str, before_ts: str) -> dict | None:
    """
    Return the last decision record for a given symbol whose timestamp
    is strictly before before_ts. Skips records missing a 'decision' field.
    """
    before_dt = datetime.fromisoformat(before_ts.replace("Z", "+00:00"))

    candidates = []
    for d in decisions:
        if d.get("symbol") != symbol:
            continue
        if "decision" not in d:          # malformed early records — skip
            continue
        try:
            ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if ts < before_dt:
            candidates.append((ts, d))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def normalize_decision(record: dict | None, agent_type: str) -> dict:
    """
    Normalize an agent decision record into a flat dict for outcome_log.
    Handles missing fields gracefully.
    """
    if record is None:
        return {
            f"{agent_type}_decision":   None,
            f"{agent_type}_confidence": None,
            f"{agent_type}_reasoning":  None,
            f"{agent_type}_flags":      [],
            f"{agent_type}_timestamp":  None,
        }

    prefix = agent_type
    return {
        f"{prefix}_decision":   record.get("decision"),
        f"{prefix}_confidence": record.get("confidence"),
        f"{prefix}_reasoning":  record.get("reasoning") or record.get("veto_reason"),
        f"{prefix}_flags":      record.get("flags", []),
        f"{prefix}_timestamp":  record.get("timestamp"),
        # HoldAgent extras
        **( {f"{prefix}_trim_pct": record.get("trim_pct")} if agent_type == "hold" else {} ),
    }

# ── Core tagging logic ────────────────────────────────────────────────────────

def build_outcome_record(
    sell_order:      dict,
    buy_order:       dict | None,
    entry_decision:  dict | None,
    hold_decision:   dict | None,
) -> dict:
    """Assemble a single tagged outcome record."""

    symbol      = sell_order["symbol"]
    exit_ts     = sell_order["filled_at"]
    exit_price  = float(sell_order.get("filled_avg_price") or 0)
    qty         = float(sell_order.get("filled_qty") or 0)

    # Entry info — from matched buy order if available
    if buy_order:
        entry_ts    = buy_order["filled_at"]
        entry_price = float(buy_order.get("filled_avg_price") or 0)
        entry_qty   = float(buy_order.get("filled_qty") or 0)

        entry_dt = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
        exit_dt  = datetime.fromisoformat(exit_ts.replace("Z", "+00:00"))
        hold_days = (exit_dt - entry_dt).days

        pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else None
    else:
        entry_ts    = None
        entry_price = None
        entry_qty   = None
        hold_days   = None
        pnl_pct     = None

    # Determine exit path from order metadata if available
    # exit_monitor labels orders via client_order_id convention: e.g. "hard_stop_AAPL_..."
    client_id   = sell_order.get("client_order_id", "")
    exit_path   = "unknown"
    for path in ["hard_stop", "trail_profit", "trail_loss", "profit_target",
                 "momentum_break", "agent_exit"]:
        if path in client_id:
            exit_path = path
            break

    record = {
        "symbol":       symbol,
        "entry_date":   entry_ts,
        "exit_date":    exit_ts,
        "hold_days":    hold_days,
        "entry_price":  entry_price,
        "exit_price":   exit_price,
        "qty":          qty,
        "actual_pnl_pct":   round(pnl_pct, 4) if pnl_pct is not None else None,
        "actual_exit_path": exit_path,
        "sell_order_id":    sell_order.get("id"),
        **normalize_decision(entry_decision, "entry"),
        **normalize_decision(hold_decision,  "hold"),
        "tagged_at": datetime.now(timezone.utc).isoformat(),
    }
    return record


def run_tracker(verbose: bool = True) -> int:
    """
    Main tracking loop. Returns count of newly tagged trades.
    """
    # Load existing outcome log — build index of already-tagged sell order IDs
    existing_records = load_json_list(OUTCOME_LOG_PATH)
    tagged_ids = {r["sell_order_id"] for r in existing_records if r.get("sell_order_id")}

    # Load agent decision logs
    entry_vetoes   = load_json_list(ENTRY_VETOES_PATH)
    hold_decisions = load_json_list(HOLD_DECISIONS_PATH)

    if verbose:
        print(f"[OutcomeTracker] Loaded {len(existing_records)} existing tagged trades")
        print(f"[OutcomeTracker] Entry veto records: {len(entry_vetoes)}")
        print(f"[OutcomeTracker] Hold decision records: {len(hold_decisions)}")

    # Fetch closed orders from Alpaca
    try:
        sells = fetch_closed_orders()
    except Exception as e:
        print(f"[OutcomeTracker] ERROR fetching Alpaca orders: {e}")
        return 0

    if verbose:
        print(f"[OutcomeTracker] Fetched {len(sells)} filled sell orders from Alpaca")

    new_records = []
    for sell in sells:
        order_id = sell.get("id")
        if order_id in tagged_ids:
            continue  # Already tagged — skip (idempotent)

        symbol  = sell["symbol"]
        exit_ts = sell.get("filled_at")
        if not exit_ts:
            continue

        # Find matching buy order
        try:
            buy = fetch_buy_for_symbol(symbol, exit_ts)
        except Exception as e:
            print(f"[OutcomeTracker] WARNING: Could not fetch buy for {symbol}: {e}")
            buy = None

        # Match agent decisions
        entry_decision = last_decision_before(entry_vetoes,   symbol, exit_ts)
        hold_decision  = last_decision_before(hold_decisions, symbol, exit_ts)

        record = build_outcome_record(sell, buy, entry_decision, hold_decision)
        new_records.append(record)

        if verbose:
            pnl_str = f"{record['actual_pnl_pct']:+.2f}%" if record['actual_pnl_pct'] is not None else "N/A"
            print(f"  [+] Tagged {symbol:6s} | exit: {exit_ts[:10]} | "
                  f"pnl: {pnl_str:8s} | path: {record['actual_exit_path']:16s} | "
                  f"entry_agent: {record['entry_decision'] or 'no_record':5s} | "
                  f"hold_agent: {record['hold_decision'] or 'no_record':5s}")

    if new_records:
        all_records = existing_records + new_records
        with open(OUTCOME_LOG_PATH, "w") as f:
            json.dump(all_records, f, indent=2)
        print(f"[OutcomeTracker] Tagged {len(new_records)} new trades → {OUTCOME_LOG_PATH}")
    else:
        print("[OutcomeTracker] No new trades to tag.")

    return len(new_records)


# ── Summary report ────────────────────────────────────────────────────────────

def print_summary():
    records = load_json_list(OUTCOME_LOG_PATH)
    if not records:
        print("[OutcomeTracker] outcome_log.json is empty.")
        return

    print(f"\n{'='*60}")
    print(f"  OUTCOME LOG SUMMARY — {len(records)} tagged trades")
    print(f"{'='*60}")

    # PnL by exit path
    from collections import defaultdict
    by_path = defaultdict(list)
    for r in records:
        if r.get("actual_pnl_pct") is not None:
            by_path[r["actual_exit_path"]].append(r["actual_pnl_pct"])

    print("\n  Exit path breakdown:")
    for path, pnls in sorted(by_path.items()):
        wins  = sum(1 for p in pnls if p > 0)
        avg   = sum(pnls) / len(pnls)
        print(f"    {path:20s} | n={len(pnls):3d} | win%={wins/len(pnls)*100:4.0f}% | avg_pnl={avg:+.2f}%")

    # Entry agent: VETO vs PASS outcomes
    print("\n  EntryAgent decision outcomes:")
    for decision in ["PASS", "VETO", None]:
        subset = [r for r in records if r.get("entry_decision") == decision
                  and r.get("actual_pnl_pct") is not None]
        if not subset:
            continue
        pnls  = [r["actual_pnl_pct"] for r in subset]
        wins  = sum(1 for p in pnls if p > 0)
        avg   = sum(pnls) / len(pnls)
        label = decision or "no_record"
        print(f"    {label:12s} | n={len(pnls):3d} | win%={wins/len(pnls)*100:4.0f}% | avg_pnl={avg:+.2f}%")

    # Hold agent: last decision before exit
    print("\n  HoldAgent last decision outcomes:")
    for decision in ["HOLD", "TRIM", "EXIT", None]:
        subset = [r for r in records if r.get("hold_decision") == decision
                  and r.get("actual_pnl_pct") is not None]
        if not subset:
            continue
        pnls  = [r["actual_pnl_pct"] for r in subset]
        wins  = sum(1 for p in pnls if p > 0)
        avg   = sum(pnls) / len(pnls)
        label = decision or "no_record"
        print(f"    {label:12s} | n={len(pnls):3d} | win%={wins/len(pnls)*100:4.0f}% | avg_pnl={avg:+.2f}%")

    print(f"\n{'='*60}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Raptor Outcome Tracker — Layer 1")
    parser.add_argument("--summary", action="store_true", help="Print outcome_log.json summary")
    parser.add_argument("--quiet",   action="store_true", help="Suppress per-trade output")
    args = parser.parse_args()

    if args.summary:
        print_summary()
    else:
        n = run_tracker(verbose=not args.quiet)
        if n > 0:
            print_summary()
