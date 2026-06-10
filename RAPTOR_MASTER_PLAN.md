# Raptor — Master Priority Plan
*Last updated: 2026-06-10 (session 4 — full-stack audit). Source of truth: GitHub + live code audit.*
*Supersedes all prior versions.*

---

## The Standard

Every number must be derivable from a formula, empirical data, or an optimization.
If "why that number?" cannot be answered, the number is wrong.

**Data integrity corollary:** Real data or skip. Never fabricate a fallback value that looks real.

**Audit integrity corollary:** A fix is only DONE when grep/test output is pasted in the same
session confirming it. Documented-but-unverified fixes are NOT done.

---

## Current System State (verified 2026-05-29)

| Component | Status |
|-----------|--------|
| All P0 blockers | FIXED |
| submit_order execution | FIXED 2026-06-05 |
| P0-1 outcome sidecar | FIXED 2026-05-29 |
| P0-8 regime unification | FIXED 2026-05-29 |
| Fabricated defaults | ELIMINATED |
| Learning layer | LIVE - 8 IC-valid terminal exits (see data note) |
| Kelly engine | SHADOW - 43/100 trades |
| MATH items | 2 open (MATH-1, MATH-5) |
| ARCH items | Gated on data |
| HYGIENE items | 0 open (all closed 2026-05-29) |

---

## SESSION 4 AUDIT — 2026-06-10 (full-stack, log-driven)

### Found in logs (last 2 weeks)

| Date(s) | Finding | Root cause | Status |
|---------|---------|-----------|--------|
| 06-01 to 06-04 | Hard stops on AMD/INTC/HPQ/TSLA logged SELL then **zero execution, zero error** — exits log truncated mid-run all 4 days. Cash frozen at $76,566.30. | Tail of the submit_order missing-`def` bug. Pre-06-05 EXECUTE block had **no try/except** around submit_order; AttributeError killed the process between the SELL log line and any error output. | Explained — fixed by 06-05 commits. Crash-visibility handlers added 06-10 so this class can never be invisible again. |
| 06-01 to 06-03 | EntryAgent emitted **binding** vetoes citing `macro_regime="RISK_OFF"` while logged regime was RISK_ON. Valid candidates blocked 3 days despite a second prompt-tightening pass. | Stochastic LLM evaluating deterministic boolean rules — hallucination cannot be prompted away. | **FIXED 2026-06-10**: six entry rules now evaluated exactly in Python (`_eval_entry_rules`); LLM demoted to advisory; disagreements logged as `AGENT_OVERRIDE` and persisted (`agent_math_disagree` field) for calibration. 8/8 unit tests pass incl. the 06-01 scenario. |
| 06-08 | Entry scan died silently mid-universe-rebuild ("Fetching bars for 7171 symbols" then nothing). No entry scan ran 06-08. | Unknown — no traceback was capturable (see crash visibility fix). Candidate causes: Python exception in fetch loop outside per-batch handler, OR Task Scheduler "stop task if it runs longer than" killing the process. | Crash handler now logs traceback; **STEVE ACTION**: check Task Scheduler task settings → Settings tab → "Stop the task if it runs longer than" for the entry task. |
| 06-05 | All 6 positions exited simultaneously via trailing_stop at 09:52 — the collapsed-trail bug fired the moment execution came back online, before the tier fix landed later that day. | Known (trail tiers). | Already fixed 06-05; noting sequence for the record. |
| 06-09 | System bought **SQQQ (-3x QQQ)** 258 shares as a momentum position; TSLL / PLTD held previously. | Universe had no leveraged/inverse ETP filter. Variance drain ≈ (k²−k)/2·σ² per day (Cheng & Madhavan 2009) breaks every multi-day hold assumption in the stack (ATR stops, hold targets, momentum persistence). | **FIXED 2026-06-10**: name-pattern exclusion in universe_builder, 10/10 pattern tests pass. **SQQQ position currently open — Steve decides whether to exit manually or let exit math close it.** |
| ongoing | `outcome_log.json`: post-05-29 records mostly `entry_decision: None` (3 of 11 populated). | NOT a sidecar bug — sidecar works (MRVL 06-09 = PASS). Entries made during the broken-JSON-parse era (pre-06-03 sanitize fix) never had PASS records written to entry_vetoes.json; nothing exists to join. | Unrecoverable data loss for those positions; labels self-heal as the book turns over. Documented, no action possible. |
| ongoing | Bat files still wrote to `logs\auto_start.log` — the raptor_auto_start.log rename agreed in a prior session **never landed in the repo** (fix-not-in-live-code failure class). | Session output not committed. | **FIXED 2026-06-10**: all 9 bat files now write `logs\raptor_auto_start.log`. |
| security | Gmail app password was **still hardcoded** in daily_recap.py + morning_scanner_email.py in the public repo (send_ontology_email.py was fixed; other two were not). | Partial application of prior fix. | Code **FIXED 2026-06-10** (env var). **STEVE ACTION — URGENT, cannot be done by Claude:** the old app password is burned (public git history). Revoke it at myaccount.google.com → Security → App passwords, generate a new one, put it in `.env` as `EMAIL_APP_PASSWORD`. Until then recap/scanner emails will not send. |

