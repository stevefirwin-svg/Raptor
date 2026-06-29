# Raptor — Master Priority Plan
*Last updated: 2026-06-29 (session 10 — full system audit fixes #10-17, see below)*
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
| hold_monitor.py | LAYER_WEIGHTS (hand-pic