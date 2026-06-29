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

ENTRY_VETOES_PATH    = "entry_vetoes.json"
HOLD_DECISIONS_PATH  = "hold_decisions.json"
OUTCOME_LOG_PATH     = "outcome_log.json"
OUTCOME_PENDING_PATH = "outcome_pending.json"

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
    except (json.JSONDecodeError, OSError) as e:
        # FIX (2026-06-29, audit P2): previously swallowed silently and
        # returned [] — indistinguishable from a genuinely empty/missing
        # file, so corruption in outcome_log.json/entry_vetoes.json/
        # hold_decisions.json went unnoticed until P&L stats looked wrong.
        print(f"[OutcomeTracker] WARNING: failed to load {path}: {e} "
              f"— treating as empty. Check this file for corruption.")
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

def build_outcome_record(sell_order, buy_order, entry_decision, hold_decision,
                         exit_regime: str = None, entry_regime: str = None) -> dict:
    # exit_regime: macro regime label at exit time (RISK_ON/NEUTRAL/RISK_OFF/CRISIS).
    # entry_regime: macro regime label at entry time — from ledger metadata.macro_regime.
    # Both enable "macro regime at entry vs exit" metric. Added 2026-06-11/2026-06-26.
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
    # exit_monitor writes: reason_SYM_TIMESTAMP as client_order_id.
    # Must match exactly what exit_monitor.py uses — not backtest labels.
    client_id = sell_order.get("client_order_id", "")
    exit_path = "unknown"
    # Order matters — more specific patterns first (math_trim before math)
    for path in [
        "math_trim",        # partial trim: math_trim_25%_SYM_TS
        "math_exit",        # full math exit
        "hard_stop",        # EXIT 1
        "trailing_stop",    # EXIT 2
        "thesis_invalid",   # EXIT 3
        "portfolio_heat",   # EXIT 4
        "leveraged_3x_cap", # EXIT 4B
        "leveraged_2x_cap", # EXIT 4B
        "time_decay",       # EXIT 5
        # Legacy backtest labels — kept for historical records
        "trail_profit", "trail_loss", "profit_target",
        "momentum_break", "agent_exit",
    ]:
        if path in client_id:
            exit_path = path
            break

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
        "exit_regime":      exit_regime,
        "entry_regime":     entry_regime,
    }


def relabel_pre_label(verbose: bool = True) -> int:
    """
    Trades from before exit_monitor started writing labeled client_order_ids
    cannot be tagged from Alpaca order data. Rather than leaving them as
    'unknown' (which poisons IC calibration), mark them 'pre_label' so they:
    - Are excluded from IC training (unknown intent)
    - Are kept for aggregate PnL stats
    - Are visibly distinct from genuinely untagged new trades
    Crypto trades (symbol contains '/') are marked 'crypto' separately.
    """
    records = load_json_list(OUTCOME_LOG_PATH)
    unknowns = [r for r in records if r.get("actual_exit_path") == "unknown"]

    if not unknowns:
        print("[OutcomeTracker] No unknown records to relabel.")
        return 0

    fixed = 0
    for r in records:
        if r.get("actual_exit_path") != "unknown":
            continue
        sym = r.get("symbol", "")
        if "/" in sym:
            r["actual_exit_path"] = "crypto"
            if verbose:
                print(f"  [crypto] {sym:10s} {r.get('exit_date','')[:10]}")
        else:
            r["actual_exit_path"] = "pre_label"
            if verbose:
                print(f"  [pre_label] {sym:8s} {r.get('exit_date','')[:10]} "
                      f"pnl={r.get('actual_pnl_pct','?')}")
        fixed += 1

    if fixed:
        tmp = OUTCOME_LOG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(records, f, indent=2)
        os.replace(tmp, OUTCOME_LOG_PATH)
        print(f"\n[OutcomeTracker] Relabeled {fixed} records -> {OUTCOME_LOG_PATH}")
        print("  pre_label = equity exits before labeled client_order_id era")
        print("  crypto    = crypto exits (no exit path label system)")
        print("  These are EXCLUDED from IC calibration but kept for PnL stats.")

    return fixed


