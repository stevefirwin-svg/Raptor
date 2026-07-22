# Raptor — Master Priority Plan
*Last updated: 2026-07-06 (session 15 — "real money" data-integrity hardening, see below)*
*Supersedes all prior versions. This is the single source of truth.*

---

## The Standard

Every number must be derivable from a formula, empirical data, or an optimization.
If "why that number?" cannot be answered, the number is wrong.

**Data integrity corollary:** Real data or skip. Never fabricate a fallback value that looks real.

**Audit integrity corollary (Rule 11):** A fix is only DONE when grep/test output is pasted
in the same session confirming it. Documented-but-unverified fixes are NOT done.

**Independence corollary:** Multiple trim events from one position entry are NOT independent
observations. All gating, IC, and DSR calculations use `position_outcomes.json` (one record
per position entry), never raw `outcome_log.json` directly.

---

## Current System State (verified 2026-06-19)

| Component | Status |
|-----------|--------|
| **Raptor location** | ✅ `C:\Raptor` — moved from OneDrive 2026-06-19, 22 files patched, 19 tasks re-registered |
| submit_order on AlpacaDataFeed | ✅ FIXED 2026-06-05, AST-verified every session |
| Crash visibility (3 entry points) | ✅ LIVE 2026-06-10 |
| Deterministic entry gate | ✅ LIVE 2026-06-10 — math governs, LLM advises |
| Gmail credentials | ✅ FIXED 2026-06-10 — env var, rotated password |
| Leveraged/inverse ETP exclusion | ✅ LIVE 2026-06-10 (variance drain, Cheng & Madhavan 2009) |
| Implementation shortfall tracker | ✅ LIVE 2026-06-10 (slippage_log.json, Perold 1988) |
| Cross-sectional sector neutralization | ✅ LIVE 2026-06-10 (Grinold & Kahn 2000) |
| Deflated Sharpe Ratio | ✅ LIVE 2026-06-10 (Bailey & López de Prado 2014) |
| OU-derived hold target | ✅ LIVE 2026-06-11 (Leung & Zhang 2019) — replaces 16+14*atr_pctile |
| exit_regime in outcome records | ✅ LIVE 2026-06-11 |
| Survivorship bias warning in backtest | ✅ LIVE 2026-06-11 |
| Regime drift metric | ✅ LIVE 2026-06-11 |
| position_outcomes.json | ✅ LIVE 2026-06-12 — deduplicated position-level records |
| DSR corrected to position-level | ✅ FIXED 2026-06-12 — true DSR 59.8% (was falsely 99.9%) |
| Ledger integrity repair | ✅ FIXED 2026-06-19 — KDP/PFE/SQQQ ghosts closed, AAL trim backfilled (805→688) |
| outcome_tracker UnicodeEncodeError | ✅ FIXED 2026-06-19 — replaced → with -> in print statements |
| OneDrive sync conflict risk | ✅ ELIMINATED 2026-06-19 — moved to C:\Raptor outside OneDrive scope |
| P0-1 outcome sidecar | ✅ LIVE 2026-05-29 |
| P0-8 regime unification | ✅ LIVE 2026-05-29 |
| DFA-1 Hurst | ✅ LIVE 2026-05-29 |
| Spearman IC + WLS + decay | ✅ LIVE |
| Bootstrap Kelly — SHADOW mode | ✅ LIVE (53/100 trades) |
| Dual-book engine (MOMENTUM live, MR suspended) | ✅ LIVE |
| Bat file log isolation (raptor_auto_start.log) | ✅ FIXED 2026-06-10 |

---

## Data State (verified 2026-06-19)

| Metric | Value | Notes |
|--------|-------|-------|
| outcome_log.json total records | 135+ | Raw, includes all trim events |
| **Independent positions (position_outcomes.json)** | **27** | **Use this for all gating** |
| Clean positions (no quality flags) | 24 | Excludes leveraged ETPs |
| True DSR | 59.8% — WEAK | n=24, SR=1.42, SR*=1.22 |
| Win rate (position-level) | 59.1% | 13W/9L on clean positions |
| Mean position PnL | 5.47% | |
| Kelly mode | SHADOW (53/100) | Not yet active |
| Open positions | 7 | KRE, WFC, MRVL, BAC, WULF, UBER, AAL(688sh) — Alpaca/ledger synced |
| Closed trades in ledger | 40 | Post-repair 2026-06-19 |
| Equity | $106,915.78 | As of 2026-06-19 |
| slippage_log.json | 30+ records | Data flows from 2026-06-10 onwards |

**Ledger repair 2026-06-19:** KDP hard_stop (Jun 18), PFE hard_stop (Jun 18), SQQQ hard_stop (Jun 15)
closed in ledger. AAL trim (117sh @ $15.835, Jun 18) backfilled. Root cause: OneDrive conflict
overwrote position_ledger.json silently after rapid sequential writes. Eliminated by move to C:\Raptor.

---

## Gates (updated 2026-06-12)

| Gate | Metric | Current | Target | Unlocks |
|------|--------|---------|--------|---------|
| **DATA-40** | Independent positions in position_outcomes.json | 27 | 40 | MATH-1, GAP-B first pass |
| **DATA-60** | Independent positions | 27 | 60 | MATH-5, ARCH-1, Kelly shadow→active |
| **DATA-100** | Independent positions | 27 | 100 | Kelly ACTIVE mode |
| **DATA-200** | Position-days | ~430 | 200+ | ARCH-3 full covariance Kelly |

At current pace (~7 positions/week): DATA-40 in ~2 weeks, DATA-60 in ~5 weeks.

---

## Session 4 Fixes (all Rule 11 verified, commit 0fc61f0)

| ID | Fix |
|----|-----|
| S4-1 | Crash visibility: main.py, exit_monitor.py, hold_monitor.py — logger.exception to log file on uncaught error |
| S4-2 | Deterministic entry gate: _eval_entry_rules() — 6 boolean rules in Python, LLM advisory only |
| S4-3 | Gmail app password removed from daily_recap.py + morning_scanner_email.py → EMAIL_APP_PASSWORD env var |
| S4-4 | 9 bat files: auto_start.log → raptor_auto_start.log |
| S4-5 | universe_builder: leveraged/inverse ETP exclusion via name pattern (Cheng & Madhavan 2009) |

## Session 4b–4d Fixes (all Rule 11 verified)

| ID | Commit | Fix |
|----|--------|-----|
| S4b | be8f37b | slippage_tracker.py — IS recording on every BUY/SELL fill, backfill via outcome_tracker, section in recap email |
| S4c | 3857259 | signals.py sector neutralization — factor z-scores demeaned per sector before IVW (Grinold & Kahn 2000) |
| S4d | 3b647bd | dsr.py — Deflated Sharpe Ratio (Bailey & López de Prado 2014), wired into AfterClose + recap email |

## Session 5 Fixes (commit 57c08d7 + 0a30513)

| ID | Fix |
|----|-----|
| S5-1 | OU hold target: ln(2)/θ via AR(1) OLS on log prices replaces 16+14*atr_pctile (Leung & Zhang 2019) |
| S5-2 | exit_regime field in build_outcome_record — enables regime drift metric |
| S5-3 | Survivorship bias warning in backtest.py metrics dict (Brown, Goetzmann & Ross 1995; Shumway 1997) |
| S5-4 | _get_regime_drift() in daily_recap — regime transition matrix entry→exit |
| S5-5 | position_outcomes.json — 27 independent positions aggregated from 76 trim events |
| S5-6 | dsr.py updated to use position_outcomes.json — true DSR 59.8% (was falsely 99.9%) |

## Session 11 — Pipeline/Scheduling Audit (2026-07-01)

Full audit of fire order, frequency, journaling, and Alpaca↔ledger data-linking across
the whole daily pipeline, requested after repeated crashes and reconciliation gaps.
Method: read every Register_*.ps1/Start_*.bat, cross-checked against actual fire history
in logs/raptor_auto_start.log (3 weeks) and per-script logs. Full writeup:
`RAPTOR_PIPELINE_AUDIT_20260701.md`. Rule 11: verified against real log/JSON data in-session.

