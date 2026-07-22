"""
rebuild_positions.py — Rebuild position_outcomes.json from outcome_log.json
=============================================================================
Referenced in RAPTOR_STARTUP.md's open-actions list since before 2026-06-19,
never built until this session (2026-07-22 audit — see RAPTOR_AUDIT_20260722.md
and RAPTOR_MASTER_PLAN.md's "position_outcomes.json still has no rebuild
script" open issue). Without it, position_outcomes.json — the ONLY file
DATA-40/DATA-60/DATA-100 gates, dsr.py, and raptor_monitor.py's gate-progress
check are allowed to read (RAPTOR_SKILL.md rule 18) — had been frozen at its
2026-06-12 snapshot (27 positions) for 40+ days while 261 raw outcome_log.json
records (112 independent positions) piled up unused.

THE INDEPENDENCE RULE (RAPTOR_MASTER_PLAN.md / RAPTOR_SKILL.md rule 3):
outcome_log.json has one row per SELL EVENT (a trim or a final exit), tagged
by outcome_tracker.py and keyed by sell_order_id. A single position entry
that gets trimmed 3 times before its final exit produces 4 rows sharing the
same (symbol, entry_date) — those 4 rows are NOT 4 independent trades. This
script's entire job is to collapse each (symbol, entry_date) group of raw
events into exactly one independent-position record, matching the schema
and aggregation method already used to produce the existing 27 records
(verified below in --verify mode, not assumed).

AGGREGATION (per symbol+entry_date group, events sorted by exit_date):
  total_qty            = sum(qty)
  position_capital     = entry_price * total_qty
  position_pnl_dollars = sum((exit_price - entry_price) * qty) per event
  position_pnl_pct     = position_pnl_dollars / position_capital * 100
  weighted_exit_price  = sum(exit_price * qty) / total_qty
  final_exit_date       = last event's exit_date
  position_hold_days    = (final_exit_date - entry_date).days, cross-checked
                           against the last event's own hold_days
  final_exit_path       = last event's actual_exit_path
  n_events / n_trims     = len(group)  (kept as two fields for schema parity
                           with the original file — both always equal; the
                           original 27-record file has no case where they
                           differ, confirmed in --verify mode)
  trim_sequence          = [each event's actual_exit_path, in exit_date order]
  entry_decision/entry_confidence/hold_decision = first non-null value found
                           across the group's events (outcome_tracker.py
                           tags these per-event; a position's entry decision
                           is the same for every event in the group, so the
                           first non-null hit is authoritative, not a guess)
  entry_regime           = pulled from position_ledger.json's matching closed
                           record's metadata.regime — NOT from outcome_log.json's
                           own entry_regime field, which is null on every single
                           one of the 261 raw records (confirmed below) due to
                           a still-open regime/macro_regime key-name mismatch
                           between main.py and ledger.py (separate bug, out of
                           scope for this script — see RAPTOR_MASTER_PLAN.md)
  exit_regime             = left null; no reliable source exists yet for this
                           field (outcome_log.json's exit_regime is null on
                           169/261 records and not trustworthy on the rest —
                           flagged here rather than guessing)
  ic_valid                = final_exit_path not in ("unknown","pre_label","crypto")
                           and position_pnl_pct is not None — the exact rule
                           outcome_tracker.py's own print_summary() uses,
                           applied at the position level
  flags                  = "no_entry_decision" if entry_decision is null;
                           "leveraged_or_inverse_etp" if the symbol matches
                           universe_builder.py's _is_leveraged_or_inverse()
                           pattern against Alpaca's asset name (falls back to
                           a small known-ticker list if Alpaca isn't reachable
                           — logs a warning either way so a fallback hit is
                           visible, never silent)
  sell_order_ids          = each event's sell_order_id
  source_record_count     = n_events
  aggregated_at            = this run's UTC timestamp

SAFETY:
  - Never overwrites position_outcomes.json without --write.
  - Always backs up the existing file to position_outcomes.json.bak-<ts>
    before writing, so a bad rebuild is a one-line revert away.
  - --verify re-derives the CURRENT 27 records from outcome_log.json's state
    as of each record's own aggregated_at cutoff and diffs numeric fields
    against the live file — run this FIRST, before ever using --write, and
    paste the output (Rule 11: verification pasted in-session before this is
    DONE).

Usage:
  python rebuild_positions.py --verify   # sanity-check against existing 27 records, no writes
  python rebuild_positions.py --dry      # show what a full rebuild would produce, no writes
  python rebuild_positions.py --write    # back up old file, write the new one
"""

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [rebuild_positions] %(levelname)s: %(message)s")
logger = logging.getLogger("raptor.rebuild_positions")

