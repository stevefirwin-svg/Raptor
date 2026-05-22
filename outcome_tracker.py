"""
outcome_tracker.py — Layer 1: Outcome Tagging
Raptor Autonomous Agent Roadmap

Pulls closed trades from Alpaca, matches them to the last EntryAgent + HoldAgent
decisions before exit, and writes tagged records to outcome_log.json.

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
POSITION_LEDGER_PATH = "position_ledger.json"

# ── Helpers ───────────────────────────────────────────────────────────────────

def alpaca_headers():
    return {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }


def parse_ts(ts_str: str) -> datetime:
    """Parse any ISO timestamp into an offset-aware UTC datetime."""
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_closed_orders(limit: int = 500) -> list:
    url = f"{BASE_URL}/v2/orders"
    params = {"status": "filled", "limit": limit, "direction": "asc"}
    resp = requests.get(url, headers=alpaca_headers(), params=params, timeout=10)
    resp.raise_for_status()
    return [o for o in resp.json() if o.get("side") == "sell"]


def fetch_buy_for_symbol(symbol: str, before_ts: str):
    url = f"{BASE_URL}/v2/orders"
    params = {"status": "filled", "symbols": symbol, "limit": 50, "direction": "desc"}
    resp = requests.get(url, headers=alpaca_headers(), params=params, timeout=10)
    resp.raise_for_status()
    orders = resp.json()

    before_dt = parse_ts(before_ts)
    buys = []
    for o in orders:
        if o.get("side") != "buy":
            continue
        try:
            if parse_ts(o["filled_at"]) < before_dt:
                buys.append(o)
        except (KeyError, ValueError):
            continue

    if not buys:
        return None
    buys.sort(key=lambda o: o["filled_at"], reverse=True)
    return buys[0]


def load_json_list(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def last_decision_before(decisions: list, symbol: str, before_ts: str):
    """Return last agent decision for symbol strictly before before_ts."""
    before_dt = parse_ts(before_ts)
    candidates = []
    for d in decisions:
        if d.get("symbol") != symbol:
            continue
        if "decision" not in d:          # malformed early record — skip
            continue
        try:
            ts = parse_ts(d["timestamp"])
        except (KeyError, ValueError):
            continue
        if ts < before_dt:
            candidates.append((ts, d))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def normalize_decision(record, agent_type: str) -> dict:
    prefix = agent_type
    if record is None:
        base = {
            f"{prefix}_decision":   None,
            f"{prefix}_confidence": None,
            f"{prefix}_reasoning":  None,
            f"{prefix}_flags":      [],
            f"{prefix}_timestamp":  None,
        }
        if agent_type == "hold":
            base[f"{prefix}_trim_pct"] = None
        return base

    base = {
        f"{prefix}_decision":   record.get("decision"),
        f"{prefix}_confidence": record.get("confidence"),
        f"{prefix}_reasoning":  record.get("reasoning") or record.get("veto_reason"),
        f"{prefix}_flags":      record.get("flags", []),
        f"{prefix}_timestamp":  record.get("timestamp"),
    }
    if agent_type == "hold":
        base[f"{prefix}_trim_pct"] = record.get("trim_pct")
    return base


# ── Core tagging ──────────────────────────────────────────────────────────────

def load_ledger_exit_map() -> dict:
    """
    Build a map of symbol -> exit_reason from position_ledger.json closed trades.
    Used as fallback when client_order_id is missing or blank (legacy orders,
    manual exits, or Alpaca paper trading truncation).
    Key: symbol (str), Value: exit_reason (str)
    Only the most recent closed entry per symbol is kept.
    """
    if not os.path.exists(POSITION_LEDGER_PATH):
        return {}
    try:
        with open(POSITION_LEDGER_PATH) as f:
            ledger = json.load(f)
        closed = ledger.get("closed", [])
        result = {}
        for entry in closed:
            sym = entry.get("symbol")
            reason = entry.get("exit_reason") or entry.get("metadata", {}).get("exit_reason")
            if sym and reason:
                result[sym] = reason  # last writer wins — most recent exit
        return result
    except Exception:
        return {}


def build_outcome_record(sell_order, buy_order, entry_decision, hold_decision) -> dict:
    symbol     = sell_order["symbol"]
    exit_ts    = sell_order["filled_at"]
    exit_price = float(sell_order.get("filled_avg_price") or 0)
    qty        = float(sell_order.get("filled_qty") or 0)

    if buy_order:
        entry_ts    = buy_order["filled_at"]
        entry_price = float(buy_order.get("filled_avg_price") or 0)
        hold_days   = (parse_ts(exit_ts) - parse_ts(entry_ts)).days
        pnl_pct     = ((exit_price - entry_price) / entry_price * 100) if entry_price else None
    else:
        entry_ts    = None
        entry_price = None
        hold_days   = None
        pnl_pct     = None

    # Detect exit path from client_order_id label written by exit_monitor.
    # Falls back to position_ledger.json exit_reason when client_order_id is
    # absent (legacy orders, manual exits, Alpaca paper trading truncation).
    client_id = sell_order.get("client_order_id", "") or ""
    exit_path = "unknown"
    exit_path_labels = [
        "hard_stop", "trail_profit", "trail_loss", "profit_target",
        "momentum_break", "agent_exit", "trailing_stop", "thesis_invalid",
        "leveraged_3x_cap", "leveraged_2x_cap", "time_decay", "portfolio_heat"
    ]
    for path in exit_path_labels:
        if path in client_id:
            exit_path = path
            break

    # Ledger fallback — only if client_order_id gave us nothing
    if exit_path == "unknown":
        _ledger_map = load_ledger_exit_map()
        ledger_reason = _ledger_map.get(symbol, "")
        for path in exit_path_labels:
            if path in ledger_reason:
                exit_path = path
                break
        # Accept raw ledger reason string even if it doesn't match a canonical label
        if exit_path == "unknown" and ledger_reason:
            exit_path = ledger_reason

    return {
        "symbol":           symbol,
        "entry_date":       entry_ts,
        "exit_date":        exit_ts,
        "hold_days":        hold_days,
        "entry_price":      entry_price,
        "exit_price":       exit_price,
        "qty":              qty,
        "actual_pnl_pct":   round(pnl_pct, 4) if pnl_pct is not None else None,
        "actual_exit_path": exit_path,
        "sell_order_id":    sell_order.get("id"),
        **normalize_decision(entry_decision, "entry"),
        **normalize_decision(hold_decision,  "hold"),
        "tagged_at": datetime.now(timezone.utc).isoformat(),
    }


def run_tracker(verbose: bool = True) -> int:
    existing  = load_json_list(OUTCOME_LOG_PATH)
    tagged_ids = {r["sell_order_id"] for r in existing if r.get("sell_order_id")}

    entry_vetoes   = load_json_list(ENTRY_VETOES_PATH)
    hold_decisions = load_json_list(HOLD_DECISIONS_PATH)

    if verbose:
        print(f"[OutcomeTracker] Existing tagged trades : {len(existing)}")
        print(f"[OutcomeTracker] Entry veto records     : {len(entry_vetoes)}")
        print(f"[OutcomeTracker] Hold decision records  : {len(hold_decisions)}")

    try:
        sells = fetch_closed_orders()
    except Exception as e:
        print(f"[OutcomeTracker] ERROR fetching Alpaca orders: {e}")
        return 0

    if verbose:
        print(f"[OutcomeTracker] Filled sell orders from Alpaca: {len(sells)}")

    new_records = []
    for sell in sells:
        order_id = sell.get("id")
        if order_id in tagged_ids:
            continue

        symbol  = sell["symbol"]
        exit_ts = sell.get("filled_at")
        if not exit_ts:
            continue

        try:
            buy = fetch_buy_for_symbol(symbol, exit_ts)
        except Exception as e:
            print(f"[OutcomeTracker] WARNING: Could not fetch buy for {symbol}: {e}")
            buy = None

        entry_dec = last_decision_before(entry_vetoes,   symbol, exit_ts)
        hold_dec  = last_decision_before(hold_decisions, symbol, exit_ts)
        record    = build_outcome_record(sell, buy, entry_dec, hold_dec)
        new_records.append(record)

        if verbose:
            pnl_str = f"{record['actual_pnl_pct']:+.2f}%" if record["actual_pnl_pct"] is not None else "N/A"
            e_dec   = record.get("entry_decision") or "no_record"
            h_dec   = record.get("hold_decision")  or "no_record"
            print(f"  [+] {symbol:8s} | exit: {exit_ts[:10]} | pnl: {pnl_str:8s} | "
                  f"path: {record['actual_exit_path']:22s} | entry: {e_dec:5s} | hold: {h_dec}")

    if new_records:
        all_records = existing + new_records
        with open(OUTCOME_LOG_PATH, "w") as f:
            json.dump(all_records, f, indent=2)
        print(f"\n[OutcomeTracker] Tagged {len(new_records)} new trade(s) → {OUTCOME_LOG_PATH}")
    else:
        print("[OutcomeTracker] No new trades to tag.")

    return len(new_records)


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary():
    records = load_json_list(OUTCOME_LOG_PATH)
    if not records:
        print("[OutcomeTracker] outcome_log.json is empty — no closed trades yet.")
        return

    from collections import defaultdict

    print(f"\n{'='*62}")
    print(f"  OUTCOME LOG SUMMARY — {len(records)} tagged trades")
    print(f"{'='*62}")

    by_path = defaultdict(list)
    for r in records:
        if r.get("actual_pnl_pct") is not None:
            by_path[r["actual_exit_path"]].append(r["actual_pnl_pct"])

    print("\n  Exit path breakdown:")
    for path, pnls in sorted(by_path.items()):
        wins = sum(1 for p in pnls if p > 0)
        avg  = sum(pnls) / len(pnls)
        print(f"    {path:24s} | n={len(pnls):3d} | win%={wins/len(pnls)*100:4.0f}% | avg_pnl={avg:+.2f}%")

    print("\n  EntryAgent decision outcomes:")
    for dec in ["PASS", "VETO", None]:
        sub = [r for r in records if r.get("entry_decision") == dec and r.get("actual_pnl_pct") is not None]
        if not sub:
            continue
        pnls = [r["actual_pnl_pct"] for r in sub]
        wins = sum(1 for p in pnls if p > 0)
        print(f"    {(dec or 'no_record'):12s} | n={len(pnls):3d} | win%={wins/len(pnls)*100:4.0f}% | avg_pnl={sum(pnls)/len(pnls):+.2f}%")

    print("\n  HoldAgent last decision outcomes:")
    for dec in ["HOLD", "TRIM", "EXIT", None]:
        sub = [r for r in records if r.get("hold_decision") == dec and r.get("actual_pnl_pct") is not None]
        if not sub:
            continue
        pnls = [r["actual_pnl_pct"] for r in sub]
        wins = sum(1 for p in pnls if p > 0)
        print(f"    {(dec or 'no_record'):12s} | n={len(pnls):3d} | win%={wins/len(pnls)*100:4.0f}% | avg_pnl={sum(pnls)/len(pnls):+.2f}%")

    print(f"\n{'='*62}\n")


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