### Fixed this session (all syntax-verified + unit-tested in container; Rule 11 requires paste-confirmed verification on Steve's machine)

| ID | Fix |
|----|-----|
| S4-1 | Crash-visibility handlers: main.py, exit_monitor.py, hold_monitor.py entry points now `logger.exception` full traceback to the log file and exit 1 on any uncaught error. The silent-death class (06-01..04, 06-08) is now structurally impossible to miss. |
| S4-2 | Deterministic entry gate: `_eval_entry_rules()` in agent_layer.py — math governs, agent advises. Reconciled decisions written to entry_vetoes.json with `decision_source="deterministic"`, raw agent view preserved (`agent_decision`, `agent_math_disagree`) → enables the agent-vs-math disagreement metric for daily_recap. |
| S4-3 | Hardcoded Gmail app password removed from daily_recap.py + morning_scanner_email.py → `os.getenv("EMAIL_APP_PASSWORD")` with dotenv load. |
| S4-4 | 9 bat files: `auto_start.log` → `raptor_auto_start.log` (Ares isolation, finally in-repo). |
| S4-5 | universe_builder: leveraged/inverse ETP exclusion, math-derived (variance drain), 1x sector ETFs unaffected. |

### Cannot be done by Claude — Steve's queue

1. **Revoke + rotate Gmail app password** (above). The committed password must be treated as public.
2. **Task Scheduler check** for the 06-08 kill: every Raptor task → Settings → confirm "Stop the task if it runs longer than" is OFF or ≥ 2 hours for the entry task (universe rebuild day takes minutes, not seconds).
3. **Decide on the open SQQQ position** (entered 06-09, pre-filter). Exit math will manage it, but it now sits outside the instrument class the math was built for.
4. **Verify S4 fixes live** per Rule 11: run the Step 4 checks + `python -c "from agent_layer import _eval_entry_rules; print(_eval_entry_rules({'regime':'TRENDING','composite_score':1.5,'kelly_fraction':0.05,'atr_pct':2,'days_since_earnings':30,'vix_regime':'NORMAL','market_momentum_scalar':1.0,'macro_regime':'RISK_ON'}))"` → expect `(False, None, None)`. Paste output next session.

### Metrics gaps confirmed against the required daily_recap list