OUTCOME_LOG_PATH        = Path("outcome_log.json")
POSITION_LEDGER_PATH    = Path("position_ledger.json")
POSITION_OUTCOMES_PATH  = Path("position_outcomes.json")

# Fallback only — used if Alpaca's asset name can't be fetched (e.g. no network
# from this context, or the symbol was delisted). Mirrors the intent of
# universe_builder.py's _is_leveraged_or_inverse() regex, applied to the small
# set of known leveraged/inverse tickers that predate the 2026-06-10 universe
# filter (per RAPTOR_STARTUP.md: "pre-filter era" positions).
_KNOWN_LEVERAGED_TICKERS = {
    "SQQQ", "TQQQ", "TSLL", "SOXL", "SOXS", "SPXU", "SPXL", "UPRO", "SDOW",
    "UDOW", "SH", "PSQ", "DOG", "FAZ", "FAS", "TZA", "TNA", "UVXY", "SVXY",
    "LABU", "LABD", "YINN", "YANG", "JNUG", "JDST", "NUGT", "DUST",
}


import re as _re_mod
_FRACTIONAL_SECONDS_RE = _re_mod.compile(r"(\.\d+)")

def _parse_dt(s):
    """Parse an ISO timestamp from outcome_log.json.

    BUG FOUND BY --verify (2026-07-22): outcome_log.json's timestamps come from
    datetime.isoformat() calls at varying points in the pipeline, which do NOT
    zero-pad fractional seconds to a fixed width — e.g. "...19:50:34.79323Z"
    (5 digits) vs the more common 6-digit "...13:52:14.648645Z". Python's
    datetime.fromisoformat() rejects non-6-digit fractional seconds on this
    runtime, so _parse_dt silently returned None for those rows, which made
    the exit-date sort fall back to datetime.min and misfile the record at the
    START of the group instead of wherever it truly belonged — for PLTD
    (2026-05-05 entry) this silently picked hold_days=21 (from the 5/26 event)
    as the "final" value instead of the true final event's hold_days=23 (the
    5/28 event, exit_date "...19:50:34.79323Z" — exactly the malformed one).
    Same root cause hit DKNG. Fix: normalize the fractional-second field to
    exactly 6 digits (pad or truncate) before parsing, instead of trusting
    the source string's width.
    """
    if not s:
        return None
    try:
        s = str(s).replace("Z", "+00:00")
        m = _FRACTIONAL_SECONDS_RE.search(s)
        if m:
            frac = m.group(1)[1:]  # digits after the dot
            frac = (frac + "000000")[:6]
            s = s[:m.start()] + "." + frac + s[m.end():]
        return datetime.fromisoformat(s)
    except Exception as e:
        logger.warning("Could not parse timestamp %r: %s — event will sort as earliest, may misorder the group", s, e)
        return None


def _first_not_none(events, *keys):
    for e in events:
        for k in keys:
            v = e.get(k)
            if v is not None:
                return v
    return None


def _load_ledger_regime_map():
    """symbol+entry_date -> metadata.regime, from position_ledger.json closed records."""
    out = {}
    if not POSITION_LEDGER_PATH.exists():
        logger.warning("position_ledger.json not found — entry_regime will be null for all records")
        return out
    try:
        led = json.loads(POSITION_LEDGER_PATH.read_text())
    except Exception as e:
        logger.warning("position_ledger.json unreadable (%s) — entry_regime will be null for all records", e)
        return out
    for rec in led.get("closed", []):
        sym = rec.get("symbol")
        ed  = str(rec.get("entry_date", ""))[:10]
        regime = (rec.get("metadata") or {}).get("regime")
        if sym and ed and regime is not None:
            out[(sym, ed)] = regime
    return out


