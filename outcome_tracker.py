"""
Raptor — Outcome Tracker
=========================
Tags closed trades with exit path, agent decisions, and trade type.
Writes to outcome_log.json — the labeled dataset for all learning components.

Fixes applied 2026-05-24:
  - trade_type populated from position_ledger metadata (MOMENTUM/MEAN_REVERSION)
  - Legacy trades (trade_type=None) backfilled as MOMENTUM (all pre-v5.5 trades were)
  - Exit path resolution improved: ledger fallback tried first when client_order_id blank
  - regime_at_entry captured from ledger metadata for MATH-1 regime-conditional IC
  - Atomic writes: crash-safe JSON persistence
"""

import json
import os
import requests
import tempfile
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

BASE_DIR            = Path(__file__).parent
OUTCOME_LOG_PATH    = BASE_DIR / "outcome_log.json"
ENTRY_VETOES_PATH   = BASE_DIR / "entry_vetoes.json"
HOLD_DECISIONS_PATH = BASE_DIR / "hold_decisions.json"
POSITION_LEDGER_PATH= BASE_DIR / "position_ledger.json"

EXIT_PATH_LABELS = [
    "hard_stop", "trail_profit", "trail_loss", "profit_target",
    "momentum_break", "agent_exit", "trailing_stop", "thesis_invalid",
    "leveraged_3x_cap", "leveraged_2x_cap", "time_decay", "portfolio_heat",
    "math_exit", "math_trim",
]


# ── Atomic write ─────────────────────────────────────────────────────────────
def _atomic_write(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


# ── JSON helpers ─────────────────────────────────────────────────────────────
def load_json_list(path) -> list:
    try:
        p = Path(path)
        if p.exists():
            data = json.loads(p.read_text())
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return {}


def load_json_dict(path) -> dict:
    try:
        p = Path(path)
        if p.exists():
            data = json.loads(p.read_text())
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


# ── Alpaca API ────────────────────────────────────────────────────────────────
def _alpaca_headers() -> tuple:
    """
    Load Alpaca credentials. Supports both key naming conventions:
      .env  → ALPACA_API_KEY / ALPACA_SECRET_KEY  (primary)
      _env  → APCA_API_KEY_ID / APCA_API_SECRET_KEY (legacy)
    """
    from dotenv import load_dotenv
    for env_file in [BASE_DIR / ".env", BASE_DIR / "_env"]:
        if env_file.exists():
            load_dotenv(env_file, override=False)
    key    = (os.environ.get("APCA_API_KEY_ID") or
              os.environ.get("ALPACA_API_KEY") or "")
    secret = (os.environ.get("APCA_API_SECRET_KEY") or
              os.environ.get("ALPACA_SECRET_KEY") or "")
    base   = (os.environ.get("APCA_API_BASE_URL") or
              "https://paper-api.alpaca.markets")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}, base


def fetch_closed_orders(limit: int = 500) -> list:
    headers, base = _alpaca_headers()
    url = f"{base}/v2/orders"
    params = {"status": "closed", "limit": limit, "direction": "desc"}
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    orders = r.json()
    return [o for o in orders if o.get("side") == "sell" and o.get("filled_qty")]


def fetch_buy_for_symbol(symbol: str, before_ts: str, lookback_days: int = 90):
    """Find the most recent filled BUY order for symbol before exit timestamp."""
    headers, base = _alpaca_headers()
    url = f"{base}/v2/orders"
    cutoff = (datetime.fromisoformat(before_ts.replace("Z", "+00:00"))
              - timedelta(days=lookback_days)).isoformat()
    params = {
        "status": "closed", "symbols": symbol,
        "side": "buy", "limit": 50, "direction": "desc",
        "after": cutoff,
    }
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        orders = r.json()
        filled = [o for o in orders
                  if o.get("side") == "buy" and o.get("filled_qty")
                  and o.get("filled_at", "") < before_ts]
        return filled[0] if filled else None
    except Exception:
        return None


# ── Ledger helpers ────────────────────────────────────────────────────────────
def _build_ledger_maps() -> tuple:
    """
    Returns:
      exit_reason_map: symbol -> exit_reason (most recent closed entry)
      trade_type_map:  symbol -> trade_type  (from ledger metadata)
      regime_map:      symbol -> regime_at_entry
    """
    exit_reason_map, trade_type_map, regime_map = {}, {}, {}
    try:
        ledger = json.loads(POSITION_LEDGER_PATH.read_text())
        for entry in ledger.get("closed", []):
            sym = entry.get("symbol")
            if not sym:
                continue
            reason = entry.get("exit_reason") or entry.get("metadata", {}).get("exit_reason", "")
            trade_type = entry.get("metadata", {}).get("trade_type")
            regime     = entry.get("metadata", {}).get("regime", "")
            exit_reason_map[sym] = reason
            if trade_type:
                trade_type_map[sym] = trade_type
            if regime:
                regime_map[sym] = regime
    except Exception:
        pass
    return exit_reason_map, trade_type_map, regime_map


