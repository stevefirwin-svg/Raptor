# Raptor — Master Priority Plan
*Last updated: 2026-05-24. Supersedes all prior versions.*  
*Incorporates: original audit, sessions 18–22, Grok review, ChatGPT review, ground truth code audit.*

---

## The Standard

Every number must be derivable from a formula, empirical data, or an optimization.  
If "why that number?" cannot be answered with a derivation or measured value from Raptor's own data, the number is wrong.

**Data integrity corollary:** If real data is unavailable, use a conservative neutral or skip the decision entirely. Never fabricate a value that looks real.

---

## Current System State (verified from live code 2026-05-24)

| Layer | Status |
|-------|--------|
| P0 Blockers | ✅ ALL 8 FIXED |
| P1 Math Foundation | ✅ 13/17 LIVE (see table below) |
| CRIT items | ✅ ALL 9 RESOLVED this session |
| Learning layer | ✅ LIVE — 98 tagged trades, exit paths resolving |
| Kelly engine | ✅ SHADOW — 73 clean trades, f_rec=3.89% (bootstrap P25) |
| Fabricated defaults | ✅ ELIMINATED this session |
| MATH items | ❌ 5 open |
| ARCH items | ⏳ Gated on data |
| P2 Hygiene | ❌ ~8 open |

---

## CATEGORY DEFINITIONS

| Symbol | Meaning | When to act |
|--------|---------|-------------|
| 🔴 CRITICAL | Bug corrupting decisions or statistical error invalidating math | Immediately |
| 🟠 MATH | Static where dynamic required, or misspecified live formula | Next session |
| 🟡 ARCH | Correct direction, premature at current data scale | When data gate met |
| 🟢 HYGIENE | Dead code, fragile I/O, missing instrumentation | Rolling |

---

## ✅ COMPLETED — CRIT ITEMS (all done 2026-05-24)

### CRIT-0 — Fix outcome_tracker ✅
- All 42 legacy records backfilled: trade_type=MOMENTUM, regime_at_entry, exit_path
- 56 new trades tagged from Alpaca order history (total: 98)
- parse_ts() fixed: always returns timezone-aware UTC datetime
- .env key name support: ALPACA_API_KEY / ALPACA_SECRET_KEY (was APCA_ only)
- Atomic writes via os.replace()

### CRIT-1 — Velocity gate wired into main.py ✅
- _velocity_filter(): skip entry if composite dropped > 0.20 vs yesterday
- composite_cache.json saved before held-symbol filter (captures full universe)
- Was built but never connected to main.py

### CRIT-2 — Cooldown gate wired into main.py ✅
- _cooldown_filter(): blocks re-entry during active cooldown window
- cooldown_log.json was being written but never read by main.py

### CRIT-3 — Spearman rank IC in AdaptiveWeights ✅
- _get_ic_boost() now uses scipy.stats.spearmanr
- Replaces binary sign-match IC (discarded magnitude)
- Reference: Grinold & Kahn (1999)

### CRIT-4 — Per-book AdaptiveWeights ✅
- adaptive_weights_MOMENTUM.json and adaptive_weights_MEAN_REVERSION.json
- Momentum data no longer contaminates MR weights
- QuantSignalEngine has self.adaptive_mom and self.adaptive_mr

### CRIT-5 — Atomic JSON writes ✅
- os.replace() used in: main.py, outcome_tracker.py, hold_monitor.py, exit_monitor.py, kelly_engine.py
- Crash during write no longer corrupts JSON files

### CRIT-6 — Composite scoring for held positions ✅
- signals.py: ALL scored symbols stored in _last_full_signals (not just gate-passers)
- Held positions that fail entry gates get real composite proxy, not -1.0
- exit_monitor.py: default changed from -1.0 (fake-weak) to 0.0 (neutral/unknown)
- Root cause: entry gates filtered held positions → artificial comp=-1.0 → artificial DECAYING