| ID | Fix |
|----|-----|
| S11-1 | `raptor_monitor.py` L3-PositionRisk crash — `float(None)` on `stop_dist_atr` for un-backfilled positions crashed the layer every run. Added `safe_float()`. Verified against real 9-position hold_health.json snapshot (5 with null stop_dist_atr) — no crash. |
| S11-2 | `daily_recap.py` — exit price showed `@ None` for market-sell exits (e.g. HOOD) instead of the real fill. Now cross-references `slippage_log.json`'s reconciled fill price by order ID. Verified against real 6/28 HOOD exit — now shows $99.81. |
| S11-3 | `Start_Monitor.bat` was silently calling `hold_monitor.py` instead of `raptor_monitor.py` — the 4:30 PM "Raptor Monitor" task would have stopped sending the EOD summary email with zero error, tonight. Logs through 6/30 confirm it used to call raptor_monitor.py correctly. Restored. |
| S11-4 | `reconcile_positions.py --fix` was documented and accepted as a flag but never implemented — passing it silently did nothing. Implemented: auto-runs `backfill_ledger.py --write` for MISSING_FROM_LEDGER symbols (GHOST_IN_LEDGER left for manual review — closing a ledger position is destructive). Wired into Start_AfterClose.bat as Step 0, so ledger↔Alpaca reconciliation now runs automatically every night instead of only when someone remembers. |
| S11-5 | `exit_monitor.py` had no process lock (unlike main.py, post the 2026-06-19 double-order incident). Added logs/exit_monitor.lock, same fail-open TTL pattern. Verified: concurrent acquire blocked, stale lock overwritten. |
| S11-6 | `exit_monitor.py` — the 3:50 PM safety-net call (Start_Afternoon_Monitor.bat) did nothing, every day: its cutoff check ran before the first cycle, so any invocation at/after 3:50 PM exited with 0 cycles. Confirmed live in exits_20260629.log: the 9:35 AM self-loop silently died after cycle 3 (10:35 AM, no traceback), and exits went unmonitored for ~4h45m until the safety net logged "0 cycles" and did nothing. Fixed: cutoff now only blocks a *new* cycle after at least one has run. |
| S11-7 | `raptor_monitor.py` L1 — added EXIT_LOOP_GAP check comparing actual vs. expected exit_monitor cycle count for time-of-day, so a repeat of 6/29 gets caught same-day instead of never. Verified against real logs: flags 6/29 (3 vs ~13 expected) ALERT, passes 6/30 (13/13) OK. |
| S11-8 | `main.py`, `exit_monitor.py` — UnicodeEncodeError on `→` in log messages (24 occurrences in raptor_run.log; logging module swallows the exception so the program didn't crash, but the log line was silently lost every time). Added stdout/stderr UTF-8 reconfigure (already present in raptor_monitor.py) + replaced the specific `→` occurrences with `->`. |
| S11-9 | ~~Added Register_Exit_Monitor.ps1 + Start_Exit_Monitor.bat~~ **RETRACTED same day** — written on the wrong assumption that the exit loop had no Task Scheduler entry. Steve pulled the real live task list and confirmed "Raptor Intraday Monitor" already runs `exit_monitor.py` directly at 9:35 AM and works correctly. Both files gutted (now refuse to run, explain why) so they can't be run by accident and register a duplicate. |

**CORRECTION 2026-07-01 (same day, after checking the live Task Scheduler with Steve):**
Everything below this line in the original session-11 writeup was inferred from repo files
only and turned out to be wrong or moot once we pulled the real task list/actions/triggers.
Full detail in `RAPTOR_PIPELINE_AUDIT_20260701.md`'s correction section. Summary:
- **Duplicate 5PM task — does not exist.** No "Raptor Analysis Lab" task is registered.
- **"Raptor Intraday Monitor" — not mislabeled/broken.** Its real Action is `python.exe
  exit_monitor.py` directly; it correctly runs the exit loop. `Start_Intraday_Monitor.bat`
  in the repo is dead code the live task never calls.
- **2026-06-29 exit-loop death — two hypotheses ruled out.** Confirmed 8-hour execution
  time limit (not a timeout kill) and sleep-after set to Never on AC/DC (not a sleep issue).
  Root cause still unconfirmed, most likely a one-off. The code fixes (S11-5/6/7) stand
  regardless — they'll catch a repeat same-day now instead of never.
- **Duplicate PreMarket firing — only one live trigger found** (9:00 AM, on "Raptor
  MacroContext"). The historical 9:28 second firing is unexplained but may not recur.
  Not chased further.
- **"Raptor Monitor" (4:30 PM) runs raptor_monitor.py directly**, not through
  Start_Monitor.bat — so S11-3's bat-file fix wasn't protecting anything actually at risk
  on the live system, though it's still correct to have fixed the file.

**No live Task Scheduler changes were needed.** All real fixes this session were in code.

**Still open (unrelated to the Task Scheduler correction above):**
- **position_outcomes.json still has no rebuild script** — bigger than a wiring fix; `rebuild_positions.py` referenced in RAPTOR_STARTUP.md doesn't exist. Needs a dedicated session using the same dedup logic that produced the original 27 independent positions, not an improvised rebuild.

## Session 15 — "Real Money" Data-Integrity Hardening (2026-07-06)

Steve's question after Session 14: "why does this keep failing, I want permanent fixes, solid
code logic as if this is running real money with real consequences." Fair challenge — worth
answering honestly before describing the fix.

**Why it kept failing:** every prior fix this session (S13-1, S14-1) was a correct but reactive
point-patch — closing the exact crash that had just been observed, in the exact field/file where
it happened, rather than asking "what's the general shape of this risk, and where else does it
live." S13-1 defaulted every `get_account()`/`get_positions()` field to 0/0.0 on `None`. That was
right for `daytrade_count` (informational, feeds no decision) but **wrong** for
equity/cash/buying_power/portfolio_value/qty/avg_entry_price/current_price — silently returning
`equity: 0.0` when Alpaca fails to report equity is itself a fabricated value that *looks real*
to every downstream consumer (margin math, position sizing, exit quantities). That is precisely
what this document's own **Data integrity corollary** already prohibits: *"Real data or skip.
Never fabricate a fallback value that looks real."* Session 13's own fix violated the project's
oldest stated principle while fixing the crash it was aimed at — worth owning plainly rather than
glossing over.

**S15-1 — the permanent fix: split every Alpaca field into CRITICAL (raise) vs. COSMETIC
(default), instead of defaulting everything.**
`data_feeds.py` gets a new `AlpacaDataError` exception and a `require_float(value, field)`
helper (raises `AlpacaDataError` naming the exact field on `None`/unparseable, no default).
- `get_account()`: `equity`, `cash`, `buying_power`, `portfolio_value` → `require_float`
  (capital-critical — feed `main.py`'s position sizing, `margin_guard.py`'s checks,
  `daily_recap.py`'s cap-headroom math). `daytrade_count` → stays `safe_int` (PDT-compliance
  display only, feeds no sizing/risk decision).
- `get_positions()`: `qty`, `avg_entry_price`, `current_price` → `require_float`
  (position-critical — `qty` feeds exit order size and the duplicate-entry check in `main.py`'s
  `all_held`; `current_price*qty` feeds market-value/margin/capital-utilization math). A single
  bad position raises for the **whole call**, not just that symbol — a positions list silently
  missing one real holding is more dangerous than aborting the cycle (main.py could re-buy a
  symbol it thinks is flat; exit_monitor could miss a stop on a symbol it thinks doesn't exist).
  `unrealized_pnl`/`unrealized_pnl_pct` stay `safe_float`-defaulted (informational/analytics,
  self-correct next cycle, not sizing-critical).
- **This is not new exception-throwing risk.** Every single caller already wraps these calls in
  a try/except with fail-closed handling — `margin_guard.py` explicitly: *"Fail CLOSED... previous
  behavior was a silent capital risk"*; `main.py`/`exit_monitor.py`/`hold_monitor.py`'s `__main__`
  blocks log FATAL and abort; `raptor_monitor.py` catches per-call since S14-1. A raise here is
  caught exactly the way an opaque `TypeError` already was on 2026-07-06 — just with a clear,
  typed, field-named error instead of an arbitrary conversion crashing wherever it lands, and
  applied consistently to every critical field instead of only whichever one Alpaca happened to
  null out this time.

**S15-2 — closed the one entry point missing crash visibility.** `watchdog.py`'s `__main__` had
no try/except at all, unlike `main.py`/`exit_monitor.py`/`hold_monitor.py` (S4-1, 2026-06-10).
An uncaught exception — including the new `AlpacaDataError` — would have no guaranteed log-file
trace on the one script running the SPY circuit breaker and hard-stop execution every 15 minutes.
Added the identical `try/except SystemExit: raise / except BaseException: logger.exception(...)`
pattern.

**S15-3 — added a permanent regression test, not just a one-off manual check.**
`test_data_feeds_safety.py` (repo root, `python test_data_feeds_safety.py`, no pytest dependency)
constructs fake Alpaca account/position objects with `None` in each field and asserts: critical
fields raise `AlpacaDataError` naming the field/symbol; cosmetic fields default safely; one bad
position among otherwise-good ones still raises for the whole batch. This is the actual
"permanent" part of "permanent fix" — if a future edit reverts a critical field back to a silent
default, or adds a new Alpaca field without deciding which bucket it belongs to, this fails loudly
in a 5-second local run instead of the bug being rediscovered live at 9:35 AM again. 12/12 passing,
verified against an isolated reconstruction of the shipped logic (see verification note below).

**Verified (Rule 11):** `py_compile` clean on `data_feeds.py` and `watchdog.py`. Every existing
call site of `get_account()`/`get_positions()` re-checked for fail-closed handling on the new
raise: `main.py`, `exit_monitor.py`, `hold_monitor.py` (crash-visibility, abort/log FATAL),
`margin_guard.py` (explicit fail-closed, blocks entries), `daily_recap.py` (caught by `main()`'s
outer handler), `raptor_monitor.py` (S14-1's per-call try/except), `watchdog.py` (S15-2, this
session), `reconcile_positions.py` (try/except → exit(1)). Manual/diagnostic-only scripts
(`check_account.py`, `diagnose_system.py`, `check_ledger_vs_alpaca.py`, `backfill_ledger.py`) were
left unwrapped deliberately — a human is watching those run, an uncaught traceback there is the
correct, informative behavior. `crypto_engine.py`/`options_engine.py` confirmed dead code
(Session 12), not touched.

**Verification note — this sandbox's mount of `C:\Raptor` was unreliable mid-session** in a new
way beyond the previously-documented stat-cache lag (S12-5): direct file reads of `data_feeds.py`
returned two different byte lengths (27,929 vs. 29,238) across nearly-simultaneous calls in the
same shell session, and `import data_feeds` silently loaded without exposing the newly-added
`AlpacaDataError`/`require_float` names even after clearing `__pycache__`. Worked around by
reconstructing the file from `Read`-tool ground truth (host-side, authoritative per S12-5's own
precedent) into a fully isolated `/tmp` directory outside the mount entirely, where
`test_data_feeds_safety.py` ran clean at 12/12. The file on disk (verified via `Read`) is correct;
this is a sandbox-verification limitation, not a code defect — flagging so a future session
doesn't waste time re-diagnosing the same mount quirk.

| ID | Fix |
|----|-----|
| S15-1 | `data_feeds.py` — `AlpacaDataError` + `require_float()`; capital/position-critical fields (equity, cash, buying_power, portfolio_value, qty, avg_entry_price, current_price) now raise instead of silently defaulting to 0/0.0. Corrects S13-1's own violation of this doc's "Data integrity corollary." |
| S15-2 | `watchdog.py::__main__` — added the S4-1 crash-visibility pattern (only entry point missing it). |
| S15-3 | `test_data_feeds_safety.py` (new file) — permanent regression test, 12/12 passing, locks the critical-vs-cosmetic contract in place against future silent reversion. |

## Session 14 — Raptor Monitor Findings Audit (2026-07-06)

Steve asked to read today's 4:30 PM `logs/monitor_20260706.json`/`monitor_run_20260706.log`
(22 findings: 3 ALERT, 5 WARN) and explain + fix everything it flagged. Traced every finding
to a root cause instead of patching symptoms; several turned out to share one cause.

**Root cause chain — one bug explains 3 of the 22 findings:**
`data_feeds.py::get_account()`'s `daytrade_count=None` crash (fixed earlier today, S13-1) is
called from `main.py`, `exit_monitor.py`, `watchdog.py`, AND `raptor_monitor.py` — all four
route through the same shared function. Before the fix landed:
- `exit_monitor.py` crashed at the **same line, every single cycle today** (13/13, 9:35 AM–3:35
  PM, `exits_20260706.log`) — meaning no exit/trim/stop check ran on any open position all day.
- `main.py`'s 9:35 AM entry scan crashed at the same call (`raptor_20260706.log`) before ever
  reaching signal generation — zero entries evaluated today, and it never reached the
  cooldown-clear step (`main.py` L287, `_cooldown_filter`), which is why L4's
  `EXPIRED_COOLDOWNS` finding (GOOGL, UBER, XLU, QXO, TSLA, STM) hadn't cleared — not a
  separate bug, just downstream of the scan never completing. Resolves itself on the next
  successful `main.py` run now that S13-1 is in.
- `raptor_monitor.py` itself hit the same crash at 16:30:02 inside `get_alpaca_positions()`.

**S14-1 — found & fixed: `get_alpaca_positions()` coupling caused a false GHOST_IN_LEDGER alarm.**
`raptor_monitor.py`'s `get_alpaca_positions()` fetched `positions` then `account` inside one
`try` block — a failure in `get_account()` discarded the *already-successfully-fetched*
`positions` list too, returning `([], {})`. Today's monitor run hit exactly this: `get_account()`
crashed (pre-fix), positions got discarded, and L2-Reconciliation compared the ledger against
an empty Alpaca position list, flagging **all 10 real open positions** (AAL, BAC, CRWV, EWZ,
GOOGL, HOOD, IGV, NEE, NOW, WULF) as `GHOST_IN_LEDGER` — a false alarm from this coupling, not
real ledger/broker drift. **Did not run the monitor's own suggested action**
(`backfill_ledger.py --write`) — that closes ledger positions and would have been destructive
against a false signal. Fixed instead: split into three independent try/excepts (connect,
get_positions, get_account) so a failure in one no longer erases another's already-fetched
result. Verified: `py_compile` clean; functional test confirms positions survive a simulated
`get_account()` `TypeError` instead of being discarded to `[]`. Recommend re-running
`raptor_monitor.py` (or waiting for tomorrow's 4:30 PM run) to confirm GHOST_IN_LEDGER clears
on its own now that both bugs are fixed — did not force a re-run from this session.

**Found, flagged for Steve — Task-Scheduler-level, can't confirm from the repo alone (see
Session 11's own precedent: repo-file assumptions about live tasks were wrong until Steve
pulled the real Task Scheduler list):**
- **`STALE_MARKET_DECISION_JSON` (245.8h / ~10.2 days old).** `market_agent.py` says it's
  "Scheduled via Task Scheduler at 9:15 AM ET" but **no `Register_*.ps1` for it exists in the
  repo** (unlike watchdog, midday monitor, raptor monitor, afterclose — all of which have one).
  `main.py` only *reads* `market_decision.json` (`load_market_decision`) — it never regenerates
  it. Last write was 2026-06-26T14:40 UTC, matching the finding exactly. Net effect: for ~10
  days, every entry scan's market-agent gate has been silently hitting S12-9's own >12h
  fail-closed default (REDUCE, 0.75 scalar) — correctly fail-closed by design, not dangerous,
  but quietly cutting every day's entry sizing to 75% without a visible alert until today's
  monitor caught the staleness. **Needs Steve to check whether a "Raptor MarketAgent" (or
  similar) task actually exists live** — if not, needs a `Register_MarketAgent.ps1` (9:15 AM,
  `python market_agent.py`) drafted and added, matching the exact gap-and-fix pattern from
  Session 11's exit-loop registration.
- **`watchdog.py` did not run at all today** — no `logs/watchdog_20260706.log` was created
  (every other watchdog-covered day has one), and it doesn't appear in `raptor_auto_start.log`
  either (though that log only captures bat-wrapped tasks, so absence there alone isn't
  conclusive — `raptor_monitor.py` isn't in it either despite definitely running today).
  Distinct from the `get_account()` crash chain above: a crash would still produce a dated log
  file (logging is configured before any Alpaca call), so this looks like the "Raptor Watchdog"
  task itself never launched today, not a Python-level failure. Needs Steve to check
  `Get-ScheduledTask "Raptor Watchdog"` / its run history directly — not something confirmable
  from repo files per Session 11's own lesson.

**Investigated, found benign — not a new bug:**
- **`STALE_OUTCOME_PENDING` (82 sidecars, up to 47 days old) + `outcome_log.json`/
  `outcome_pending.json` untouched since 2026-07-02 despite `Start_AfterClose.bat` "completing"
  on 7/3 and 7/6.** Ran `outcome_tracker.py` directly this session — it executes cleanly with no
  code-level exception (the only failure is this sandbox's own blocked network egress to
  Alpaca, a tooling limitation, not a repo bug). Checked `exits_20260703.log`: zero SELL/fill
  events that day (one transient DNS blip mid-day, self-recovered next cycle, 12/13 cycles
  clean) — so there was genuinely nothing new for `outcome_tracker.py` to tag on 7/3, and 7/6
  had zero fills too (exit_monitor was down all day from the crash above). Both "stale" dates
  are explained by "no new trades that day," not a broken AfterClose step. The 82-item backlog
  itself (some 47 days old) is a pre-existing, separate accumulation — plausibly connected to
  the already-documented S12-8 fragmented-trade-record issue (several overlapping symbols:
  AMD, DKNG, CSX, TSLA) — but confirming that link and safely resolving 82 individual sidecars
  needs a dedicated session with real Alpaca access, not a guess from this sandbox. Did not
  attempt to force-resolve any of them.
- `Start_AfterClose.bat` (and every other `.bat` entry point) doesn't check any step's exit
  code — a crash in `outcome_tracker.py` would still let the bat file print "complete" and
  move on. Confirmed benign *this time* (no crash occurred, just no new data), but the same
  blind-spot Session 12's audit already fixed once for `premarket_scanner.py`
  ("swallowed all step failures and always exited 0... now tracks per-step success") exists
  across all six `Start_AfterClose.bat` steps and was not addressed this session — flagging,
  not fixing, since it's a bat-file-wide change outside tonight's scope.

| ID | Fix |
|----|-----|
| S14-1 | `raptor_monitor.py::get_alpaca_positions()` — decoupled `get_positions()`/`get_account()` into independent try/excepts so one failing no longer discards the other's already-fetched result. Root cause of today's false `GHOST_IN_LEDGER` (10/10 real positions flagged). Verified: `py_compile` clean, functional test confirms positions survive a simulated `get_account()` failure. |

## Session 13 — Recap Crash Fix + Same-Class Audit (2026-07-06)

Daily recap failed this morning (4:15 PM `Start_Recap` task). `recap_errors.log`
2026-07-06T16:15:04: `daily_recap.py` crashed in `get_account_data()` →
`data_feeds.py::get_account()` — `int(acct.daytrade_count)` threw
`TypeError: int() argument ... not 'NoneType'`. Alpaca returned
`daytrade_count=None` for the account. No recap email sent.

Steve's question after the first fix ("why did this happen, we've gone over
daily recap so many times") prompted a full audit rather than stopping at the
one field — actual crash history is only 3 incidents total (5/28 build_html
arg mismatch, 6/12 NameError typo fixed same day, today), each a genuinely
different root cause, but `get_account()` converted **9 fields** straight
from raw Alpaca objects with bare `float()`/`int()` and only 1 had ever
broken and gotten fixed. The other 8 were the same latent class, unfixed
until now. Same audit extended to `daily_recap.py`'s own ledger/outcome-log
ingestion, and a previously-known-but-never-closed gap in `hold_monitor.py`'s
file locking.

| ID | Fix |
|----|-----|
| S13-1 | `data_feeds.py::get_account()` — added `safe_int()`/`safe_float()` helpers (same None-but-present-key pattern as `raptor_monitor.py`'s existing `safe_float()`, now logs a warning on every substitution instead of silently defaulting). Applied to **all 5** `get_account()` fields (`equity`, `cash`, `buying_power`, `portfolio_value`, `daytrade_count`) and **all 5** `get_positions()` fields (`qty`, `avg_entry`, `current_price`, `unrealized_pnl`, `unrealized_pnl_pct`) — not just the one that crashed today. Verified: reconstructed-copy `py_compile` clean (live `C:\Raptor` bash mount was stale/truncated at 729 of 757 real lines — same known issue as S12-5); functional test confirms `cash=None`, `avg_entry_price=None`, `daytrade_count=None` all degrade to a logged default instead of crashing. |
| S13-2 | `daily_recap.py` — added `_first_not_none(d, *keys, default)` helper and applied it at 5 call sites (`get_realized_total`, `get_pnl_windows`, `get_portfolio_analytics`'s returns list and its `cap_efficiency` calc, `get_days_held`) that used the fragile `d.get(primary, d.get(secondary))` chain. That chain only falls back to `secondary` when `primary` is *absent* — a record with `primary` explicitly `null` returns `None` and skips the fallback entirely (same failure class as S13-1, confirmed live: `outcome_log.json`/`position_outcomes.json` carry explicit nulls in `hold_days`, `entry_price`, `actual_pnl_pct`, `entry_date`, `entry_regime`, `exit_regime` on real records today). Two of the five (`cap_efficiency`'s `pnl_usd`/`entry_value`) had no `is not None` guard at all and would have crashed exactly like S13-1 the first time either came back null on a real ledger record; the other three would have silently under-counted realized P&L instead of crashing. `position_ledger.json` itself has zero nulls today (verified), so this changes no current output — it closes a dormant crash/undercount risk. Verified: reconstructed-copy `py_compile` clean; functional test with simulated nulls confirms `get_realized_total` recovers the correct total ($350 from two trades, one with `pnl=None`/`pnl_dollar=150`) instead of dropping data, and `get_portfolio_analytics` no longer crashes with `pnl_usd`/`entry_value`/`market_value` nulls. |
| S13-3 | `hold_monitor.py::save_history()` (writes `hold_history.json`) had **no lock at all** — the exact gap that caused the 2026-06-11 crash (`WinError 32`, `hold_history.json.tmp` → `hold_history.json`, "process cannot access the file") when `run_monitor()`'s three call sites (Raptor MidDay Monitor 12:30 PM, Start_Afternoon_Monitor 3:50 PM, `daily_recap.py`'s inline call ~4:15 PM) collide. `position_ledger.json` and `slippage_log.json` got this exact treatment in the 2026-07-01 audit (S12-1, S12-9); `hold_history.json` was missed. Added `hold_history_lock()` to `ledger_lock.py` (thin wrapper over the existing generic `_file_lock()`, own mutex file, same fail-open 20s-timeout/120s-stale-clear behavior as the other two locks) and wired it around both call sites: `hold_monitor.py`'s `__main__` block and `daily_recap.py`'s inline `run_monitor()` call. Verified: `py_compile` clean on both files; acquire/mutex-file-creation confirmed working in-session. Release-side `os.remove` couldn't be independently re-verified from this sandbox — the bash mount showed "Operation not permitted" on cleanup, but reproduced identically on the **already-shipped, already-live-verified** `slippage_lock` using the same underlying `_file_lock` code, confirming it's a sandbox/mount artifact and not a defect in the new lock. Two stray test lock files (`logs/hold_history.mutex`, `logs/slippage_log.mutex`) are left over from this test — harmless (fail-open, auto-clear after 120s), safe to delete manually. |

**Not fixed, flagged only:** `_get_regime_drift()` takes a `trades` parameter it never uses (reads `outcome_log.json` directly instead) — dead parameter, not a bug, left alone since changing it wasn't asked for and touches PnL-adjacent code. `hold_monitor.py`'s `int(dh)` on `hold_health.json`'s `days_held` (line ~653) is inside a blanket `try/except` that would silently drop remaining symbols on a bad value — low severity (degrades gracefully already, no current bad data), not fixed this session.

## Session 12 — Senior-Dev Deep Debug (2026-07-01)

Requested after Session 11: "no more silent crashes, no more discrepancies, every monitor
communicates with each data pipeline with no issues." Focus: find what Session 11 didn't
catch, specifically the Alpaca↔ledger discrepancies Steve has hit repeatedly with zero
traceback anywhere.

**S12-1 — FOUND & FIXED: lost-update race condition between watchdog.py and exit_monitor.py.**
Both processes independently do `Ledger()` load → mutate in memory → `_save()` (atomic
tmp+`os.replace()`, but replaces the *whole file*). watchdog.py runs every 15 min as a fresh
process; exit_monitor.py runs every 30 min inside one long-lived self-looping process. No
coordination existed between them. Concretely: if watchdog fires a hard-stop and calls
`record_exit()` while exit_monitor is mid-cycle (already loaded its own, now-stale snapshot),
exit_monitor's next `_save()` silently overwrites watchdog's exit and the position reappears
as ACTIVE in the ledger — genuinely flat on Alpaca, "open" in the ledger, no exception raised
anywhere. This reproduces exactly the GHOST_IN_LEDGER/MISSING_FROM_LEDGER symptom class.
Fix: new `ledger_lock.py` — a cross-process mutex (`os.O_CREAT|O_EXCL`, portable Windows/POSIX)
held for the *entire* load-through-save span, not just individual writes. Fails open (proceeds
with a logged warning) after a 20s timeout rather than ever blocking trading, consistent with
the existing lock philosophy (`main.py`'s `raptor_scan.lock`, `exit_monitor.py`'s own lock).
Wired into both `watchdog.py` and `exit_monitor.py`'s `__main__` blocks.
Verified: isolated test harness confirmed (a) normal acquire/release, (b) a second acquirer
correctly blocks while the lock is held and fails open after timeout with a warning, (c) a
stale lock (>120s old) is detected and cleared rather than waited out. Full run log pasted
in-session.

**S12-2 — FOUND & FIXED: same class of bug as L3, in Layer 5 (Macro) of raptor_monitor.py.**
`macro_context.py` writes `{"value": None, "regime": "UNKNOWN"}` for VIX (and other signals)
whenever the live fetch fails. L5 read `vix_data.get("value", 0)` — which only substitutes
the default when the key is *absent*, not when it's present with an explicit `None` — so a
failed VIX fetch crashed the whole layer. Fixed with the same `safe_float()` / `or {}` guards
already used elsewhere in the file (L3, and every other consumer of this field in the codebase
except this one call site).

**S12-3 — Repo-wide silent-exception sweep (AST-based, not just grep).** Walked every
`except: pass` in the live pipeline files (watchdog.py, hold_monitor.py, outcome_tracker.py,
agent_layer.py, signals.py, macro_context.py, ledger.py, backfill_ledger.py, main.py,
exit_monitor.py, market_agent.py, data_feeds.py, universe_builder.py, slippage_tracker.py,
margin_guard.py). Every hit found is either (a) a safe fallback to a sensible default
(hold_monitor's history/health load failures → `{}`; watchdog's SPY/ATR fetch failures →
0.0/proxy, matching its own "circuit breaker disabled on error = safe" design) or (b) a
narrow, deliberate, already-documented control-flow catch (agent_layer's JSON-parse retry on
`JSONDecodeError`; signals.py's `LinAlgError` guard on a singular OLS matrix, falls back to
`reliable: False` rather than fabricating a number). No new silent-crash risk found beyond
S12-2. `options_engine.py`'s 3 bare `except:` clauses are dead code — not imported anywhere
in the live pipeline, confirmed via grep.

**S12-4 — Ledger key-format consistency check.** Verified the `"v5.4:{symbol}"` ledger key
prefix is used identically across every writer/reader (main.py, exit_monitor.py, watchdog.py,
daily_recap.py, both repair scripts) — no schema drift found on this dimension.

**S12-5 — Compile-integrity scare, resolved: sandbox mount staleness, not a real bug.**
Mid-session, `py_compile` via the bash sandbox reported a syntax error in `exit_monitor.py`
("'(' was never closed", line 958) and, separately, a null byte in `market_agent.py`. Root
cause: the bash sandbox's mount of `C:\Raptor` was frozen at old snapshots (mtimes from
6/19–6/29, predating this session's edits and in one case predating the file's real content
entirely) — a worse case of the previously-known stat-cache lag. Confirmed via the Read tool
(host-side, authoritative) that every edited file is complete and well-formed, then
reconstructed all 7 touched files (`exit_monitor.py`, `watchdog.py`, `main.py`,
`raptor_monitor.py`, `daily_recap.py`, `reconcile_positions.py`, `ledger_lock.py`) into a
sandbox scratch directory from the Read tool's live content and ran `py_compile` there —
**all 7 compile clean.** Takeaway: the bash mount cannot be trusted for verification on this
repo without reconstruction; Read-tool content is ground truth.

**S12-6 — Full JSON schema audit, done as a follow-up same day.** Full writeup in
`RAPTOR_JSON_SCHEMA_AUDIT_20260701.md`. Headline findings, not yet fixed (flagged for next
session):
- **`pnl_pct` unit-guessing heuristic in `daily_recap.py`** (`if abs(pnl_f) < 1.5: *100`)
  misfires on any real trade returning -1.5%..+1.5%, displaying e.g. +1.2% as +120%. No
  current writer needs the heuristic — recommend removing it.
- **`"regime"` vs `"macro_regime"`** — two coexisting key names for the same concept (in-memory
  `macro` dict vs. the persisted `macro_context.json`/`market_decision.json` files). Already
  caused one confirmed bug (outcome_tracker.py, 2026-06-26) and is now patched ad hoc with
  `or` fallbacks in 4+ places instead of normalized once at the source.
- **`repair_ledger_20260619.py`** (predates the 2026-06-29 multi-trim P&L fix) may have written
  an incorrect headline `pnl`/`pnl_pct` for any symbol it repaired that had trims before the
  repair — those numbers still feed Sharpe/DSR/win-rate today. Needs a manual cross-check.
- Two lower-severity items (unused `pnl_pct` field on backfilled open positions;
  `raptor_dashboard.py` computes a merged closed-trade list it never uses) — see doc for detail.

**S12-7 — Same-day fixes applied for S12-6's findings A/B, plus corrections after deeper checking:**
- **Fixed:** `data_feeds.py`'s `get_full_dataset()` now aliases `macro["macro_regime"]`/
  `macro["macro_score"]` from `regime`/`score` at the single point that dict is built, closing
  the naming split at the source instead of leaving every consumer to guess (additive only —
  never removes the original keys, so nothing existing can break).
- **Fixed:** removed `daily_recap.py`'s `pnl_pct` decimal-guessing heuristic — no writer ever
  produces a decimal-scale value there, so it had no real case left to catch and was silently
  corrupting the display for any trade returning -1.5%..+1.5%.
- **Fixed:** `raptor_dashboard.py`'s "recent closed trades" now reads from the
  (deduped-by-symbol+exit_date) `closed_all` merge instead of `outcome_log.json` alone, so
  ledger-only closes (manual repairs, or any run where `outcome_tracker.py` itself failed) show
  up.
- **Retracted (no fix needed):** the "stray `pnl_pct` on open positions" finding — re-reading
  `backfill_ledger.py`'s full write path showed that field is only used for its own dry-run
  print, never passed to `ledger.record_entry()`. Never reached the ledger.
- **Retracted as originally scoped, but escalated to something bigger (S12-8 below):** the
  `repair_ledger_20260619.py` trim-aggregation concern — checked the live ledger directly and
  found KDP/SQQQ already carry a `"backfill_note": "Corrected 2026-06-29 — audit P0-1"` with
  correct aggregated `pnl`/`pnl_pct` (verified by hand-summing each trim). PFE never had prior
  trims. This specific question was already resolved in a prior session.

**S12-8 — NEW discovery while verifying S12-7. DECISION (Steve, 2026-07-01): leave history
as-is, documented only, no ledger values changed.**
While checking the live `position_ledger.json` for S12-7, found that the historical
"math_trim routing bug" (independently documented in the ledger itself — restored by
`backfill_positions.py` 2026-05-27) left **19 excess fragmented "closed" trade records**
(28 fragments representing 9 real trades) from 2026-05-15 through 06-18 across KDP (4
fragments), CVE (4), PLTD (4), AMD (3), INTC (3), SMCI (3), DKNG (3), CSX (2), TSLA (2). Each
real, continuous holding period for these symbols is currently split across 2-4 separate
`closed` records (same `entry_price` to 6 decimals, different fake `entry_date` stamps from
repeated backfill-then-refragment cycles) — everything from 2026-06-05 onward is clean and
unaffected. This inflates trade count and skews the realized-return distribution feeding
Sharpe/Sortino/DSR/win-rate/expectancy, and directly affects the `DATA-40`/`DATA-60`/`DATA-100`
Kelly-activation gate counters in `raptor_monitor.py` Layer 4 — conflicts with the
"Independence corollary" already established for `position_outcomes.json`. Full symbol-by-symbol
table in `RAPTOR_JSON_SCHEMA_AUDIT_20260701.md` (Finding F). **No values in `position_ledger.json`
were touched** — `pnl`, `pnl_pct`, `entry_date`, `exit_date`, and share counts on all 28 fragments
are exactly as they were. This is purely a documentation note so that whenever
`rebuild_positions.py` (Session 11's open item) or any future trade-count-sensitive analysis
gets built, it accounts for these 9 symbols producing 19 fewer independent trades than a raw
count of `closed` suggests.

**Not completed this session (lower priority, flagged for a future pass):**
- Full line-by-line cross-check of every ontology math claim (Kelly, DSR, trail-multiplier
  tiers) against RAPTOR_MASTER_PLAN.md's stated formulas — spot-checked several during S12-1/2
  but not exhaustively re-derived.
- Field-by-field schema pass on `hold_health.json`'s full snapshot shape, `kelly_estimates.json`,
  and `slippage_log.json` (S12-6 covered field-name/unit consistency for the highest-traffic
  fields only, not every field in every file).

**S12-9 — Tier 1/2 logic & debugging audit (2026-07-01, same day): margin_guard.py,
kelly_engine.py, dsr.py, config.py, slippage_tracker.py, universe_builder.py,
premarket_scanner.py, market_agent.py.** Full detail in
`RAPTOR_JSON_SCHEMA_AUDIT_20260701.md`'s "Tier 1/2" section. Summary:
- **Fixed:** `kelly_engine.py`'s `load_outcomes()` had the same decimal/percentage
  ambiguity bug as Finding A, but worse — near-breakeven trades (|return|≤1%, e.g. WFC
  +0.41%, CSX/UBER -0.20%) were left un-normalized and fed into the Kelly f*=μ/σ² math
  as ~40x-inflated returns, corrupting SHADOW-mode diagnostics now and would have
  corrupted ACTIVE-mode production sizing at n≥100 trades/book. Now matches `dsr.py`'s
  correct unconditional `/100.0` normalization. Also fixed: `universe_builder.py`'s
  `sensitivity_report()` was comparing against stale pre-sweep thresholds (500K/$20M
  instead of the live 750K/$30M filters); `premarket_scanner.py` swallowed all step
  failures and always exited 0, so Task Scheduler had no way to detect a FATAL
  macro/market-agent failure — now tracks per-step success and exits non-zero on failure.
- **Fixed same day, per Steve's go-ahead ("fix both"):** `market_agent.py`'s
  `load_market_decision()`/`evaluate_session()` fail-open bug — all three fallback paths
  (file missing, unreadable, >12h stale) now default to `REDUCE`/`risk_scalar=0.75`
  (new `FAIL_CLOSED_SCALAR` constant) instead of full-size `SCAN`, matching
  `margin_guard.py`'s fail-closed design elsewhere in this codebase. `main.py` already
  had a first-class `REDUCE` handler (`my_equity *= risk_scalar`), so no other files
  needed changes. Also fixed: `slippage_tracker.py`'s `record_fill()`/`backfill_slippage()`
  had no lock around their read-modify-write of `slippage_log.json` — generalized
  `ledger_lock.py`'s mutex into a reusable `_file_lock()` primitive and added a
  `slippage_lock()` wrapper (separate mutex file, `slippage_log.mutex`, so it never
  contends with the ledger's own lock) around both functions' full load-through-save span.
- **Clean, no issues:** `margin_guard.py`, `config.py`, `dsr.py` (the latter is the
  reference-correct pattern kelly_engine.py now matches).
- Steve confirmed crypto_engine.py is unused — dropped from the audit scope, no further
  action needed.

## Session 10 — Full System Audit Fixes (2026-06-29)

Source: `Raptor_v5.4_Full_System_Audit.docx` punch list, worked in priority order per
standing instruction. All fixes below Rule-11 verified (AST + functional tests pasted
in-session; `C:\Raptor`'s bash mount has a known stat-cache lag, so verification was done
by reconstructing each edited block into a sandbox and running `ast.parse()` + targeted
unit tests against it).

| ID | Fix |
|----|-----|
| S10-1 | `ledger.py` — fixed multi-trim P&L aggregation bug: partial trims against the same position entry were not being correctly rolled up into realized P&L. |
| S10-2 | `slippage_log.json` / `outcome_pending.json` — audit flagged possible corruption; on inspection both had already self-healed (valid JSON, no action needed — confirmed via load test, not assumed). |
| S10-3 | Sidecar JSON loaders (slippage/outcome_pending and related) now `logger.error`/`warning` explicitly on parse failure instead of silently falling back to an empty default — a failed load was previously invisible. |
| S10-4 | **Security:** `data_feeds.py::FREDDataFeed._fetch_series` was logging the live FRED `api_key` in plaintext to `logs/` on every failed request — `requests`' `HTTPError`/`Timeout` embeds the full request URL (including the `api_key` query param) in `str(e)`, and the handler logged `e` directly. Added `_redact_api_key()`, applied before the `logger.error` call. `submit_order` (data_feeds.py:200, AlpacaDataFeed) re-verified untouched per Skill Rule 7. |
| S10-5 | `signals.py::Signal` — removed dead `sentiment_score` field. Hardcoded to `0.0` at both construction sites since the sentiment feed was disabled 2026-05-22 (P1-15); confirmed via grep that no downstream consumer (`hold_monitor.py`, `exit_monitor.py`, `daily_recap.py`) ever reads it. Not one of the 16 protected factors — dropping it does not conflict with "do not modify factors." See RAPTOR_ONTOLOGY.md P1-15, now marked FIXED. |
| S10-6 | `signals.py::generate_signals` — `vol_ratio` has a statistically significant **negative** IC (-0.1692, t=-3.11, n=331; `factor_ic_report.json` 2026-06-26). Caveat surfaced to Steve before acting: `n_outcome=0` — the IC rests entirely on the noisier `hold_history.json` secondary source, not yet confirmed against a single realized closed-trade outcome. Steve's explicit call: do not remove it from the 16-factor structure (preserves "do not modify factors" / the 208% backtest shape) — halve its weight and redistribute the freed share proportionally to the 5 current top-IC factors (`accum_dist` 0.40/t=7.90, `adx_dir` 0.25/t=4.60, `rel_strength` 0.17/t=3.17, `price_cloud` 0.13/t=2.37, `rev_momentum` 0.13/t=2.34). Functional test confirmed the post-redistribution weight ratio is exactly 0.5x and only the 5 named factors gain share. |
| S10-7 | `exit_monitor.py` — EXIT5 (time-decay thesis check) read `hold_health.json` with no freshness check. `hold_monitor.py` only runs 9:28 AM + 3:50 PM; a crashed/skipped run leaves the file silently stale for hours while `exit_monitor`'s 30-min loop keeps reading it. Added per-symbol staleness detection (per RAPTOR_STARTUP.md's existing "timestamped today" convention) — stale symbols now skip EXIT5's deterioration check and default to hold rather than act on outdated composite/health data. |
| S10-8 | `hold_monitor.py` — cosmetic display bug: a missing real stop (`stop_dist_atr is None`) was coerced to `0.00 ATR` in both the per-symbol log line and the daily recap HTML table — visually identical to a position genuinely sitting *at* its stop (`stop_dist_atr == 0.0`, the dangerous case). Both display sites now render `—` when there's no real stop, so the two cases can't be confused. |

## Session 9 — Repo Audit (2026-06-28)

**Critical infra finding — CRLF line-ending corruption risk (not yet fixed, needs Steve decision):**
`git diff --stat` showed 347 files "modified" with ~114,460 insertions / 114,455 deletions —
nearly every tracked file. Root cause: the working tree on `C:\Raptor` has CRLF line endings
(`file signals.py` → "with CRLF line terminators") while the git history is LF, and there is no
`.gitattributes` and no `core.autocrlf` set. Confirmed via `git diff --ignore-space-at-eol`:
**only 1 file has a real content change** (`logs/github_push.log`, +5 lines). Everything else
is pure whitespace/EOL noise. Risk: the next `git add -A && git commit` (e.g. via
`Daily_GitHub_Push.bat`) will commit a ~115K-line diff across 346 files for zero functional
change, permanently polluting `git log` / `git blame` and burying any real future diff in noise.
**Fix applied and committed this session:** added `.gitattributes` with `* text=auto eol=lf`,
ran `git add --renormalize .`, and committed. `core.autocrlf` set to `false` to keep future
commits clean given the working tree stays CRLF on disk (Windows) while git stores LF.

**Fixes applied and verified this session (Rule 11):**
| ID | Fix |
|----|-----|
| S9-1 | `hold_monitor.py::_score_stop_distance` — `stop_dist_atr == 0` (price at/through stop) now scores -1.0 (was neutral 0.0, same code path as missing data). Matches P2-9 in known issues. |
| S9-2 | `signals.py` — two bare `except: continue` / `except: ... = None` blocks (factor computation loop, Ridge regression fit) replaced with `except Exception as e: logger.warning(...)`. Failures were previously invisible — a growing fraction of the universe could silently drop out of scoring with no log trace. |

**Corrections to stale doc claims found during audit:**
- **P2-7 (OBV magic constant 1000)** was already fixed in code (see comment in `hold_monitor.py::_score_volume`: normalizes by rolling std of OBV slopes instead of a hardcoded 1000 floor) but was never marked done in this plan or removed from the ontology's open-gaps list. Removed below.
- **P2-8 (ATR expansion binary)** — doc previously said the 0.80–1.20 range scores exactly 0.0. Actual code (`hold_monitor.py::_score_volatility`) scores 0.0 only for `atr_exp < 0.80` (contraction) and a flat 0.2 for the 0.80–1.20 normal band. Still not continuous (still loses information, still worth fixing) but the documented value was wrong. Corrected in ontology §14.3.

**Repo cleanup — done this session:**
- Deleted 9 zip/patch files in repo root with no remaining purpose: `files.zip` (duplicate of 9 files already present individually in root), `raptor_s4b_slippage.zip`, `raptor_s4c_neutralization.zip`, `raptor_s4d_dsr.zip`, `raptor_s5_fixes.zip`, `raptor_s5b_positions.zip`, `raptor_s5c_markdowns.zip`, `morning_email.patch`, `raptor_fixes_20260524.patch`.
- Deleted `archive/backfill_ledger.py` — was byte-identical to root `backfill_ledger.py`, true duplicate, served no archival purpose.
- Deleted 4 stray "Copy" files in `logs/`: `github_push - Copy.log`, `raptor_20260331 - Copy.log`, `raptor_auto_start - Copy.log`, `trades - Copy.csv`.
- `outcome_tracker_v2.py` left in `archive/` — unreferenced anywhere but kept for archival history (not a true duplicate, just dead code).

## Session 8 Fixes (2026-06-19)

| ID | Fix |
|----|-----|
| S8-1 | **OneDrive migration:** Raptor moved to `C:\Raptor`. 22 files patched (all bat/ps1/py/md). 19 Task Scheduler tasks re-registered and verified. OneDrive no longer watches Raptor. Git is sole sync mechanism. Root cause of 3 ledger corruptions eliminated. |
| S8-2 | **Ledger repair:** KDP/PFE/SQQQ ghost positions closed (exits confirmed in exits_20260618.log and outcome_tracker.log). AAL trim backfilled (117sh @ $15.835, 805→688). Alpaca/ledger sync confirmed 7/7. |
| S8-3 | **outcome_tracker UnicodeEncodeError fixed:** `→` (U+2192) replaced with `->` in 4 print statements. Was crashing after successful write — data was safe but log was always showing traceback. |
| S8-4 | **Root cause documented:** OneDrive file-system watcher conflicts with `os.replace()` atomic writes when multiple files are written in rapid succession. Silent revert to cloud version — no exception thrown. Pattern: `position_ledger.json` written 5x in one exit_monitor cycle. Fix: run outside OneDrive scope. |

| ID | Fix |
|----|-----|
| S6-1 | OU hold target rework: θ fit on market-residual log-price (not raw), not raw I(1)-contaminated series (see RAPTOR_ONTOLOGY.md §16) |
| S6-2 | ADF-style unit-root pre-test gates hold_target — falls back to time-stop branch with `reliable=False` instead of fabricating a number on trending/random-walk names |
| S6-3 | Marriott-Pope (1941) bias correction on φ̂ before θ conversion — corrects early-exit bias from finite-sample OLS bias |
| S6-4 | Parametric bootstrap CI (`hold_target_low`/`hold_target_high`) replaces unreliable delta-method interval; new `Signal` fields are backward-compatible (defaults + existing `getattr` call sites unaffected) |
| S6-5 | Citation correction: ln(2)/θ documented as half-life heuristic, not Leung & Zhang (2019)'s actual optimal-stopping result |
| S6-6 | Documentation/code drift fix: ontology §9 wrongly described an "OU-theta derived" trailing stop that was never implemented; corrected to match live time/profit-tiered `_trail_mult()` in exit_monitor.py |

## Session 7 Fixes (2026-06-17)

| ID | Fix |
|----|-----|
| S7-1 | `kelly_engine.py::_dd_constrained_f` rewritten: replaces ad hoc `dd_tolerance/(σ√252)` heuristic with derived drawdown-excursion-probability formula `P(breach β) = β^((2−λ)/λ)`, inverted for λ (fraction of full Kelly) given a target tolerance and breach probability. See RAPTOR_ONTOLOGY.md §17. |
| S7-2 | `dd_budget_lambda` field added to `kelly_estimates.json` per book — exposes the implied fraction-of-full-Kelly the drawdown budget allows (currently ~0.10 for MOMENTUM book, vs the 0.50 half-Kelly haircut actually applied upstream of it in the pipeline) |
| S7-3 | `P_TOL` (target probability of ever breaching `MAX_DD`) added as an explicit, flagged `TODO:DERIVE` constant — was previously absent; the old heuristic had no probabilistic interpretation at all, so there was nothing to flag. Placeholder = 0.05 (conventional tail, not yet fit to Raptor's own equity curve). Gated at DATA-60. |
| S7-4 | Diagnostic-only fat-tail correction factor (`f_star_correction_factor_DIAGNOSTIC_ONLY`) added to `return_diagnostics()` — surfaces the skew-vs-kurtosis directional correction to naive Kelly (η* = s/κ crossover) without feeding it into production sizing, per the 4th-order Taylor expansion's unreliability at κ≈8-10 |
| S7-5 | Verified via unit tests (lambda formula matches hand-derived session value 0.0819 for 12%/5% inputs; boundary/degenerate guard rejects p_tol ≥ β; fail-open behavior confirmed) and a full run against live `outcome_log.json` (53 trades) — `f_dd_constrained` moved from 3.83% (old heuristic) to 5.07% (new formula), still the binding constraint ahead of half-Kelly's 13.17% |
| S7-6 | Zero breaking changes confirmed: diffed `kelly_estimates.json` output keys old vs new — all prior keys retained, only additive fields. `get_recommended_kelly()` (sole downstream consumer) reads only `f_recommended`/`mode`, both unchanged in shape. Kelly remains SHADOW mode — no live sizing affected by this change (Rule 5). |



## Open Priority Queue

### No data gate — can build any session

| # | Item | File | Notes |
|---|------|------|-------|
| 1 | Agent-vs-math disagreement rate in daily_recap | daily_recap.py | Data flowing from S4-2 (2026-06-10); needs 1 week of records |
| 2 | ARCH-2: HMM macro regime for Raptor | macro_context.py | Hamilton (1989) via hmmlearn; probability vector output, no discrete labels. Ares already has it. |
| 3 | ARCH-5: Point-in-time universe | universe_builder.py | Requires external data source (Quandl/Sharadar). Survivorship warning already in backtest. |
| 4 | margin_guard.py WARN_THRESHOLD derivation | margin_guard.py | TODO:DERIVE — needs equity curve data. Guard is fully wired and correct; threshold needs calibration. |
| 5 | P2-8: ATR expansion binary → continuous | hold_monitor.py | Flat 0.2 for 0.80–1.20 range (corrected description 2026-06-28, was documented as 0.0) — still not continuous, still loses information |
| 6 | ~~P2-9: Stop distance layer zero signal~~ | hold_monitor.py | **FIXED 2026-06-28 (S9-1)** — dist==0 now scores -1.0 |
| 7 | P1-15: Sentiment dead path | signals.py | sentiment_score always 0.0 — remove or fix the pipeline |
| 8 | ~~7 zip/patch files + duplicate + Copy files in repo root~~ | repo root | **DELETED 2026-06-28 (session 9)** — see cleanup list above |
| 9 | Consume hold_target_low/high/reliable downstream | hold_monitor.py, daily_recap.py | New fields exist on Signal (S6-4) but time-exit logic and recap email don't read them yet — a `reliable=False` position is currently treated identically to a high-confidence one |

### DATA-40 gate (≥40 independent positions in position_outcomes.json)

| # | Item | Reference |
|---|------|-----------|
| 1 | GAP-B: Trail tier calibration first pass | Thorp 2006; backtest drawdown analysis |
| 2 | MATH-1: Regime-conditional IC buckets | ic_by_regime split in signals.py AdaptiveWeights._fit() |

### DATA-60 gate (≥60 independent positions)

| # | Item | Reference |
|---|------|-----------|
| 1 | MATH-5: Reduce n_prior 50→20 in kelly_engine.py | Bayesian Kelly prior reduction |
| 2 | ARCH-1: IC layer weights in hold_monitor | Spearman IC per layer vs realized PnL |
| 3 | Kelly shadow → active mode | Config flag flip |
| 4 | Noise-band floor derivation from gap_atr log data | EVT/GPD via scipy.stats.genpareto |
| 5 | Purged walk-forward IC validation | López de Prado 2018 ch.7 — purge + embargo |

### DATA-200 gate

| # | Item |
|---|------|
| 1 | ARCH-3: Full covariance Kelly |
| 2 | ARCH-4: LightGBM non-linear factor model (500+ clean trades) |

---

## Arbitrary Constants — Must Derive (updated)

| Location | Constant | How to derive | Gate |
|----------|---------|---------------|------|
| signals.py | Kelly SNR normalizer /3.0 | Bootstrap Kelly percentile distribution | DATA-40 |
| signals.py | Kelly clip 0.02/0.12 | EVT tail on closed position returns | DATA-40 |
| signals.py | Regime blend sigma 0.25 | Historical regime transition frequency | None — derive from macro_context history |
| signals.py | OU hold_target min=3, max=30 | Regress realized hold_days vs theta estimate (estimator reworked 2026-06-17, see ontology §16 — bounds calibration still pending data) | DATA-40 |
| hold_monitor.py | LAYER_WEIGHTS (hand-picked) | Spearman IC per layer vs PnL | DATA-60 (ARCH-1) |
| hold_monitor.py | TIER_STRONG=0.20, TIER_STABLE=-0.15 | Health score vs forward return distribution | DATA-40 |
| exit_monitor.py | Trail modifier 0.3/1.3/0.75 | Backtest trail width sensitivity | DATA-40 (GAP-B) |
| config.py | initial_stop_atr_mult 3.0 | EVT-derived — gate: DATA-60 | DATA-60 |
| config.py | max_portfolio_drawdown 0.12 | EVT tail on portfolio return distribution | DATA-60 |
| kelly_engine.py | P_TOL = 0.05 (target probability of ever breaching MAX_DD) | Calibrate against margin_guard.py trigger cost / Steve's explicit ruin-cost function once enough live drawdown episodes exist (session 7, 2026-06-17 — see ontology §17) | DATA-60 |
| macro_context.py | Vote thresholds 3/0/-2 | Replace with HMM probability vector (ARCH-2) | None |
| dsr.py | N_TRIALS_DEFAULT = 10 | Count of strategy-altering commits — update each session | Rolling |

---

## P1 Status Table (updated 2026-06-12)

| ID | Description | Status |
|----|-------------|--------|
| P1-1 | Kalman macro classifier | NOT BUILT — replaced by Gaussian regime blend in signals.py |
| P1-2 | Vol-regime hard stop | ✅ CONFIRMED |
| P1-3 | OU trailing stop | ❌ NEVER BUILT — was documentation-only; live trail is time-tier + profit-ATR (`exit_monitor._trail_mult`). Corrected 2026-06-17. |
| P1-4 | Bayesian Kelly | ✅ CONFIRMED — bootstrap live, SHADOW mode |
| P1-5 | OU hold target | ⚠️ REWORKED 2026-06-17 — market-residual series, unit-root gate, bias correction, bootstrap CI added (S6-1..S6-4). CI/reliability flag not yet consumed downstream (see queue item below). |
| P1-6 | IC layer weights hold monitor | GATED — DATA-60 (currently 27 positions) |
| P1-7 | Continuous trim | ✅ CONFIRMED |
| P1-8 | Regime-relative thesis threshold | ✅ CONFIRMED |
| P1-9 | Watchdog intraday | NOT BUILT — fetches 5 daily bars, not intraday. Rebuild or remove bat. |
| P1-10 | Composite velocity gate | ✅ CONFIRMED |
| P1-11 | Re-entry cooldown | ✅ CONFIRMED |
| P1-12 | Portfolio heat partial trim | ✅ CONFIRMED (25% proportional) |
| P1-13 | Multi-MA breadth (50/150/200) | ✅ CONFIRMED |
| P1-14 | Universe sensitivity sweep | FUTURE (ARCH-5) |
| P1-15 | Sentiment dead path | OPEN — sentiment_score always 0.0 |
| P1-16 | Afternoon rescore | PARTIAL — exit_monitor GAP9 rescore live |
| P1-17 | Conviction gradient sizing | ✅ CONFIRMED via book_conviction percentile |

---

## Known Issues — Open

| Issue | Impact | Priority |
|-------|--------|----------|
| Trail multiplier tiers (2.5/2.0/2.5×) — TODO:DERIVE | Stops may be too tight/loose on winners | GAP-B at DATA-40 |
| macro_context.py vote-count thresholds — no statistical basis | Regime misclassification risk | ARCH-2: replace with HMM |
| position_outcomes.json built manually — not auto-updated after close | New positions won't appear until Claude rebuilds it | No rebuild script exists yet (rebuild_positions.py referenced in RAPTOR_STARTUP.md was never built) — needs a dedicated session, see Session 11 |
| ~~Alpaca/ledger reconciliation only ever manual~~ | ~~MISSING_FROM_LEDGER/GHOST_IN_LEDGER alerts recurred with no automated fix~~ | **PARTIALLY FIXED 2026-07-01 (S11-4)** — `reconcile_positions.py --fix` now implemented and runs nightly via Start_AfterClose.bat for MISSING_FROM_LEDGER. GHOST_IN_LEDGER still requires manual confirmation by design (auto-closing a position is destructive). |
| WFC/KRE stops above price (as of Jun 18) | EXIT 1 will fire on next exit_monitor run — expected, not a bug | Self-resolving Mon 9:52 AM |
| `outcome_tracker_v2.py` unreferenced in `archive/` | Dead code, harmless | Low — delete whenever convenient |
| **CRLF line-ending mismatch** | **RESOLVED 2026-06-28** — `.gitattributes` (`* text=auto eol=lf`) added, `git add --renormalize .` run and committed, `core.autocrlf=false` set | Done |

---

## Completed (all sessions, chronological)

All pre-session-4 completions archived. For full history see git log.
Key pre-S4 completions: CRIT-0 through CRIT-9, MATH-2/3/4, H-1 through H-8,
P0-1, P0-8, submit_order fix, trail tier interim fix, double-trim guard,
log tracking enabled, DFA-1 Hurst, soft z-score shrinkage, Ledoit-Wolf SNR.