def _is_leveraged_or_inverse(symbol: str) -> bool:
    """Best-effort leveraged/inverse ETP check. Tries universe_builder.py's
    real regex-against-Alpaca-asset-name logic first; falls back to a known-
    ticker list (logged) if Alpaca isn't reachable from this run."""
    try:
        from data_feeds import AlpacaDataFeed
        from config import CONFIG
        from universe_builder import _is_leveraged_or_inverse as _real_check
        dm = AlpacaDataFeed(CONFIG)
        asset = dm.trading_client.get_asset(symbol)
        name = getattr(asset, "name", "") or ""
        return _real_check(symbol, name)
    except Exception as e:
        fallback = symbol in _KNOWN_LEVERAGED_TICKERS
        logger.warning(
            "Could not verify %s against Alpaca asset name (%s) — using fallback "
            "known-ticker list, result=%s. Verify manually if this symbol is borderline.",
            symbol, e, fallback)
        return fallback


def _aggregate_group(symbol, entry_date, events, ledger_regime_map, check_leveraged=True):
    events = sorted(events, key=lambda e: _parse_dt(e.get("exit_date")) or datetime.min.replace(tzinfo=timezone.utc))

    entry_price = events[0].get("entry_price")
    entry_prices = {e.get("entry_price") for e in events}
    if len(entry_prices) > 1:
        logger.warning(
            "%s entry_date=%s: entry_price disagrees across %d events (%s) — "
            "using the first event's value (%.4f). This should not happen for "
            "one position entry; investigate outcome_log.json for this symbol.",
            symbol, entry_date, len(events), sorted(entry_prices), entry_price)

    # Rule 5 (Real data or skip. Never invent a default): 7 records in the
    # current outcome_log.json (IREN, BSX x4+) have entry_price=null — a
    # pre-existing data-quality gap in outcome_log.json itself, not something
    # to paper over with a fabricated price. Skip the whole group rather than
    # guess; these positions simply won't appear in position_outcomes.json
    # until entry_price is backfilled at the source.
    if entry_price is None:
        logger.warning(
            "%s entry_date=%s: entry_price is null on outcome_log.json record(s) — "
            "skipping group (real data or skip, not fabricating a price)",
            symbol, entry_date)
        return None

    total_qty = sum(float(e.get("qty") or 0) for e in events)
    if total_qty <= 0:
        logger.warning("%s entry_date=%s: total_qty <= 0 — skipping group (bad data)", symbol, entry_date)
        return None

    position_capital = entry_price * total_qty
    position_pnl_dollars = sum(
        (float(e.get("exit_price") or 0) - entry_price) * float(e.get("qty") or 0)
        for e in events
    )
    position_pnl_pct = round((position_pnl_dollars / position_capital) * 100, 4) if position_capital else None
    weighted_exit_price = round(
        sum(float(e.get("exit_price") or 0) * float(e.get("qty") or 0) for e in events) / total_qty, 4
    )

    final_event = events[-1]
    # FIX (found by --verify against the existing 27 records, before this script
    # ever touched --write): originally this recomputed hold_days as
    # (final_exit_date - entry_date).days and preferred that over outcome_log's
    # own reported value. That recompute was WRONG, consistently, by exactly 1
    # day on effectively every record checked (e.g. TSLA 2026-03-30->2026-04-01
    # is 2 calendar days by date-diff, but outcome_log.json's own hold_days=1
    # — and the original, known-correct position_outcomes.json record agrees
    # with outcome_log's 1, not the date-diff's 2). outcome_log.json's hold_days
    # uses a different (trading-session-based) convention than a naive calendar
    # date subtraction, and the existing 27-record file was built trusting it
    # directly. Trust it directly here too, rather than "fixing" it into a
    # systematic off-by-one across the entire dataset.
    hold_days = final_event.get("hold_days")
    # Floor at 1 (found by --verify): outcome_log.json tags same-session/next-
    # day round-trips as hold_days=0 (e.g. KRE 2026-04-09 13:34 UTC entry ->
    # 2026-04-10 13:34 UTC exit — a full 24h later, but tagged 0). The existing,
    # known-correct position_outcomes.json records floor this at 1 (a position
    # that was actually carried overnight was held for at least 1 day); every
    # multi-event record checked already had hold_days >= 1 and is unaffected
    # by the floor, so this only changes the same-day/next-day edge case.
    if hold_days is not None and hold_days < 1:
        hold_days = 1

    entry_decision   = _first_not_none(events, "entry_decision")
    entry_confidence = _first_not_none(events, "entry_confidence")
    hold_decision    = _first_not_none(events, "hold_decision")
    entry_regime     = ledger_regime_map.get((symbol, entry_date))
    final_exit_path  = final_event.get("actual_exit_path")

    flags = []
    if entry_decision is None:
        flags.append("no_entry_decision")
    if check_leveraged and _is_leveraged_or_inverse(symbol):
        flags.append("leveraged_or_inverse_etp")

    ic_valid = (final_exit_path not in ("unknown", "pre_label", "crypto")
                and position_pnl_pct is not None)

    return {
        "symbol": symbol,
        "entry_price": entry_price,
        "entry_date": entry_date,
        "final_exit_date": str(final_event.get("exit_date", ""))[:10],
        "total_qty": total_qty,
        "position_capital": round(position_capital, 2),
        "position_pnl_pct": position_pnl_pct,
        "position_pnl_dollars": round(position_pnl_dollars, 2),
        "weighted_exit_price": weighted_exit_price,
        "position_hold_days": hold_days,
        "final_exit_path": final_exit_path,
        "n_events": len(events),
        "n_trims": len(events),
        "trim_sequence": [e.get("actual_exit_path") for e in events],
        "entry_decision": entry_decision,
        "entry_confidence": entry_confidence,
        "hold_decision": hold_decision,
        "entry_regime": entry_regime,
        "exit_regime": None,  # no reliable source yet — see module docstring
        "ic_valid": ic_valid,
        "flags": flags,
        "sell_order_ids": [e.get("sell_order_id") for e in events],
        "source_record_count": len(events),
        "aggregated_at": datetime.now(timezone.utc).isoformat(),
    }