# ── Timestamp parse ───────────────────────────────────────────────────────────
def parse_ts(ts: str) -> datetime:
    """Parse timestamp — always returns timezone-aware UTC datetime."""
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Decision matching ─────────────────────────────────────────────────────────
def last_decision_before(decisions: list, symbol: str, before_ts: str) -> Optional[dict]:
    """Find most recent agent decision for symbol before timestamp."""
    cutoff = parse_ts(before_ts)
    matches = [
        d for d in decisions
        if d.get("symbol") == symbol
        and parse_ts(d.get("timestamp", "1970-01-01T00:00:00+00:00")) < cutoff
    ]
    return matches[-1] if matches else None


def normalize_decision(decision: Optional[dict], prefix: str) -> dict:
    if not decision:
        return {
            f"{prefix}_decision":  None,
            f"{prefix}_confidence": None,
            f"{prefix}_reasoning": None,
            f"{prefix}_flags":     [],
            f"{prefix}_timestamp": None,
        }
    return {
        f"{prefix}_decision":   decision.get("decision"),
        f"{prefix}_confidence": decision.get("confidence"),
        f"{prefix}_reasoning":  decision.get("reasoning"),
        f"{prefix}_flags":      decision.get("flags", []),
        f"{prefix}_timestamp":  decision.get("timestamp"),
    }


# ── Exit path detection ───────────────────────────────────────────────────────
def _detect_exit_path(sell_order: dict, ledger_exit_reason: str) -> str:
    """
    Resolve exit path from client_order_id label, falling back to ledger.
    client_order_id is set by exit_monitor as: f"{exit_reason}_{symbol}_{timestamp}"
    """
    client_id = sell_order.get("client_order_id", "") or ""
    for label in EXIT_PATH_LABELS:
        if label in client_id:
            return label

    # Ledger fallback
    if ledger_exit_reason:
        for label in EXIT_PATH_LABELS:
            if label in ledger_exit_reason:
                return label
        # Accept raw ledger string if it's non-empty
        if ledger_exit_reason.strip():
            return ledger_exit_reason.strip()

    return "unknown"


# ── Core record builder ───────────────────────────────────────────────────────
def build_outcome_record(sell_order, buy_order, entry_decision, hold_decision,
                          ledger_exit_reason="", trade_type=None, regime_at_entry="") -> dict:
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

    exit_path = _detect_exit_path(sell_order, ledger_exit_reason)

    # trade_type: use ledger metadata if available; default MOMENTUM for legacy pre-v5.5 trades
    resolved_type = trade_type or "MOMENTUM"

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
        "trade_type":       resolved_type,
        "regime_at_entry":  regime_at_entry,
        **normalize_decision(entry_decision, "entry"),
        **normalize_decision(hold_decision,  "hold"),
        "tagged_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Backfill existing records ─────────────────────────────────────────────────
def backfill_existing_records(existing: list,
                               exit_reason_map: dict,
                               trade_type_map: dict,
                               regime_map: dict) -> tuple:
    """
    Retroactively fix trade_type and exit_path on existing records.
    Returns (updated_records, n_changed).
    """
    changed = 0
    for rec in existing:
        dirty = False
        sym = rec.get("symbol", "")

        # Fix trade_type: None → MOMENTUM (all legacy trades are momentum)
        if rec.get("trade_type") in (None, "None", "?", ""):
            rec["trade_type"] = trade_type_map.get(sym, "MOMENTUM")
            dirty = True

        # Fix exit_path: unknown → try ledger
        if rec.get("actual_exit_path") in ("unknown", None, ""):
            ledger_reason = exit_reason_map.get(sym, "")
            if ledger_reason:
                for label in EXIT_PATH_LABELS:
                    if label in ledger_reason:
                        rec["actual_exit_path"] = label
                        dirty = True
                        break
                else:
                    if ledger_reason.strip():
                        rec["actual_exit_path"] = ledger_reason.strip()
                        dirty = True

        # Add regime_at_entry if missing
        if not rec.get("regime_at_entry"):
            regime = regime_map.get(sym, "")
            if regime:
                rec["regime_at_entry"] = regime
                dirty = True

        if dirty:
            changed += 1

    return existing, changed


