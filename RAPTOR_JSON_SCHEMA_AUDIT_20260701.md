# Raptor JSON Schema Audit — 2026-07-01 (Session 12 continuation)

Scope: every JSON file the live pipeline reads or writes, checked for field-name
consistency across producers/consumers, null/type conventions, and value-shape
drift (decimal vs. percentage, key formats). Methodology: mapped every script that
touches each file (grep across all `.py` files), then read the actual write/read
call sites for the fields that matter to trading decisions or analytics.

Files covered: `position_ledger.json`, `hold_health.json`, `hold_history.json`,
`macro_context.json`, `market_decision.json`, `composite_cache.json`,
`cooldown_log.json`, `outcome_log.json`, `outcome_pending.json`,
`position_outcomes.json`, `slippage_log.json`, `kelly_estimates.json`,
`trim_log.json`, `entry_vetoes.json`, `hold_decisions.json`.

---

## Finding A — `pnl_pct` decimal-vs-percentage heuristic in `daily_recap.py` (real bug risk)

Every current writer of `position_ledger.json`'s `closed` list stores `pnl_pct` as
a **percentage** (e.g. `5.2` for +5.2%): `ledger.py`'s `record_exit`/`record_trim`
(`round((pnl/entry_value)*100, 4)`) and both manual repair scripts.

`daily_recap.py`'s closed-trades table (`build_html`, ~line 872) nonetheless
guesses the unit from magnitude:

```python
if pnl is not None:
    pnl_f = float(pnl)
    # If abs < 1.5, was stored as decimal — convert
    if abs(pnl_f) < 1.5:
        pnl_f = pnl_f * 100
```

No current writer ever puts a decimal-scale value in this field, so this
heuristic has no real case left to catch — but it actively misfires on any
genuine trade whose real return falls between -1.5% and +1.5%: a true +1.2%
winner gets multiplied by 100 and displayed as **+120%**. This is a silent
display bug (the underlying ledger data is fine; only the recap email would
show a wrong number), but it's exactly the "discrepancy nobody notices until it
matters" class of bug.

**Recommendation:** remove the heuristic. If there's a historical reason to
suspect a decimal-scale write happened once (worth double-checking `crypto_engine.py`
and `backtest.py`, which are separate books), guard with an explicit provenance
field instead of a magnitude guess.

---

## Finding B — `"regime"` vs `"macro_regime"` key split (real, confirmed, systemic)

Two coexisting conventions for the same concept:

- The **in-memory `macro` dict** that `data_feeds.py`/`DataManager` builds and
  passes to `signals.py`, `main.py`, `exit_monitor.py`, `daily_recap.py` during a
  scan uses the key **`"regime"`**.
- The **raw `macro_context.json` file** (written by `macro_context.py`) and
  **`market_decision.json`** (written by `market_agent.py`) use the key
  **`"macro_regime"`**.

This has already caused one documented production bug — `outcome_tracker.py` line
406 carries the comment "BUG FIX 2026-06-26: was `.get("regime")` — macro_context.json
uses key `macro_regime`". It's now hand-patched with `or` fallback chains in at
least four places (`main.py:255`, `exit_monitor.py:266`, `outcome_tracker.py:415`,
`ledger.py` reads `metadata.macro_regime` specifically). Each fallback site had to
be independently discovered and fixed after something silently misread the wrong
key — there's no single place that normalizes it.

**Recommendation:** pick one canonical key (`macro_regime`, since that's what both
persisted JSON files use) and normalize it once, at the point where
`data_feeds.py` builds its in-memory `macro` dict, so every downstream consumer
can rely on one key without an `or` chain.

---

## Finding C — RESOLVED, already fixed 2026-06-29 (verified against the live ledger)

Original concern: `repair_ledger_20260619.py` predates the P0-1 multi-trim aggregation
fix and might have left KDP/PFE/SQQQ with an incorrect headline P&L.

Checked the live `position_ledger.json` directly: KDP and SQQQ both carry a
`"backfill_note": "Corrected 2026-06-29 — audit P0-1: prior pnl/pnl_pct reflected
only the final leg, dropping ... earlier trims"` and their current `pnl`/`pnl_pct`
correctly sum every trim plus the final leg (verified by hand: KDP
25.54+23.88+9.58+16.87=75.87 ✓, SQQQ 76.96-684.48=-607.52 ✓). PFE never had prior
trims, so no correction was needed there. **No fix required — this was already
done in a prior session.**

## Finding F — ~19 fragmented "closed" trade records from the May 2026 math_trim-routing bug — DECISION: leave history as-is, documented only (2026-07-01)