def build_all(check_leveraged=True):
    if not OUTCOME_LOG_PATH.exists():
        logger.error("outcome_log.json not found — nothing to rebuild from")
        return []
    log = json.loads(OUTCOME_LOG_PATH.read_text())
    ledger_regime_map = _load_ledger_regime_map()

    groups = defaultdict(list)
    n_crypto_skipped = 0
    for r in log:
        ed = str(r.get("entry_date", ""))[:10]
        sym = r.get("symbol")
        if not sym or not ed:
            logger.warning("Skipping malformed outcome_log record (missing symbol/entry_date): %s", r.get("sell_order_id"))
            continue
        # Crypto pairs (e.g. "ETH/USD") don't fit the equity assumption this
        # aggregator relies on — a single position's entry_price is constant
        # across every trim/exit event. Confirmed on the current data: SOL/USD,
        # ETH/USD, and BTC/USD each disagree on entry_price across their own
        # events (crypto_engine.py evidently tracks these differently — likely
        # multiple separate buys sharing one nominal entry_date rather than one
        # entry trimmed multiple times). outcome_tracker.py's own
        # print_summary() already excludes actual_exit_path=="crypto" from
        # IC-valid trades, and the existing 27-record position_outcomes.json
        # contains zero crypto symbols — matching that same exclusion here
        # rather than guessing at crypto-specific aggregation math.
        if "/" in sym or r.get("actual_exit_path") == "crypto":
            n_crypto_skipped += 1
            continue
        groups[(sym, ed)].append(r)
    if n_crypto_skipped:
        logger.info("Excluded %d crypto outcome_log record(s) — not part of the equity position_outcomes.json (matches outcome_tracker.py's own IC exclusion)", n_crypto_skipped)

    results = []
    for (sym, ed), events in sorted(groups.items(), key=lambda kv: kv[0][1]):
        rec = _aggregate_group(sym, ed, events, ledger_regime_map, check_leveraged=check_leveraged)
        if rec:
            results.append(rec)
    return results