# ── Main tracker ─────────────────────────────────────────────────────────────
def run_tracker(verbose: bool = True) -> int:
    existing     = load_json_list(OUTCOME_LOG_PATH)
    existing     = existing if isinstance(existing, list) else []
    tagged_ids   = {r["sell_order_id"] for r in existing if r.get("sell_order_id")}

    entry_vetoes   = load_json_list(ENTRY_VETOES_PATH)
    hold_decisions = load_json_list(HOLD_DECISIONS_PATH)
    exit_reason_map, trade_type_map, regime_map = _build_ledger_maps()

    # ── Backfill existing records first ──────────────────────────────────────
    existing, n_backfilled = backfill_existing_records(
        existing, exit_reason_map, trade_type_map, regime_map
    )
    if n_backfilled > 0 and verbose:
        print(f"[OutcomeTracker] Backfilled {n_backfilled} existing record(s) "
              f"(trade_type / exit_path / regime)")

    if verbose:
        print(f"[OutcomeTracker] Existing tagged trades : {len(existing)}")
        exit_unknown = sum(1 for r in existing if r.get("actual_exit_path") == "unknown")
        type_missing = sum(1 for r in existing if not r.get("trade_type"))
        print(f"[OutcomeTracker] Exit path unknown      : {exit_unknown}")
        print(f"[OutcomeTracker] Trade type missing     : {type_missing}")

    # ── Fetch new closed orders ───────────────────────────────────────────────
    try:
        sells = fetch_closed_orders()
    except Exception as e:
        print(f"[OutcomeTracker] ERROR fetching Alpaca orders: {e}")
        # Still save backfilled records even if Alpaca fetch fails
        if n_backfilled > 0:
            _atomic_write(OUTCOME_LOG_PATH, existing)
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
            if verbose:
                print(f"[OutcomeTracker] WARNING: Could not fetch buy for {symbol}: {e}")
            buy = None

        entry_dec  = last_decision_before(entry_vetoes,   symbol, exit_ts)
        hold_dec   = last_decision_before(hold_decisions, symbol, exit_ts)
        trade_type = trade_type_map.get(symbol, "MOMENTUM")   # default MOMENTUM
        regime     = regime_map.get(symbol, "")
        ledger_exit = exit_reason_map.get(symbol, "")

        record = build_outcome_record(
            sell, buy, entry_dec, hold_dec,
            ledger_exit_reason=ledger_exit,
            trade_type=trade_type,
            regime_at_entry=regime,
        )
        new_records.append(record)

        if verbose:
            pnl_str = f"{record['actual_pnl_pct']:+.2f}%" if record["actual_pnl_pct"] is not None else "N/A"
            e_dec   = record.get("entry_decision") or "no_record"
            h_dec   = record.get("hold_decision")  or "no_record"
            print(f"  [+] {symbol:8s} | exit: {exit_ts[:10]} | pnl: {pnl_str:8s} | "
                  f"path: {record['actual_exit_path']:22s} | type: {record['trade_type']:15s} | "
                  f"entry: {e_dec:5s} | hold: {h_dec}")

    all_records = existing + new_records
    _atomic_write(OUTCOME_LOG_PATH, all_records)

    total_new = len(new_records)
    if total_new > 0 and verbose:
        print(f"\n[OutcomeTracker] Tagged {total_new} new trade(s) → {OUTCOME_LOG_PATH}")
    elif verbose:
        print("[OutcomeTracker] No new trades to tag.")

    return total_new


# ── Summary ───────────────────────────────────────────────────────────────────
def print_summary():
    records = load_json_list(OUTCOME_LOG_PATH)
    if not records:
        print("[OutcomeTracker] outcome_log.json is empty.")
        return

    print(f"\n{'='*65}")
    print(f"  OUTCOME LOG SUMMARY — {len(records)} tagged trades")
    print(f"{'='*65}")

    by_path = defaultdict(list)
    by_type = defaultdict(list)
    for r in records:
        pnl = r.get("actual_pnl_pct")
        if pnl is not None:
            by_path[r.get("actual_exit_path", "unknown")].append(pnl)
            by_type[r.get("trade_type", "?")].append(pnl)

    print("\n  Exit path breakdown:")
    for path, pnls in sorted(by_path.items()):
        wins = sum(1 for p in pnls if p > 0)
        avg  = sum(pnls) / len(pnls)
        print(f"    {path:26s} | n={len(pnls):3d} | win%={wins/len(pnls)*100:4.0f}% | avg={avg:+.2f}%")

    print("\n  By trade type:")
    for tt, pnls in sorted(by_type.items()):
        wins = sum(1 for p in pnls if p > 0)
        avg  = sum(pnls) / len(pnls)
        print(f"    {tt:18s} | n={len(pnls):3d} | win%={wins/len(pnls)*100:4.0f}% | avg={avg:+.2f}%")

    unknown_exit  = sum(1 for r in records if r.get("actual_exit_path") == "unknown")
    missing_type  = sum(1 for r in records if not r.get("trade_type"))
    print(f"\n  Data quality: exit_unknown={unknown_exit}  trade_type_missing={missing_type}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--quiet",   action="store_true")
    args = parser.parse_args()
    if args.summary:
        print_summary()
    else:
        run_tracker(verbose=not args.quiet)