While verifying Finding C I read the full live `position_ledger.json` and found
something larger than the original repair-script question. Between 2026-05-15 and
2026-05-27, a bug (independently documented in the ledger itself: *"position was
incorrectly closed in ledger by math_trim routing bug"*, restored by
`backfill_positions.py` on 2026-05-27) caused partial trims on positions like
KDP, CVE, AMD, INTC, SMCI, PLTD, DKNG, CSX, TSLA to be recorded as **full closes**
instead of trims. Each time this happened, the position was still genuinely open
on Alpaca, so the next reconciliation pass re-added it via `backfill_ledger.py`
as a **brand-new "open" position** with today's date as `entry_date` (but the
same real `entry_price`, since Alpaca's `avg_entry` doesn't change on a sell) —
then the *next* trim event repeated the cycle.

Net effect: a single real, continuous holding period for each of these symbols
is currently split across **2–4 separate records** in `position_ledger.json`'s
`closed` list, each showing `"regime": "BACKFILL"` and a shortened, fake
`entry_date`. Exact fragment count per symbol, hand-counted against the live
file (identify by `entry_price` recurring across records — Alpaca's `avg_entry`
doesn't change on a sell, so every fragment of one real position shares it):

| Symbol | Entry price | Fragments | Date span |
|---|---|---|---|
| KDP  | 28.670593 | 4 | 05-15 → 06-18 (final leg properly closed via repair_ledger_20260619.py) |
| CVE  | 28.72     | 4 | 05-15 → 05-28 |
| PLTD | 7.74      | 4 | 05-15 → 05-28 |
| AMD  | 304.32    | 3 | 05-15 → 06-05 |
| INTC | 87.74     | 3 | 05-15 → 06-05 |
| SMCI | 34.096966 | 3 | 05-15 → 06-05 |
| DKNG | 25.053855 | 3 | 05-15 → 05-29 |
| CSX  | 45.419615 | 2 | 05-15 → 05-29 |
| TSLA | 391.11    | 2 | 05-15 → 06-05 |

9 symbols, 28 total fragment records representing what should be 9 real
trades — **19 excess "phantom" trade-count entries.** Everything from
2026-06-05 onward is clean and unaffected (the routing bug was already fixed by
then). A few other symbols (OWL, TSLL, KRE, FXI, TTD) also carry a single
`"regime": "BACKFILL"` record each from the same May backfill event but were
never fragmented — those are individually correct trades, just with placeholder
metadata (`t_stat`/`kelly_fraction`/`composite_score` all `null`) instead of
real entry-time values, which is expected for anything backfilled rather than
entered live.

**Why this matters:** `daily_recap.py` and `raptor_monitor.py`'s Sharpe/Sortino/
win-rate/expectancy/DSR calculations, and the `DATA-40`/`DATA-60`/`DATA-100` Kelly
activation gate in `raptor_monitor.py` Layer 4, all count every record in
`closed` as one independent trade — so trade count is overstated by 19, and the
realized-return distribution feeding Sharpe/DSR has a handful of legs from 9
continuous trades being double- or triple-counted as independent draws, which
conflicts with the "Independence corollary" the master plan already establishes
for `position_outcomes.json`.

**Decision (Steve, 2026-07-01): leave history as-is, this note is the record of
the issue.** No values in `position_ledger.json` were changed — `pnl`, `pnl_pct`,
`entry_date`, `exit_date`, and share counts on every fragment above are
untouched. This section exists so that whenever `rebuild_positions.py`
(referenced in RAPTOR_STARTUP.md, not yet built — see the open item in Session
11) or any future trade-count-sensitive analysis is built, it accounts for these
9 symbols producing 19 fewer independent trades than a raw count of `closed`
would suggest.

## Finding D — RETRACTED after closer inspection (2026-07-01, same day)

Original claim: `backfill_ledger.py` writes a stray `pnl_pct` field onto *open*
ledger positions that `ledger.py`'s native `record_entry()` never creates.

On re-reading the full write path: `backfill_ledger.py` does compute a local
`pnl_pct` and puts it in its own `to_write` list (line 75), but that field is
only ever used for the script's own dry-run preview print (line 93,
`pnl={r['pnl_pct']:+.1f}%`) — it is **not** one of the arguments passed to
`ledger.record_entry()` (which only accepts `model, symbol, shares, entry_price,
date, metadata`), so it never reaches `position_ledger.json` at all. No schema
drift exists here; no fix needed.

---

## Finding E — `raptor_dashboard.py` computes a merged closed-trade list it never uses (completeness gap, not a crash)

```python
closed_all = outcome + closed_ledger   # line 141
analytics = compute_analytics(outcome)  # line 143 — uses `outcome` alone
```