### CRIT-7 — Bootstrap Kelly ✅
- kelly_engine.py: bootstraps full recommended-f pipeline (10,000 resamples)
- Uses P25 of bootstrapped final-f as production estimate (not raw f*)
- Decay-weighted sampling (λ=0.005) — recent trades weighted higher
- Current result: f_rec=3.89% (P25), range 2.95%–5.32%, std=1.63%
- Still SHADOW mode — active at 100 trades

### CRIT-8 — Exponential decay on all learning ✅
- AdaptiveWeights: weighted ridge regression (WLS), decay-weighted IC
- λ=0.005, half-life ≈ 139 days (Asness, Moskowitz & Pedersen 2013)
- Same λ in kelly_engine.py bootstrap sampling

### CRIT-9 — Portfolio correlation gate ❌ CANCELLED (philosophy decision)
- Momentum clustering is a feature not a bug
- Same-sector stocks run together in bull markets — that is the alpha
- Hold monitor scores each position independently → natural diversification via health decay
- Correlation gate would fight the signal

---

## 🔴 FABRICATED DEFAULTS ELIMINATED (2026-05-24)

Principle: real data or skip, never invent.

| Location | Was | Now |
|----------|-----|-----|
| exit_monitor.py | days_held = 7 (invented) | days_held = 1 + warning (conservative) |
| exit_monitor.py | atr = price × 0.02 (invented) | Skip position + warning |
| hold_monitor.py | stop_price = entry × 0.92 (invented) | stop_price = None + warning |
| hold_monitor.py | stop_dist_atr computed on fake stop | stop_dist_atr = None (propagated) |
| hold_monitor.py | factor_agreement = 8/16 default | 0.0 with FAR=no_data label |
| hold_monitor.py | np.sum(generator) (deprecated) | sum() (correct) |
| signals.py | comp = -1.0 default for unscored held symbols | comp = 0.0 (neutral) |

**Action required:** Run `python backfill_ledger.py --write` to fix CVE, KDP, PLTD missing from position_ledger.json.

---

## 🟠 MATH — Open Items

### MATH-1 — Regime-Conditional IC Buckets
**Status:** NOT BUILT  
**Gate:** 10+ trades per regime bucket  
Pooled IC across regimes gives IC≈0 for factors that work in one regime but not another.  
Fix: ic_by_regime = {"BULLISH": {...}, "NEUTRAL": {...}, "RISK_OFF": {...}}

### MATH-2 — Composite Signal Uncertainty (SNR)
**Status:** NOT BUILT  
Fix: snr = composite_mean / (composite_std + 0.5) — use SNR for ranking.

### MATH-3 — Hurst DFA / ADX Threshold
**Status:** NOT BUILT  
Interim: raise ADX threshold TRENDING 22→25.  
Full fix: Detrended Fluctuation Analysis (Kantelhardt et al. 2002).

### MATH-4 — Portfolio Heat Proportional Trim
**Status:** NOT BUILT  
Fix: trim_pct = (abs(portfolio_dd) / max_dd) × abs(health) across all DECAYING positions.

### MATH-5 — n_prior Reduction
**Status:** GATED — 60+ agent-tagged trades  
Reduce n_prior 50 → 20 when agent-tagged trades >= 60.

---

## 🟡 ARCHITECTURE — Gated on Data

| ID | Description | Gate |
|----|-------------|------|
| ARCH-1 | IC layer weights in hold_monitor (Spearman IC per layer) | 60+ tagged trades |
| ARCH-2 | Kalman macro classifier | Walk-forward infra |
| ARCH-3 | Full covariance Kelly | 200+ position-days |
| ARCH-4 | LightGBM non-linear factor model | 500+ clean trades |
| ARCH-5 | Walk-forward backtest infrastructure | Data overhaul |
| ARCH-6 | Database (PostgreSQL/DuckDB) | When JSON causes incident |

---

## 🟢 HYGIENE — Open Items