def backfill_unknowns(verbose: bool = True) -> int:
    """
    Re-tag existing outcome_log.json records where actual_exit_path='unknown'.
    Fetches the original Alpaca sell order by sell_order_id and applies the
    updated exit path detection logic. Writes updated records in-place.
    """
    records = load_json_list(OUTCOME_LOG_PATH)
    unknowns = [r for r in records if r.get("actual_exit_path") == "unknown"
                and r.get("sell_order_id")]

    if not unknowns:
        print("[OutcomeTracker] No unknown records to backfill.")
        return 0

    if verbose:
        print(f"[OutcomeTracker] Backfilling {len(unknowns)} unknown records...")

    fixed = 0
    for r in records:
        if r.get("actual_exit_path") != "unknown" or not r.get("sell_order_id"):
            continue
        try:
            url = f"{BASE_URL}/v2/orders/{r['sell_order_id']}"
            resp = requests.get(url, headers=alpaca_headers(), timeout=10)
            if resp.status_code != 200:
                continue
            order = resp.json()
            client_id = order.get("client_order_id", "")
            new_path = "unknown"
            for path in [
                "math_trim", "math_exit", "hard_stop", "trailing_stop",
                "thesis_invalid", "portfolio_heat", "leveraged_3x_cap",
                "leveraged_2x_cap", "time_decay",
                "trail_profit", "trail_loss", "profit_target",
                "momentum_break", "agent_exit",
            ]:
                if path in client_id:
                    new_path = path
                    break
            if new_path != "unknown":
                if verbose:
                    print(f"  [fix] {r['symbol']:8s} {r.get('exit_date','')[:10]} "
                          f"unknown -> {new_path}  (client_id={client_id[:40]})")
                r["actual_exit_path"] = new_path
                fixed += 1
            elif verbose:
                print(f"  [skip] {r['symbol']:8s} client_id='{client_id[:60]}' — still unknown")
        except Exception as e:
            if verbose:
                print(f"  [err] {r.get('symbol','?')}: {e}")

    if fixed:
        tmp = OUTCOME_LOG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(records, f, indent=2)
        os.replace(tmp, OUTCOME_LOG_PATH)
        print(f"\n[OutcomeTracker] Backfilled {fixed} records -> {OUTCOME_LOG_PATH}")
    else:
        print("[OutcomeTracker] No records could be re-tagged from Alpaca order data.")

    return fixed