`closed_all` is built but never referenced again. The dashboard's "recent closed
trades" section (`recent_closed`, line 213) sorts from `outcome` (i.e.
`outcome_log.json`) only — never `closed_ledger` (`position_ledger.json`'s
`closed` list). Any position closed through a path that never got tagged into
`outcome_log.json` (a manual ledger repair, or a run where `outcome_tracker.py`
itself failed) is invisible in the dashboard even though it's correctly present
in the ledger. Low severity: cosmetic, not a trading-logic issue.

**Recommendation:** either use `closed_all` for the recent-trades view (dedup by
symbol+exit_date against double-counting positions present in both), or delete
the dead `closed_all` line.

---

## Not a bug — ruled out during this audit

- **`market_decision.json`'s `macro_regime` key** — confirmed `market_agent.py`
  writes it (line 170) and `morning_scanner_email.py` reads the same key; no
  mismatch here despite the similarity to Finding B.
- **`raptor_dashboard.py`'s `compute_analytics`** already defends against Finding
  B/pnl-naming by reading `t.get("actual_pnl_pct", t.get("pnl_pct", 0))` — a good
  example of the ad hoc, per-call-site patching that Finding B's recommendation
  would make unnecessary.
- **`hold_health.json`'s internal `pnl_pct`** (top-level record and nested
  `snapshot.pnl_pct`) — both consistently percentage-scale, no drift found.
- **Ledger key format `"{model}:{symbol}"`** — verified identical across every
  writer/reader (`main.py`, `exit_monitor.py`, `watchdog.py`, `daily_recap.py`,
  both repair scripts).

## Not completed (would need more time)

- A field-by-field audit of `hold_health.json`'s ~15-field snapshot schema
  against every consumer (`raptor_dashboard.py`, `daily_recap.py`,
  `exit_monitor.py`) — spot-checked `pnl_pct` and `stop_dist_atr` only.
- `kelly_estimates.json` and `slippage_log.json` schemas — enumerated
  readers/writers (see file map below) but didn't do a field-level pass.

---

## Tier 1/2 logic & debugging audit — 2026-07-01 (same-day continuation)

Scope: full read + logic audit of 8 files not covered by the schema pass above —
`margin_guard.py`, `kelly_engine.py`, `dsr.py`, `config.py` (Tier 1); `slippage_tracker.py`,
`universe_builder.py`, `premarket_scanner.py`, `market_agent.py` (Tier 2).

### Fixed

**`kelly_engine.py` — `load_outcomes()` decimal/percentage heuristic (real bug, same class as Finding A, higher severity).**
`outcome_tracker.py` always writes `actual_pnl_pct` as a percentage. The old
normalization `pnl / 100.0 if abs(pnl) > 1.0 else pnl` left any trade with
|return| ≤ 1% un-divided — a real `+0.41%` WFC trade or `-0.20%` CSX/UBER trade
was fed into the Kelly `f* = μ/σ²` math as a `+41%`/`-20%` return, corrupting
mean, variance, skew, kurtosis, and `f_recommended` for exactly the trades that
matter most for accuracy (near-breakeven ones). `dsr.py` already does this
correctly with an unconditional `/100.0` — `kelly_engine.py` now matches it.
Currently only affects SHADOW-mode diagnostics (n<100 trades/book), but would
have silently corrupted ACTIVE-mode production position sizing once a book
crossed `MIN_TRADES_ACTIVE=100`.

**`universe_builder.py` — `sensitivity_report()` stale thresholds.** The
2026-05-22 sensitivity sweep raised the live screen filters from 500K→750K
shares and $20M→$30M dollar volume (`_screen()`, correctly updated at the
time). `sensitivity_report()`'s own `thresholds` table was never updated and
was still computing "near-threshold" counts against the old 500K/$20M
cutoffs — silently misleading output for anyone using this report to tune
filters further. Aligned to the actual enforced values.

**`premarket_scanner.py` — swallowed failures, exit code always 0.** Both
pipeline steps (`macro_context.py`, `market_agent.py`) caught and logged
exceptions but always fell through to "Pre-market init complete." with no way
for the Task Scheduler job to detect a FATAL failure. Since this runs
unattended at 9:00 AM ET, a silent macro/market-agent failure could go
unnoticed for days. Now tracks per-step success and exits non-zero if either
step failed — consistent with the fail-closed philosophy already established
in `margin_guard.py`.

