# Raptor — Master Priority Plan
*Last updated: 2026-05-27. Source of truth: GitHub + live code audit.*
*Supersedes all prior versions including RAPTOR_AUDIT_AND_PLAN.md (now deleted).*

---

## The Standard

Every number must be derivable from a formula, empirical data, or an optimization.  
If "why that number?" cannot be answered, the number is wrong.

**Data integrity corollary:** Real data or skip. Never fabricate a fallback value that looks real.

---

## Current System State (verified 2026-05-27)

| Component | Status |
|-----------|--------|
| All P0 blockers | ✅ FIXED |
| All CRIT items (0–8) | ✅ RESOLVED |
| Fabricated defaults | ✅ ELIMINATED |
| Learning layer | ✅ LIVE — 42 IC-valid trades |
| Kelly engine | ✅ SHADOW — 42/100 trades |
| MATH items | ❌ 3 open (MATH-1, MATH-3, MATH-5) |
| ARCH items | ⏳ Gated on data |
| HYGIENE items | ❌ ~7 open |

---

## CATEGORY DEFINITIONS

| Symbol | Meaning | When to act |
|--------|---------|-------------|
| 🔴 MATH | Static where dynamic required, or misspecified formula | Next session |
| 🟡 ARCH | Correct direction, premature at current data scale | When data gate met |
| 🟢 HYGIENE | Dead code, fragile I/O, missing instrumentation | Rolling |

---

## ✅ COMPLETED — All resolved, no further action needed

| ID | What was fixed |
|----|---------------|
| CRIT-0 | outcome_tracker: 98 trades backfilled, parse_ts UTC-aware, atomic writes, .env key aliases |
| CRIT-1 | Velocity gate wired into main.py (_velocity_filter) |
| CRIT-2 | Cooldown gate wired into main.py (_cooldown_filter) |
| CRIT-3 | Spearman rank IC in AdaptiveWeights (replaced binary sign-match) |
| CRIT-4 | Per-book AdaptiveWeights (MOMENTUM + MR files, no cross-contamination) |
| CRIT-5 | Atomic JSON writes (os.replace) in all critical files |
| CRIT-6 | Composite scoring for held positions (0.0 neutral, not -1.0 fake-weak) |
| CRIT-7 | Bootstrap Kelly (10k resamples, P25, decay-weighted λ=0.005) |
| CRIT-8 | Exponential decay on all learning (λ=0.005, ~139-day half-life) |
| MATH-2 | Ledoit-Wolf SNR entry ranking (live 2026-05-25) |
| MATH-4 | Portfolio heat proportional 25% trim (live 2026-05-25) |
| MATH-3a | ADX threshold raised 22→25 (interim Hurst proxy) |
| 2026-05-27 | exit_monitor: ATR fallback `price*0.02` → read from hold_health.json stop_dist_atr, skip with warning if missing |
| 2026-05-27 | exit_monitor: days_held fallback 7 → conservative 1 + warning |
| 2026-05-27 | accum_dist: abs(r) → r² (consistent with obv_r2, correct quality weight) |
| 2026-05-27 | Soft z-score shrinkage replaces hard \|z\|>0.10 threshold (eliminates score cliffs) |
| 2026-05-27 | ONTOLOGY synced: accum_dist formula, composite soft shrinkage, updated date |
| 2026-05-27 | Soft z-score shrinkage replaces hard \|z\|>0.10 threshold (eliminates score cliffs) |
| 2026-05-27 | Spearman IC comments + derivation inline in _get_ic_boost |

---

## 🔴 MATH — Open Items

### MATH-1 — Regime-Conditional IC Buckets
**Gate:** 10+ trades per regime bucket (not yet met)  
Pooled IC across regimes gives IC≈0 for factors that work in one regime but not another.  
**Fix:** `ic_by_regime = {"BULLISH": {...}, "NEUTRAL": {...}, "RISK_OFF": {...}}`  
**Location:** signals.py AdaptiveWeights._fit()

### MATH-3 — Hurst DFA / ADX Full Fix
**Status:** Partial — ADX threshold raised 22→25 as interim proxy  
**Full fix:** Detrended Fluctuation Analysis (Kantelhardt et al. 2002)  
**Location:** signals.py Factors.hurst() — replace R/S with DFA exponent

### MATH-5 — n_prior Reduction
**Gate:** IC-valid trades >= 60 (have 42)  
**Fix:** Reduce n_prior 50→20 in kelly_engine.py  
Bayesian prior dominates at low n; with sufficient data it should recede.

---

## 🟡 ARCHITECTURE — Gated on Data

| ID | Description | Gate |
|----|-------------|------|
| ARCH-1 | IC layer weights in hold_monitor (Spearman IC per layer) | 60+ IC-valid trades |
| ARCH-2 | Kalman macro classifier (replaces vote-count in macro_context.py) | Walk-forward infra |
| ARCH-3 | Full covariance Kelly | 200+ position-days |
| ARCH-4 | LightGBM non-linear factor model | 500+ clean trades |
| ARCH-5 | Walk-forward backtest infrastructure | Data overhaul |
| ARCH-6 | Database (PostgreSQL/DuckDB) | When JSON causes incident |

---