Present: exit-reason breakdown, rolling win rate, trim efficiency, capital efficiency, consecutive-loss streak, hold-days.
Missing: **agent-vs-math disagreement rate** (data now flows via S4-2 — implement once a week of reconciled records exists) and **macro regime at entry vs exit per position** (requires regime stamped into ledger metadata at entry; stamp exists for new entries via `metadata.regime`, but exit-side regime is not recorded in outcome records — add `exit_regime` field to build_outcome_record, ~5 lines, queued for next session to keep this session's diff reviewable).

---

## CATEGORY DEFINITIONS

| Symbol | Meaning | When to act |
|--------|---------|-------------|
| MATH | Static where dynamic required, or misspecified formula | Next session |
| ARCH | Correct direction, premature at current data scale | When data gate met |
| HYGIENE | Dead code, fragile I/O, missing instrumentation | Rolling |

---

## COMPLETED

| ID | What was fixed |
|----|---------------|
| 2026-06-05 | submit_order missing `def` line in data_feeds.py — method existed as floating dead code inside get_portfolio_history, never registered on AlpacaDataFeed. Every order submission since ~2026-05-25 raised AttributeError, crashed execute loop silently. 11 days of exits and trims never reached Alpaca. Fixed: def line restored, execute loop wrapped in try/except per-order so one failure doesn't abort remaining positions. |
| 2026-06-05 | logs/ removed from .gitignore — runtime logs now tracked in git for diagnostic analysis. |
| 2026-06-05 | _trail_mult() profit tiers corrected: profit_atr>=4.0 was 1.0x (too tight, fires on any daily move), now 2.5x. All tiers raised to be consistent with 3.0x ATR entry stop logic. TODO:DERIVE remains for final calibration at 60+ exits (GAP-B). |
| 2026-06-05 | position_ledger.json stops reset: all 7 positions had stops ratcheted above or within 1 ATR of current price. Reset to max(hw-2.5*ATR, price-2.5*ATR). Logged with stop_reset_reason field. |
| 2026-06-05 | Double-trim guard fixed: was reading stale _ledger_map snapshot (built before execute loop), so last_trim_ts written during loop was invisible to the guard. 36 Alpaca insufficient_qty rejections across 9 dates back to 2026-04-10. Fixed: guard now reads from live _ledger.data object updated in-loop. |
| 2026-06-05 | Log analysis added as Step 3 in RAPTOR_STARTUP.md — logs are ground truth, read before code every session. |
| CRIT-0 | outcome_tracker: trades backfilled, parse_ts UTC-aware, atomic writes |
| CRIT-1 | Velocity gate wired into main.py |
| CRIT-2 | Cooldown gate wired into main.py |
| CRIT-3 | Spearman rank IC in AdaptiveWeights (replaced binary sign-match) |
| CRIT-4 | Per-book AdaptiveWeights (MOMENTUM + MR files) |
| CRIT-5 | Atomic JSON writes (os.replace) in all critical files including ledger |
| CRIT-6 | Composite scoring for held positions (0.0 neutral, not -1.0) |
| CRIT-7 | Bootstrap Kelly (10k resamples, P25, decay-weighted) |
| CRIT-8 | Exponential decay on all learning (lambda=0.005, ~139-day half-life) |
| MATH-2 | Ledoit-Wolf SNR entry ranking |
| MATH-4 | Portfolio heat proportional 25% trim |
| MATH-3a | ADX threshold raised 22 to 25 (interim Hurst proxy) |
| 2026-05-27 | exit_monitor: fabricated ATR fallback eliminated |
| 2026-05-27 | exit_monitor: days_held fallback 7 to conservative 1 + warning |
| 2026-05-27 | accum_dist: abs(r) to r-squared |
| 2026-05-27 | Soft z-score shrinkage replaces hard threshold |
| 2026-05-27 | record_trim added to ledger.py - partial trims no longer close position |
| 2026-05-27 | Bat ordering fixed: hold_monitor before exit_monitor in all bat files |
| 2026-05-27 | Double-trim guard: last_trim_ts written, 30-min block per symbol |
| 2026-05-27 | portfolio_heat trims written to trim_log.json |
| 2026-05-29 | P0-1: outcome_pending sidecar - exit_monitor writes order-ID keyed record after every sell; outcome_tracker reads it for entry_decision. Replaces broken timestamp matching. Files: exit_monitor.py, outcome_tracker.py |
| 2026-05-29 | P0-8: regime override - main.py + exit_monitor load macro_context.json after get_full_dataset() and overwrite macro regime with canonical RISK_ON/NEUTRAL/RISK_OFF/CRISIS label. EntryAgent veto rules now live. Files: main.py, exit_monitor.py |
| 2026-05-29 | MATH-3: Hurst DFA full fix — replaced R/S estimator with DFA-1 (Kantelhardt et al. 2002). Log-spaced windows, linear detrending per window, same output sign convention. Min 60 bars. File: signals.py Factors.hurst() |
| 2026-05-29 | H-1: Deleted dead files: raptor_state.json, diagnose.py, diagnose_regime.py, Start_Raptor_Recap.bat. Removed reference from check_task_scheduler.py |
| 2026-05-29 | H-2: No code change needed — prompt_calibrator references are comments only, no broken import |
| 2026-05-29 | H-3: Universe size now dynamic in daily_recap.py — get_signals() returns len(universe), passed through build_html(), replaces hardcoded ~120 |
| 2026-05-29 | H-4: Added to daily_recap.py: exit reason breakdown (% + avg hold days per reason), rolling 10-trade win rate, consecutive loss streak, trim efficiency, capital efficiency. New table row + exit breakdown line in email |
| 2026-05-29 | H-5: compute_trim no longer parses stop_dist_atr from string detail field. compute_health_score now injects stop_dist_atr_raw (float) into return dict; compute_trim reads it directly. File: hold_monitor.py |
| 2026-05-29 | H-6: No code change needed — prompt versioning already lazy-loaded (P2-12 was fixed in prior session) |
| 2026-05-29 | H-7: Removed dead EQUITY_ALLOCATION=1.00 constant from main.py — was a no-op multiply |
| 2026-05-29 | H-8: config.py kelly_fraction corrected 0.15 → 0.12 to match actual clip ceiling in signals.py. Both carry TODO:DERIVE |

---

## LIVE DATA SNAPSHOT (2026-05-29)

outcome_log.json - 121 total records:
  IC-valid terminal exits:   8  (THE REAL GATE COUNT)
  math_trim:                54  (excluded from IC - partial exits)
  pre_label:                47  (historical, no factor scores - excluded)
  crypto:                   12  (separate system - excluded)
  entry_decision populated:  0  (P0-1 just fixed - will populate on next exit)

  WARNING: Prior docs claimed 42 IC-valid. That number incorrectly included
  math_trim and pre_label records. Real count is 8.
  All gates referencing "Have 42" in prior docs are wrong.

kelly_estimates.json:
  SHADOW mode (43/100 trades)
  win_rate = 27.9%  (was 38% before partial trims were correctly excluded)
  f_recommended = 1% (bootstrap P25)
  The trim-inflated win_rate was masking a weaker terminal book.

factor_ic_report.json:
  n_outcome = 0  (IC built on proxy history, not real closed-trade outcomes)
  n_history = 139
  WARNING: ALL IC VALUES ARE PROVISIONAL.
  Do not keep or drop any factor based on current report.
  IC becomes valid once n_outcome > 30.

macro_regime: RISK_ON (from macro_context.json - P0-8 now correctly propagated)

---

## MATH - Open Items

### MATH-1 - Regime-Conditional IC Buckets
Gate: 10+ trades per regime bucket (currently 0 - P0-1 just fixed the data pipe)
Fix: ic_by_regime split in signals.py AdaptiveWeights._fit()
### MATH-5 - n_prior Reduction
Gate: IC-valid trades >= 60 (currently 8)
Fix: Reduce n_prior 50 to 20 in kelly_engine.py

---

## ARCHITECTURE - Gated on Data

| ID | Description | Gate |
|----|-------------|------|
| ARCH-1 | IC layer weights in hold_monitor | 60+ IC-valid terminal exits |
| ARCH-2 | Kalman/continuous macro classifier | Walk-forward infra |
| ARCH-3 | Full covariance Kelly | 200+ position-days |
| ARCH-4 | LightGBM non-linear factor model | 500+ clean trades |
| ARCH-5 | Walk-forward backtest infrastructure | Data overhaul |
| ARCH-6 | Database | When JSON causes incident |

---

## KNOWN ISSUES — Open

| Issue | Impact | Priority |
|-------|--------|----------|
| Trail multiplier tiers (2.5/2.0/2.5×) — interim values, not yet derived | Improved from 1.0/1.5/2.0× but still TODO:DERIVE. Gate: GAP-B (60+ exits) | GAP-B: derive from backtest drawdown analysis (Thorp 2006) |
| INTC ledger stop=112.02 (stale backfill value) above current price | EXIT 1 fires every run, position can never trim | Fix ledger stop manually to ATR-based value |
| UnicodeEncodeError on → character in log output (cp1252 encoding) | Cosmetic logging error, execution continues | Low priority |

## HYGIENE - Open Items

All hygiene items closed 2026-05-29. See COMPLETED table for details.

| ID | Issue | Status |
|----|-------|--------|
| H-1 | Delete dead files | DONE 2026-05-29 |
| H-2 | prompt_calibrator.py reference | DONE — comments only, no broken import |
| H-3 | Universe size hardcoded ~120 | DONE 2026-05-29 |
| H-4 | Missing recap metrics | DONE 2026-05-29 |
| H-5 | compute_trim string parse | DONE 2026-05-29 |
| H-6 | Prompt versioning on import | DONE — already fixed in prior session |
| H-7 | EQUITY_ALLOCATION dead variable | DONE 2026-05-29 |
| H-8 | kelly_fraction=0.15 mismatch | DONE 2026-05-29 |

---

## ARBITRARY CONSTANTS - Must Derive

| Location | Constant | How to derive |
|----------|---------|---------------|
| signals.py | Kelly SNR normalizer /3.0 | Bootstrap Kelly percentile distribution |
| signals.py | Kelly clip 0.02/0.12 | EVT tail on closed trade returns |
| signals.py | Regime blend sigma 0.25 | Historical regime transition frequency |
| signals.py | hold_target 16+14*atr_pctile | ln(2)/theta per-stock OU speed (Leung & Zhang 2019) |
| hold_monitor.py | LAYER_WEIGHTS (hand-picked) | Spearman IC per layer vs PnL - gate: ARCH-1 |
| hold_monitor.py | TIER_STRONG=0.20, TIER_STABLE=-0.15 | Health score vs forward return distribution |
| exit_monitor.py | Trail modifier 0.3/1.3/0.75 | Backtest trail width sensitivity |
| config.py | initial_stop_atr_mult 3.0 | EVT-derived - gate: 50+ clean trades |
| config.py | max_portfolio_drawdown 0.12 | EVT tail on portfolio return distribution |
| macro_context.py | Vote thresholds 3/0/-2 | Regime IC vs forward return by bucket |

---

## BUILD ORDER

NOW (no gate):
  Run outcome_tracker.py daily after close - IC-valid count grows one trade at a time

WHEN IC-valid >= 60 (currently 8, need 52 more terminal exits):
  MATH-5: Reduce n_prior 50 to 20 in kelly_engine.py
  ARCH-1: IC layer weights in hold_monitor
  MATH-1: Regime-conditional IC buckets

WHEN 100 terminal exits (currently 8):
  Kelly ACTIVE mode - coded, needs config flag flip

WHEN 200+ position-days:
  ARCH-3: Full covariance Kelly

ROLLING HYGIENE:
  All H items closed. Next hygiene pass triggered by new incidents or new dead code.

---

## P1 STATUS TABLE - VERIFIED FROM CODE 2026-05-29

| ID | Description | Status |
|----|-------------|--------|
| P1-1 | Kalman macro classifier | NOT BUILT - replaced by Gaussian regime blend in signals.py |
| P1-2 | Vol-regime hard stop | CONFIRMED |
| P1-3 | OU trailing stop | CONFIRMED |
| P1-4 | Bayesian Kelly | CONFIRMED - bootstrap live |
| P1-5 | OU hold target | NOT BUILT - hardcoded 15 days MOM, dist_to_mean for MR |
| P1-6 | IC layer weights hold monitor | GATED - need 60 IC-valid trades (have 8) |
| P1-7 | Continuous trim | CONFIRMED |
| P1-8 | Regime-relative thesis threshold | CONFIRMED |
| P1-9 | Watchdog intraday | NOT IN FILES - delete bat or rebuild |
| P1-10 | Composite velocity gate | CONFIRMED |
| P1-11 | Re-entry cooldown | CONFIRMED |
| P1-12 | Portfolio heat partial trim | CONFIRMED (25% proportional) |
| P1-13 | Multi-MA breadth (50/150/200) | CONFIRMED |
| P1-14 | Universe sensitivity sweep | FUTURE |
| P1-15 | Sentiment dead path | HYGIENE - field always 0.0 |
| P1-16 | Afternoon rescore | PARTIAL - exit_monitor GAP9 rescore live |
| P1-17 | Conviction gradient sizing | CONFIRMED via book_conviction percentile |