def _load_outcome_pending() -> dict:
    """
    Load the sidecar written by exit_monitor after every successful sell.
    Keyed by Alpaca order ID. Contains entry_decision, exit_reason, composite.
    This is the P0-1 fix — direct order-ID join replaces fragile timestamp match.
    """
    if not os.path.exists(OUTCOME_PENDING_PATH):
        return {}
    try:
        with open(OUTCOME_PENDING_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        # FIX (2026-06-29, audit P2): previously swallowed silently and
        # returned {} — this sidecar is the P0-1 direct order-ID join that
        # run_tracker() relies on for accurate entry_decision attribution.
        # A silently-empty pending dict makes every trade fall back to the
        # weaker timestamp-match heuristic with no visible warning.
        print(f"[OutcomeTracker] WARNING: failed to load {OUTCOME_PENDING_PATH}: {e} "
              f"— treating as empty. entry_decision attribution will fall back "
              f"to timestamp matching for all trades this run. Check this file "
              f"for corruption.")
        return {}


def run_tracker(verbose: bool = True) -> int:
    existing   = load_json_list(OUTCOME_LOG_PATH)
    tagged_ids = {r["sell_order_id"] for r in existing if r.get("sell_order_id")}

    # P0-1: Load sidecar for direct order-ID → entry_decision join.
    # Falls back to timestamp-match (last_decision_before) for older records.
    pending        = _load_outcome_pending()
    entry_vetoes   = load_json_list(ENTRY_VETOES_PATH)
    hold_decisions = load_json_list(HOLD_DECISIONS_PATH)

    if verbose:
        print(f"[OutcomeTracker] Existing tagged trades : {len(existing)}")
        print(f"[OutcomeTracker] Pending sidecar records: {len(pending)}")
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

        # P0-1: use sidecar if available (exact order-ID match), else timestamp search
        _pending_rec = pending.get(order_id)
        if _pending_rec:
            entry_dec = {
                "decision":   _pending_rec.get("entry_decision"),
                "confidence": _pending_rec.get("entry_confidence"),
                "reasoning":  _pending_rec.get("entry_reasoning"),
                "flags":      _pending_rec.get("entry_flags", []),
                "timestamp":  _pending_rec.get("submitted_at"),
                "symbol":     symbol,
            }
        else:
            entry_dec = last_decision_before(entry_vetoes, symbol, exit_ts)
        hold_dec  = last_decision_before(hold_decisions, symbol, exit_ts)
        # Stamp exit-time macro regime (added 2026-06-11)
        # BUG FIX 2026-06-26: was .get("regime") — macro_context.json uses key "macro_regime"
        _exit_regime = None
        _entry_regime = None
        try:
            import json as _json
            from pathlib import Path as _Path
            _mc = _Path("macro_context.json")
            if _mc.exists():
                _mc_data = _json.loads(_mc.read_text())
                _exit_regime = _mc_data.get("macro_regime") or _mc_data.get("regime")
        except Exception:
            pass
        # entry_regime: read from outcome_pending sidecar (written by exit_monitor at sell time),
        # falling back to ledger metadata for positions already closed before this fix.
        try:
            import json as _json2
            from pathlib import Path as _Path2
            _pending_path = _Path2("outcome_pending.json")
            if _pending_path.exists():
                _pending_all = _json2.loads(_pending_path.read_text())
                _order_id_key = sell.get("id", "")
                _pending_rec_er = _pending_all.get(_order_id_key, {})
                _entry_regime = _pending_rec_er.get("entry_regime")
            if not _entry_regime:
                # Fallback: read from position_ledger closed entry for this symbol
                _led_path = _Path2("position_ledger.json")
                if _led_path.exists():
                    _led = _json2.loads(_led_path.read_text())
                    for _ct in reversed(_led.get("closed", [])):
                        if _ct.get("symbol") == symbol:
                            _entry_regime = _ct.get("entry_regime") or (_ct.get("metadata") or {}).get("macro_regime")
                            break
        except Exception:
            pass
        record    = build_outcome_record(sell, buy, entry_dec, hold_dec,
                                         exit_regime=_exit_regime,
                                         entry_regime=_entry_regime)
        new_records.append(record)

        if verbose:
            pnl_str = f"{record['actual_pnl_pct']:+.2f}%" if record["actual_pnl_pct"] is not None else "N/A"
            e_dec   = record.get("entry_decision") or "no_record"
            h_dec   = record.get("hold_decision")  or "no_record"
            print(f"  [+] {symbol:8s} | exit: {exit_ts[:10]} | pnl: {pnl_str:8s} | "
                  f"path: {record['actual_exit_path']:22s} | entry: {e_dec:5s} | hold: {h_dec}")

    if new_records:
        all_records = existing + new_records
        tmp = OUTCOME_LOG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(all_records, f, indent=2)
        os.replace(tmp, OUTCOME_LOG_PATH)
        print(f"\n[OutcomeTracker] Tagged {len(new_records)} new trade(s) -> {OUTCOME_LOG_PATH}")
    else:
        print("[OutcomeTracker] No new trades to tag.")

    # Backfill any pending-fill slippage records (market orders that were still
    # pending_new when exit_monitor ran — fill price available by end of day).
    try:
        from slippage_tracker import backfill_slippage as _bf_slip
        _n_backfilled = _bf_slip(alpaca_headers, BASE_URL)
        if _n_backfilled and verbose:
            print(f"[SlippageTracker] Backfilled {_n_backfilled} pending fill(s)")
    except Exception as _se:
        if verbose:
            print(f"[SlippageTracker] Backfill skipped: {_se}")

    return len(new_records)


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary():
    records = load_json_list(OUTCOME_LOG_PATH)
    if not records:
        print("[OutcomeTracker] outcome_log.json is empty — no closed trades yet.")
        return

    from collections import defaultdict

    # Separate IC-valid trades from pre-label era and crypto
    ic_valid = [r for r in records
                if r.get("actual_exit_path") not in ("unknown", "pre_label", "crypto")
                and r.get("actual_pnl_pct") is not None]
    pre_label = [r for r in records if r.get("actual_exit_path") == "pre_label"]
    crypto    = [r for r in records if r.get("actual_exit_path") == "crypto"]
    unknown   = [r for r in records if r.get("actual_exit_path") == "unknown"]

    print(f"\n{'='*62}")
    print(f"  OUTCOME LOG SUMMARY — {len(records)} tagged trades")
    print(f"  IC-valid (labeled exits): {len(ic_valid)}")
    print(f"  Pre-label era (excluded from IC): {len(pre_label)}")
    print(f"  Crypto (excluded from IC): {len(crypto)}")
    print(f"  Still unknown: {len(unknown)}")
    print(f"{'='*62}")

    by_path = defaultdict(list)
    for r in records:
        path = r.get("actual_exit_path", "unknown")
        if r.get("actual_pnl_pct") is not None:
            by_path[path].append(r["actual_pnl_pct"])

    print("\n  Exit path breakdown:")
    for path, pnls in sorted(by_path.items()):
        wins = sum(1 for p in pnls if p > 0)
        avg  = sum(pnls) / len(pnls)
        ic_flag = "  [excl IC]" if path in ("pre_label", "crypto", "unknown") else ""
        print(f"    {path:24s} | n={len(pnls):3d} | win%={wins/len(pnls)*100:4.0f}% | avg={avg:+.2f}%{ic_flag}")

    print("\n  By trade type:")
    by_type = defaultdict(list)
    for r in records:
        t = r.get("trade_type", "MOMENTUM")
        if r.get("actual_pnl_pct") is not None:
            by_type[t].append(r["actual_pnl_pct"])
    for t, pnls in sorted(by_type.items()):
        wins = sum(1 for p in pnls if p > 0)
        print(f"    {t:18s} | n={len(pnls):3d} | win%={wins/len(pnls)*100:4.0f}% | avg={sum(pnls)/len(pnls):+.2f}%")

    print(f"\n  Data quality: exit_unknown={len(unknown)}  pre_label={len(pre_label)}  crypto={len(crypto)}")
    print(f"{'='*62}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Raptor Outcome Tracker — Layer 1")
    parser.add_argument("--summary",  action="store_true", help="Print outcome_log.json summary")
    parser.add_argument("--quiet",    action="store_true", help="Suppress per-trade output")
    parser.add_argument("--backfill", action="store_true", help="Re-tag existing unknown records from Alpaca")
    parser.add_argument("--relabel",  action="store_true", help="Mark pre-label-era unknowns as pre_label/crypto")
    args = parser.parse_args()

    if args.summary:
        print_summary()
    elif args.backfill:
        n = backfill_unknowns(verbose=not args.quiet)
        if n > 0:
            print_summary()
    elif args.relabel:
        n = relabel_pre_label(verbose=not args.quiet)
        if n > 0:
            print_summary()
    else:
        n = run_tracker(verbose=not args.quiet)
        if n > 0:
            print_summary()