## 🟢 HYGIENE — Open Items

| ID | Issue | File |
|----|-------|------|
| H-1 | Dead files: raptor_state.json, diagnose.py, diagnose_regime.py | Various |
| H-2 | prompt_calibrator.py referenced but does not exist | agent_layer.py |
| H-3 | Universe size hardcoded ~120 in daily_recap | daily_recap.py |
| H-4 | Missing recap metrics (exit breakdown, rolling win rate, trim efficiency) | daily_recap.py |
| H-5 | compute_trim fallback parses stop_dist from string | hold_monitor.py |
| H-6 | OBV magic constant 1000 — should be symbol's own OBV std | hold_monitor.py |
| H-7 | Volatility layer dead zone ATR 0.80–1.20 | hold_monitor.py |
| H-8 | Prompt versioning runs on every import | agent_layer.py |

---

## ARBITRARY CONSTANTS — Must Derive (flagged, not yet replaced)

These exist in live code. Each needs empirical derivation when data permits.

| Location | Constant | Target derivation |
|----------|---------|------------------|
| signals.py:507 | Kelly SNR normalizer `/ 3.0` | Bootstrap Kelly percentile distribution |
| signals.py:508 | Kelly clip `0.02 / 0.12` | EVT tail analysis on closed trade returns |
| signals.py:288 | Regime blend sigma `0.25` | Historical regime transition frequency |
| hold_monitor.py:46–54 | LAYER_WEIGHTS (hand-picked) | IC-weighted — ARCH-1 gate: 60 IC-valid |
| hold_monitor.py:353 | Score `0.5 / -0.8` for stop distance | stop_dist_atr distribution in hold_history |
| hold_monitor.py:62–63 | TIER_STRONG=0.20, TIER_STABLE=-0.15 | Health score distribution vs forward returns |
| config.py:59 | initial_stop_atr_mult `3.0` | EVT-derived (gate: 50+ clean trades) |
| config.py:80 | max_portfolio_drawdown `0.12` | EVT tail on portfolio return distribution |
| main.py:478 | velocity min_velocity `-0.15` | IC of velocity vs forward return (need 60+) |
| main.py:478 | cooldown SNR floor `0.8` | SNR distribution of re-entry success vs failure |
| exit_monitor.py:246 | Flat threshold `< 0.02` | Cross-sectional return distribution percentile |

---

## BUILD ORDER — NEXT SESSIONS

```
IMMEDIATE:
  python outcome_tracker.py          # run daily to tag new closed trades

NEXT SESSION (no gate):
  MATH-3  Full Hurst DFA — replace R/S exponent with DFA

WHEN IC-valid >= 60 (have 42):
  MATH-5  Reduce n_prior 50→20
  ARCH-1  IC layer weights in hold_monitor
  MATH-1  Regime-conditional IC buckets

WHEN 100 equity trades (SHADOW → ACTIVE eligible):
  Kelly ACTIVE mode — already coded, needs config flag

WHEN 200+ position-days:
  ARCH-3  Full covariance Kelly

ROLLING:
  H-1  Delete dead files
  H-4  Add missing daily_recap metrics
```

---

## LIVE DATA SNAPSHOT (2026-05-27)

```
outcome_log.json:     42 IC-valid trades | all MOMENTUM
kelly_estimates.json: SHADOW (42/100) | win_rate=38% | mu=+4.3% | sigma=20%
factor_ic_report.json: ma_stack IC=+0.48 | adx_dir IC=+0.44 | vol_ratio IC=-0.11 ⚠️
Exit quality:          math_trim 70% win | trailing_stop 20% win (fires too late)
```

---

## P1 STATUS TABLE — VERIFIED FROM CODE 2026-05-27

| ID | Description | Status |
|----|-------------|--------|
| P1-1 | Kalman macro classifier | NOT IN CODE — replaced by regime probability blend (Gaussian) |
| P1-2 | Vol-regime hard stop | ✅ CONFIRMED |
| P1-3 | OU trailing stop | ✅ CONFIRMED |
| P1-4 | Bayesian Kelly | ✅ CONFIRMED — bootstrap upgrade live |
| P1-5 | OU hold target | NOT IN CODE — hardcoded 15 for MOM, dist_to_mean for MR |
| P1-6 | IC layer weights hold monitor | ⏳ GATED 60+ trades |
| P1-7 | Continuous trim | ✅ CONFIRMED |
| P1-8 | Regime-relative thesis threshold | ✅ CONFIRMED |
| P1-9 | Watchdog intraday | DELETE or build properly |
| P1-10 | Composite velocity gate | ✅ CONFIRMED |
| P1-11 | Re-entry cooldown | ✅ CONFIRMED |
| P1-12 | Portfolio heat partial trim | ✅ CONFIRMED (25% proportional, live 2026-05-25) |
| P1-13 | Multi-MA breadth (50/150/200) | ✅ CONFIRMED |
| P1-14 | Universe sensitivity sweep | FUTURE |
| P1-15 | Sentiment dead path | HYGIENE |
| P1-16 | Afternoon rescore | Partial — exit_monitor GAP9 rescore live |
| P1-17 | Conviction gradient sizing | ✅ CONFIRMED via book_conviction percentile |