**`market_agent.py` — `load_market_decision()` and `evaluate_session()` fail open on missing/unreadable/stale data (fixed 2026-07-01, per Steve's go-ahead).** All three fallback paths — `market_decision.json`/`macro_context.json` missing, unreadable, or >12h stale — used to default to `{"decision": "SCAN", "risk_scalar": 1.0}`, i.e. full-size normal trading, the opposite of the fail-closed pattern `margin_guard.py` already uses deliberately elsewhere in this codebase. Now all three default to `{"decision": "REDUCE", "risk_scalar": FAIL_CLOSED_SCALAR=0.75}` instead — conservative without being as extreme as STANDBY (a data hiccup alone isn't evidence of an actual crisis). `main.py` already has a first-class `REDUCE` code path (scales `my_equity *= risk_scalar`), so this required no changes outside `market_agent.py`.

**`slippage_tracker.py` — `record_fill()`/`backfill_slippage()` had no file lock around read-modify-write of `slippage_log.json` (fixed 2026-07-01).** Same class of race already found and fixed for `position_ledger.json` (via `ledger_lock.py`): `watchdog.py` (15-min cycle) and `exit_monitor.py` (30-min cycle) could each independently load, append, and atomically rewrite `slippage_log.json`, and if both fired in the same overlapping window for different symbols, the second writer's full-file save would silently drop the first writer's record. Fixed by generalizing `ledger_lock.py`'s mutex primitive (`_file_lock`) and adding a `slippage_lock()` wrapper with its own mutex file (`slippage_log.mutex`) — `ledger_lock()` itself is unchanged and still uses `position_ledger.mutex`, so the two locks never contend with each other. Both `record_fill()`'s load-append-save and `backfill_slippage()`'s load-mutate-save (including its network calls to Alpaca, which need to happen inside the lock since the in-memory snapshot must stay valid across the whole cycle) now hold the lock for their full span.

### Clean — no issues found

- **`margin_guard.py`** — fails closed on any exception (returns `False, 0, "guard error (fail closed): ..."`); check ordering (equity≤0 → cap → hard block on unpermitted margin → 90%/85%/75% thresholds) is sound.
- **`config.py`** — `RaptorConfig.validate_all()` correctly asserts API-key presence and trail-multiplier ordering; only gap is it's called from `main.py` alone, not from every entry point (minor, not a bug).
- **`dsr.py`** — the reference-correct implementation of the percentage normalization pattern (unconditional `/100.0` for both `position_pnl_pct` and `actual_pnl_pct`); no changes needed.

## File → script map (for future reference)

| File | Written by | Read by |
|---|---|---|
| `position_ledger.json` | `ledger.py` (canonical), `backfill_ledger.py`, `repair_ledger_20260619.py`, `repair_ledger_20260624.py` | `daily_recap.py`, `hold_monitor.py`, `outcome_tracker.py`, `raptor_monitor.py`, `raptor_dashboard.py`, `reconcile_positions.py`, `check_ledger_vs_alpaca.py`, `diagnose_system.py`, `factor_lab.py`, `morning_scanner_email.py`, `sync_to_claude.py` |
| `hold_health.json` | `hold_monitor.py` | `exit_monitor.py`, `watchdog.py`, `daily_recap.py`, `raptor_monitor.py`, `raptor_dashboard.py`, `reconcile_positions.py`, `diagnose_system.py` |
| `macro_context.json` | `macro_context.py` | `main.py`, `exit_monitor.py`, `outcome_tracker.py`, `agent_layer.py`, `raptor_monitor.py`, `raptor_dashboard.py`, `market_agent.py`, `diagnose_system.py` |
| `market_decision.json` | `market_agent.py` | `main.py` (via `load_market_decision`), `morning_scanner_email.py`, `raptor_monitor.py`, `raptor_dashboard.py`, `diagnose_system.py` |
| `outcome_log.json` | `outcome_tracker.py` | `daily_recap.py`, `dsr.py`, `kelly_engine.py`, `factor_lab.py`, `raptor_dashboard.py`, `raptor_monitor.py`, `sync_to_claude.py` |
| `outcome_pending.json` | `exit_monitor.py` | `outcome_tracker.py`, `raptor_monitor.py` |
| `position_outcomes.json` | manual rebuild (no automated writer — see Session 11 open item) | `dsr.py`, `raptor_monitor.py`, `sync_to_claude.py` |
| `slippage_log.json` | `slippage_tracker.py` | `daily_recap.py`, `raptor_monitor.py` |
| `kelly_estimates.json` | `kelly_engine.py` | `raptor_monitor.py`, `diagnose_system.py` |
| `trim_log.json` | `exit_monitor.py` | `daily_recap.py`, `raptor_dashboard.py`, `morning_scanner_email.py`, `diagnose_system.py` |
| `entry_vetoes.json` / `hold_decisions.json` | `agent_layer.py` | `exit_monitor.py`, `outcome_tracker.py`, `raptor_dashboard.py`, `diagnose_system.py` |