def verify():
    """Re-derive today's 27 existing records from outcome_log.json and diff
    numeric fields against the live position_outcomes.json. Run this BEFORE
    ever using --write."""
    if not POSITION_OUTCOMES_PATH.exists():
        logger.error("position_outcomes.json not found — nothing to verify against")
        return False

    existing = {(r["symbol"], r["entry_date"]): r for r in json.loads(POSITION_OUTCOMES_PATH.read_text())}
    rebuilt_all = build_all(check_leveraged=False)  # skip Alpaca calls for a fast, network-free verify pass
    rebuilt = {(r["symbol"], r["entry_date"]): r for r in rebuilt_all}

    numeric_fields = ["total_qty", "position_capital", "position_pnl_pct",
                       "position_pnl_dollars", "weighted_exit_price",
                       "position_hold_days", "n_events"]

    n_checked = 0
    n_mismatch = 0
    missing = []
    for key, old in existing.items():
        new = rebuilt.get(key)
        if new is None:
            missing.append(key)
            continue
        n_checked += 1
        diffs = []
        for f in numeric_fields:
            ov, nv = old.get(f), new.get(f)
            if ov is None or nv is None:
                continue
            if isinstance(ov, (int, float)) and isinstance(nv, (int, float)):
                if abs(ov - nv) > max(0.02, abs(ov) * 0.001):  # tolerance for rounding
                    diffs.append((f, ov, nv))
            elif ov != nv:
                diffs.append((f, ov, nv))
        if diffs:
            n_mismatch += 1
            logger.warning("MISMATCH %s entry_date=%s: %s", key[0], key[1], diffs)

    print(f"\n{'='*70}")
    print(f"  VERIFY: existing position_outcomes.json ({len(existing)} records) vs rebuild logic")
    print(f"{'='*70}")
    print(f"  Checked (found in both):  {n_checked}")
    print(f"  Numeric mismatches:       {n_mismatch}")
    print(f"  In existing but not rebuilt (entry_date/symbol not in outcome_log.json): {len(missing)}")
    if missing:
        for m in missing[:10]:
            print(f"    {m}")
    print(f"  New independent positions available beyond the existing 27: {len(rebuilt) - n_checked}")
    print(f"{'='*70}\n")

    return n_mismatch == 0


def main():
    parser = argparse.ArgumentParser(description="Rebuild position_outcomes.json from outcome_log.json")
    parser.add_argument("--verify", action="store_true", help="Check rebuild logic against existing 27 records, no writes")
    parser.add_argument("--dry", action="store_true", help="Show full rebuild output, no writes")
    parser.add_argument("--write", action="store_true", help="Back up old file and write the new one")
    parser.add_argument("--no-leveraged-check", action="store_true", help="Skip Alpaca asset-name lookups (faster, less accurate leveraged/inverse flagging)")
    args = parser.parse_args()

    if args.verify:
        ok = verify()
        sys.exit(0 if ok else 1)

    results = build_all(check_leveraged=not args.no_leveraged_check)
    clean = [r for r in results if "leveraged_or_inverse_etp" not in r["flags"]]

    print(f"\n{'='*70}")
    print(f"  REBUILD: {len(results)} independent positions from outcome_log.json")
    print(f"  Clean (excl. leveraged/inverse ETPs): {len(clean)}")
    print(f"  DATA-40 gate: {len(clean)}/40  {'MET' if len(clean) >= 40 else 'OPEN'}")
    print(f"  DATA-60 gate: {len(clean)}/60  {'MET' if len(clean) >= 60 else 'OPEN'}")
    print(f"{'='*70}\n")

    if args.dry:
        print("--dry: no files written.")
        return

    if args.write:
        if POSITION_OUTCOMES_PATH.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = Path(f"position_outcomes.json.bak-{ts}")
            backup_path.write_text(POSITION_OUTCOMES_PATH.read_text())
            logger.info("Backed up existing file to %s", backup_path)
        POSITION_OUTCOMES_PATH.write_text(json.dumps(results, indent=2))
        logger.info("Wrote %s (%d records, %d clean)", POSITION_OUTCOMES_PATH, len(results), len(clean))
    else:
        print("No action taken. Use --verify first, then --dry to preview, then --write to commit.")


if __name__ == "__main__":
    main()