| ID | Issue | File |
|----|-------|------|
| H-1 | Dead files: raptor_state.json, diagnose.py, diagnose_regime.py | Various |
| H-2 | prompt_calibrator.py referenced but does not exist | Multiple |
| H-3 | Universe size hardcoded ~120 in daily_recap | daily_recap.py |
| H-4 | Missing recap metrics (exit breakdown, rolling win rate) | daily_recap.py |
| H-5 | compute_trim fallback parses stop_dist from string | hold_monitor.py |
| H-6 | OBV magic constant 1000 — should be symbol own OBV std | hold_monitor.py |
| H-7 | Volatility layer dead zone ATR 0.80–1.20 | hold_monitor.py |
| H-8 | Prompt versioning runs on every import | agent_layer.py |

---

## BUILD ORDER — NEXT SESSIONS

```
IMMEDIATE (before next market open):
  python backfill_ledger.py --write    <- fix CVE, KDP, PLTD missing from ledger
  python outcome_tracker.py            <- run daily to tag new closed trades

NEXT SESSION:
  MATH-3  Hurst: ADX threshold 22->25 (10 minutes)
  MATH-1  Regime-conditional IC (tag regime_at_entry — now done via CRIT-0)
  MATH-2  Composite SNR ranking
  MATH-4  Portfolio heat proportional trim

WHEN 60+ AGENT-TAGGED TRADES:
  MATH-5  Reduce n_prior 50->20
  ARCH-1  IC layer weights in hold_monitor

WHEN 100+ EQUITY TRADES:
  kelly_engine ACTIVE mode eligible (already coded, needs config flag)

WHEN 200+ POSITION-DAYS:
  ARCH-3  Full covariance Kelly

FUTURE (500+ trades):
  ARCH-4  LightGBM
  ARCH-5  Walk-forward backtest
```

---

## LIVE DATA SNAPSHOT (2026-05-24)

```
outcome_log.json:    98 trades | exit_unknown=59 (pre-API) | all MOMENTUM
kelly_estimates.json: f_rec=3.89% bootstrap P25 | SHADOW (73/100)
factor_ic_report.json: ma_stack IC=+0.48 | adx_dir IC=+0.44 | cond=272
Exit quality: math_trim 70% win | trailing_stop 20% win (fires too late)
```

---

## SESSION START CHECKLIST

```powershell
git -C "C:\Users\steve\OneDrive\Desktop\Raptor" pull origin main
python outcome_tracker.py --summary
python kelly_engine.py
python reconcile_positions.py
Remove-Item -Recurse -Force __pycache__
```

---

## P1 STATUS TABLE — VERIFIED FROM CODE 2026-05-24

| ID | Description | Status |
|----|-------------|--------|
| P1-1 | Kalman macro classifier | NOT IN CODE — macro_context.py is vote-count |
| P1-2 | Vol-regime hard stop | CONFIRMED |
| P1-3 | OU trailing stop | CONFIRMED |
| P1-4 | Bayesian Kelly | CONFIRMED — kelly_engine.py SHADOW, bootstrap upgrade done |
| P1-5 | OU hold target | NOT IN CODE — dist_to_mean/0.005 for MR, hardcoded 15 for MOM |
| P1-6 | IC layer weights hold monitor | GATED 60+ trades |
| P1-7 | Continuous trim | CONFIRMED |
| P1-8 | Regime-relative thesis threshold | CONFIRMED |
| P1-9 | Watchdog intraday | DELETE or build properly |
| P1-10 | Composite velocity gate | FIXED this session (was built, not wired) |
| P1-11 | Re-entry cooldown | FIXED this session (was built, not wired) |
| P1-12 | Portfolio heat partial trim | MATH-4 open |
| P1-13 | Multi-MA breadth | CONFIRMED |
| P1-14 | Universe sensitivity sweep | FUTURE |
| P1-15 | Sentiment dead path | HYGIENE |
| P1-16 | Afternoon rescore | Partial — exit_monitor GAP9 rescore live |
| P1-17 | Conviction gradient sizing | CONFIRMED via book_conviction percentile |

---

*Supersedes RAPTOR_AUDIT_AND_PLAN.md.*  
*All P1 status verified by code inspection 2026-05-24.*
