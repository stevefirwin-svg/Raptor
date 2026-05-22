# Raptor — Master Priority Plan
*Supersedes RAPTOR_AUDIT_AND_PLAN.md*
*Last updated: 2026-05-22. Incorporates: original audit (2026-05-19), sessions 18–19, Grok review, ChatGPT review.*

---

## The Standard

Every number in the system must be derivable from a formula, empirical data, or an optimization. If someone asks "why that number?" and the answer is anything other than a mathematical derivation or measured value from Raptor's own data, the number is wrong and must be redesigned.

---

## Current System State

| Layer | Status |
|-------|--------|
| P0 Blockers | ✅ ALL 8 FIXED (2026-05-20) |
| P1 Math Foundation (Week 2) | ✅ 10 of 17 LIVE (2026-05-22) |
| P1 Remaining | ❌ 7 items open |
| Statistical Rigor (Grok/ChatGPT review) | ❌ 8 new items identified |
| P2 Hygiene | ❌ ~12 items open |

---

## CATEGORY DEFINITIONS

| Category | Meaning | Act on it when |
|----------|---------|----------------|
| **🔴 CRITICAL** | Silent corruption, catastrophic sizing risk, or data that invalidates all other math | Immediately |
| **🟠 MATH** | Static where dynamic is required, or statistical error in a live formula | Next session |
| **🟡 ARCHITECTURE** | Correct design direction but premature at current data scale | When data gate is met |
| **🟢 HYGIENE** | Dead code, fragile I/O, missing instrumentation | Rolling, any session |

---

## 🔴 CRITICAL — Do These Before Any Further Math Improvements

These are either bugs that corrupt live decisions or statistical errors so severe they override the benefit of everything built above them.

---

### CRIT-1 — Bootstrap Kelly (replaces current μ/σ² formula)

**Status:** ❌ NOT BUILT  
**File:** `signals.py` → `_bayesian_kelly()`  
**Severity:** The current Kelly formula is sitting on a distribution where P25 of bootstrap resamples = -75%. The Bayesian prior (n_prior=50) accidentally saves us by overriding the empirical number, but this is safety by coincidence, not design.

**Data proof (run on actual outcome_log.json, 2026-05-22):**
```
Empirical f*:        29.6%
Bootstrap median:    25.5%
Bootstrap P25:      -75.4%   ← 25% of resamples say short yourself
Bootstrap P10:     -207.0%
Return skewness:     2.42    ← severely fat-tailed right
Return kurtosis:    10.76    ← not close to normal
```

The μ/σ² formula assumes iid normal returns. Kurtosis=10.8 means the tails are 4× fatter than normal. A single big winner (42% gain on one trade) inflates μ and makes the formula wildly unstable.

**Fix:**
```
1. Draw 10,000 bootstrap samples (with replacement) from outcome_log trades
2. Compute f* = μ/σ² for each sample
3. f_final = P25(bootstrap_f*)   ← conservative percentile, not median
4. Still apply half-Kelly and n_prior shrinkage on top
5. Log f_bootstrap_p25, f_bootstrap_median, f_bootstrap_p10 each scan
```

**Reference:** Thorp (2006) — Kelly under parameter uncertainty. Bootstrap Kelly is standard practice at quant funds precisely because of fat-tailed return distributions.

**Gate:** None. Build immediately.

---

### CRIT-2 — Exponential Decay on All Learning

**Status:** ❌ NOT BUILT  
**Files:** `signals.py` → `AdaptiveWeights`, `_bayesian_kelly()`  
**Severity:** All three learning components (Ridge regression, IC boost, Bayesian Kelly) treat a trade from 6 months ago equally to one from yesterday. Markets regime-shift. The current system will be training on a 2024 momentum regime while deploying in a 2026 mean-reversion regime.

**Fix — add decay weights everywhere:**
```
λ = 0.005   (half-life ≈ 139 days — recent 3 months dominate, older fades)
w_t = exp(-λ × days_since_trade)

Ridge regression: weighted least squares — X.T @ W @ X + λI
IC calculation:   weighted sign-match fraction instead of equal-weight
Bayesian Kelly:   weight each trade's pnl by w_t before computing μ and σ²
```

**Why λ=0.005:** Half-life of 139 days means trades from 6 months ago have 50% weight, trades from 1 year ago have 16% weight. This matches the typical regime persistence horizon for US equity factors (Asness, Moskowitz & Pedersen 2013).

**Reference:** Exponentially weighted moving covariance is standard in RiskMetrics (1994) and all modern volatility estimators. Same principle applies to signal learning.

**Gate:** None. Build immediately — one parameter, applied in three places.

---

### CRIT-3 — Rank IC (Spearman) Replaces Binary IC

**Status:** ❌ NOT BUILT  
**File:** `signals.py` → `AdaptiveWeights._get_ic_boost()`  
**Severity:** Current IC discards magnitude. A factor predicting +20% and +0.1% return gets equal credit. Rank IC captures the full ordinal relationship.

**Data proof:**
```
On simulated data with identical signal quality:
Binary IC:  0.06   ← discards 80% of available information
Rank IC:    0.30   ← full ordinal relationship preserved
```

**Current code (wrong):**
```python
ic = {fn: sum(1 for t in recent if t.get(fn,0)*t.get("y",0)>0)/len(recent) - 0.5}
```

**Fix:**
```python
from scipy.stats import spearmanr
ic = {}
for fn in self.factor_names:
    z_scores = [t.get(fn, 0.0) for t in recent]
    returns  = [t.get("y", 0.0) for t in recent]
    rho, _ = spearmanr(z_scores, returns)
    ic[fn] = rho if not np.isnan(rho) else 0.0
```

**Reference:** Grinold & Kahn (1999) — "Active Portfolio Management." Spearman rank IC is the universal standard for factor evaluation in quantitative finance.

**Gate:** None. One function, 5 minutes.

---

### CRIT-4 — Atomic JSON Writes

**Status:** ❌ NOT BUILT  
**Files:** Every file that calls `path.write_text(...)` or `json.dump(f, ...)`  
**Severity:** A Python crash during a write to `composite_cache.json`, `cooldown_log.json`, `adaptive_weights.json`, or `outcome_log.json` leaves a half-written file. The next read returns corrupt data and silently produces wrong decisions.

**Fix — one pattern, apply everywhere:**
```python
import os, tempfile, json
from pathlib import Path

def atomic_write(path: Path, data: dict):
    """Write JSON atomically — crash-safe."""
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)   # atomic on all major OS
```

`os.replace()` is atomic at the OS level — either the old file exists or the new one does, never a partial write.

**Files to update:** `composite_cache.json` writes in main.py, `cooldown_log.json` writes in exit_monitor.py and main.py, `adaptive_weights.json` writes in signals.py, `outcome_log.json` writes in outcome_tracker.py, `hold_health.json` writes in hold_monitor.py.

**Gate:** None. One hour of work.

---

### CRIT-5 — No Portfolio Correlation Model

**Status:** ❌ NOT BUILT  
**File:** `main.py` (entry sizing), `signals.py` (Kelly)  
**Severity:** Kelly assumes positions are independent. If 7 of 10 positions are in correlated sectors (energy, tech), effective portfolio volatility is far higher than `sum(individual_kelly × equity)` implies. At $105K with 10% Kelly, this means a correlated drawdown could be 2-3× worse than expected.

**Example:** NVDA + AMD + SMH + TSM = one semiconductor trade with 4× leverage. None are blocked by the current system.

**Fix (simple version — no full covariance matrix needed):**
```
For each new signal candidate:
  1. Get 60-day daily returns for candidate and all held positions
  2. Compute pairwise correlation with each held position
  3. If any correlation > 0.70: kelly_adj = kelly × (1 - max_correlation)
  4. If sector matches an existing position: additional 20% reduction
```

**Upgrade path (when 200+ position-days of data available):**  
Full minimum-variance portfolio Kelly scaling: `f_adj = f × (σ_target / σ_portfolio)` where σ_portfolio accounts for the full covariance matrix.

**Reference:** Markowitz (1952), modern portfolio theory. ChatGPT review correctly identified this as P0 severity.

**Gate:** None for simple version. Full covariance requires ~200 position-days.

---

## 🟠 MATH — Statistical Improvements to Live Formulas

These are live formulas with known mathematical errors. Not bugs, but incorrect specifications that reduce alpha or increase risk.

---

### MATH-1 — Regime-Conditional Learning

**Status:** ❌ NOT BUILT  
**File:** `signals.py` → `AdaptiveWeights`  
**Priority:** Highest of the MATH tier

**Problem:** The Ridge regression and IC boost pool all trades regardless of macro regime. A mean-reversion factor with IC=+0.15 in RISK_ON may have IC=-0.10 in RISK_OFF. Mixing them gives IC≈0 and the false conclusion the factor is useless.

**Fix:**
```
Maintain separate IC buckets per regime:
  ic_by_regime = {
      "RISK_ON":  {fn: spearman_ic, ...},
      "NEUTRAL":  {fn: spearman_ic, ...},
      "RISK_OFF": {fn: spearman_ic, ...},
  }

When blending weights:
  current_regime = macro_context.json → macro_regime
  use ic_by_regime[current_regime] for IC boost
  fallback to pooled IC if regime bucket has < 10 trades
```

Minimum trades per regime bucket before regime IC activates: 10.

**Gate:** Needs 10+ trades per regime bucket. Start accumulating immediately by tagging all outcome_log entries with regime_at_entry.

---

### MATH-2 — Composite Signal Uncertainty

**Status:** ❌ NOT BUILT  
**File:** `signals.py` → `generate_signals()`  
**Problem:** Two stocks with identical composite mean score are treated identically even when one has all factors agreeing (high certainty) and one has factors split (low certainty).

**Example:**
```
Stock A: factors = [0.9, 0.8, 0.9, 0.8]  → composite = 0.85, std = 0.05
Stock B: factors = [0.9, -0.8, 1.1, 0.5] → composite = 0.43, std = 0.73
```

Same composite if weights are equal. Stock B should be demoted.

**Fix:**
```python
composite_mean = sum(z[fn] * w[fn] for fn in active) / active_weight_sum
composite_std  = sqrt(sum(w[fn]**2 * (z[fn] - composite_mean)**2 for fn in active))
snr = composite_mean / (composite_std + 0.5)   # signal-to-noise ratio
```

Use `snr` for ranking instead of raw composite. The +0.5 prevents division-by-zero and provides shrinkage for low-factor-count symbols.

**Note:** The current t-statistic partially captures this but uses cross-factor z-score dispersion (disagreement among factors within a stock) rather than the signal-to-noise ratio. Both should be computed and logged.

---

### MATH-3 — Hybrid Normalization (Cross-Sectional + Time-Series)

**Status:** ❌ NOT BUILT  
**File:** `signals.py` → `_raw()`, z-score computation  
**Problem:** Pure cross-sectional normalization destroys absolute level information. In a market-wide crash, a stock down 4% when everything is down 10% gets a *positive* z-score — "relatively good." But absolute conditions are poor and the stock is still losing money.

**Fix:**
```
For each factor, compute two z-scores:
  z_cross = (raw - cross_sectional_median) / (cross_sectional_MAD × 1.4826)
  z_time  = (raw - own_60d_median) / (own_60d_MAD × 1.4826)   ← stock vs itself

z_final = 0.5 × z_cross + 0.5 × z_time
```

The time-series component requires storing 60 days of each factor's own values per symbol. This is an additional data structure but preserves absolute level signals that cross-sectional normalization suppresses.

**Gate:** Requires 60-day per-symbol factor history. Build a `factor_history_cache.json` alongside composite_cache.json, then blend.

---

### MATH-4 — Hurst Exponent Stability

**Status:** ❌ NOT BUILT  
**File:** `signals.py` → `Factors.hurst()`  
**Problem:** R/S analysis over lags 2–20 with only ~80 bars produces unreliable estimates. At lag=20, only 4 non-overlapping segments exist — the variance of the Hurst estimate is very high. Micro-regime misclassification (TRENDING vs REVERTING) corrupts factor weights for the entire signal.

**Better approach — Detrended Fluctuation Analysis (DFA):**
```
DFA is more robust than R/S for short series:
1. Integrate the return series (cumulative sum)
2. Divide into non-overlapping windows of size s
3. Fit linear trend in each window, compute residual variance
4. F(s) = sqrt(mean residual variance across windows)
5. log(F(s)) vs log(s) slope = Hurst exponent
```

DFA is the modern standard for Hurst estimation on financial time series (Kantelhardt et al. 2002). It is less biased than R/S at short lags.

**Interim fix (cheaper):** Add a minimum ADX confirmation. Currently TRENDING requires Hurst > 0.55 AND ADX > 25. Raise to ADX > 30 to reduce noise. This partially compensates for unreliable Hurst without requiring DFA implementation.

**Gate:** None for ADX threshold tightening. DFA implementation: any session.

---

### MATH-5 — n_prior Reduction in Bayesian Kelly

**Status:** PENDING DATA GATE  
**File:** `signals.py` → `_bayesian_kelly()`  
**Current:** n_prior=50 (very conservative, dominating the empirical data)  
**When to change:** When 60+ agent-tagged trades exist in outcome_log.json  
**Change:** Reduce n_prior to 20. At that point the data quality is sufficient for the empirical component to contribute meaningfully.

**How to check:** `python -c "import json; ol=json.load(open('outcome_log.json')); print(len([t for t in ol if t.get('entry_decision') not in [None,'no_record']]))"

---

### MATH-6 — P1-9 Watchdog Intraday Bars

**Status:** ❌ NOT BUILT  
**File:** `watchdog.py`  
**Problem:** Watchdog fetches 5 daily bars and calls it intraday monitoring. EMA8 on 5 daily bars is a 5-day MA — not intraday. The SPY circuit breaker triggers on -3% absolute which ignores daily vol regime.

**Fix:**
```
Use Alpaca 15-min bar endpoint
Intraday vol = realized_variance(5-min_returns) × sqrt(78)
SPY circuit breaker = intraday_return < -3σ_daily_vol  (not -3% absolute)
```

**Alternative:** Delete watchdog.py and Start_Watchdog.bat if intraday infrastructure is not worth building. Do not pretend to monitor intraday when you're not.

---

### MATH-7 — P1-12 Portfolio Heat Proportional Trim

**Status:** ❌ NOT BUILT  
**File:** `exit_monitor.py`  
**Current:** When portfolio_dd < -12%, full exit of weakest composite position. Binary.  
**Fix:**
```
α = abs(portfolio_dd) / max_portfolio_dd    ← severity fraction
For each position with health < 0:
    trim_pct = α × abs(health)              ← proportional to both severity and health
```

Spreads risk reduction across multiple positions, preserves optionality on recovery, and scales the trim to the actual portfolio heat level.

---

### MATH-8 — P1-16 Afternoon Rescore

**Status:** ❌ NOT BUILT  
**File:** `signals.py`, bat files  
**Fix:** Run lightweight signal engine at 3:50 PM on held positions + top 30 candidates only. Flag for priority next-morning exit if composite decayed > 1σ AND leapfrogged by a stronger fresh signal. ~5 second marginal cost.

---

## 🟡 ARCHITECTURE — Correct Direction, Premature at Current Scale

These are the right long-term designs but require more data or infrastructure than currently exists.

---

### ARCH-1 — Factor Interaction Terms / Non-Linear Model

**Status:** FUTURE  
**Gate:** 500+ clean tagged trades  
**Why premature:** XGBoost with interaction terms needs ~1000 labeled examples to generalize. At 79 trades, adding non-linearity is pure overfit. Ridge with λ=1.0 is the correct choice at this data scale.  
**When ready:** Replace AdaptiveWeights Ridge with LightGBM. Test on walk-forward holdout before deploying.

---

### ARCH-2 — Full Portfolio Covariance Kelly Scaling

**Status:** FUTURE  
**Gate:** 200+ position-days  
**Why premature:** A covariance matrix estimated from fewer than 200 observations per asset pair is unreliable. Simple correlation gate (CRIT-5) is the right interim solution.  
**When ready:** `f_adj = f × (σ_target / σ_portfolio)` where σ_portfolio comes from rolling 60-day covariance matrix of all held positions.

---

### ARCH-3 — Hidden Markov Model for Regime

**Status:** FUTURE  
**Gate:** 5+ years of FRED data calibrated, separate from live system  
**Current Kalman filter is sufficient** for now. HMM adds explicit regime transition probabilities and multi-regime probability distributions. Build when walk-forward validation infrastructure exists.

---

### ARCH-4 — Walk-Forward Backtest on Full Pipeline

**Status:** FUTURE  
**Gate:** Requires data infrastructure overhaul  
**What's needed:** Historical bar data, FRED history, full pipeline replay with parameter snapshots. Validates whether adaptive weights, Kalman, OU estimates actually improved realized Sharpe out-of-sample vs static baseline.

---

### ARCH-5 — Database (PostgreSQL/DuckDB) Replacing JSON

**Status:** FUTURE  
**Gate:** When JSON soup causes a data corruption incident, or system scales beyond 1 machine  
**Interim:** Atomic writes (CRIT-4) + schema validation. JSON is fine for $105K single-machine deployment.

---

### ARCH-6 — P1-6 IC Layer Weights in Hold Monitor

**Status:** GATED (need 60+ agent-tagged trades)  
**File:** `hold_monitor.py` → `LAYER_WEIGHTS`  
**Current:** 0.25 / 0.20 / 0.15 / 0.15 / 0.10 / 0.08 / 0.05 / 0.02 — hand-picked  
**Fix:** Spearman IC of each layer score vs forward 5/10-day trade return from hold_history.json + outcome_log. Rolling 90-day window. Normalize, no negative weights.  
**Do not touch layer weights manually before 60+ trades.**

---

## 🟢 HYGIENE — Rolling Improvements

### Open Hygiene Items (from original audit, incomplete as of 2026-05-22)

| ID | Issue | File | Status |
|----|-------|------|--------|
| P2-1 | Dead files: outcome_tracker_v2.py, send_ontology_email.py, raptor_state.json, diagnose.py | Various | ❌ Open |
| P2-2 | Duplicate/broken bat files | *.bat | ❌ Open |
| P2-3 | Universe size hardcoded "~120" in recap | daily_recap.py | ❌ Open |
| P2-4 | Missing recap metrics (exit breakdown, rolling win rate, etc.) | daily_recap.py | ❌ Open |
| P2-6 | compute_trim fallback parses stop_dist from string | hold_monitor.py | ❌ Open (fallback path only) |
| P2-7 | OBV magic constant 1000 — should be symbol's own OBV std | hold_monitor.py | ❌ Open |
| P2-8 | Volatility layer returns 0.0 for ATR 0.80–1.20 regardless of PnL | hold_monitor.py | ❌ Open |
| P2-9 | stop_distance layer returns 0.0 if dist==0 (Python falsy bug) | hold_monitor.py | ❌ Open |
| P2-10 | pre_entry_health computed but not used as entry gate | main.py | ❌ Open |
| P2-12 | Prompt versioning runs on every import | agent_layer.py | ❌ Open |
| P2-13 | outcome_tracker duplicate exit_path patterns | outcome_tracker.py | ❌ Open |
| P2-14 | prompt_calibrator.py referenced everywhere but doesn't exist | Multiple | ❌ GATED on 30+ agent-tagged trades |
| P2-15 | EQUITY_ALLOCATION=1.00 dead constant | main.py | ✅ FIXED 2026-05-22 |
| P2-16 | kelly_fraction=0.15 in config is dead | config.py | ✅ FIXED 2026-05-22 |

---

## COMPLETE PRIORITY QUEUE — NEXT SESSION ORDER

**Read this top to bottom. This is the build order.**

```
SESSION NEXT:
  CRIT-3  Rank IC (Spearman) — 10 minutes, one function
  CRIT-4  Atomic JSON writes — 1 hour, applies everywhere
  CRIT-1  Bootstrap Kelly — replaces current μ/σ² formula
  CRIT-2  Exponential decay on all learning (Ridge + IC + Kelly)

SESSION AFTER:
  CRIT-5  Portfolio correlation gate (simple version)
  MATH-1  Regime-conditional IC buckets
  MATH-2  Composite signal uncertainty (SNR)
  MATH-7  P1-12 Portfolio heat proportional trim
  MATH-8  P1-16 Afternoon rescore

WHEN DATA GATE MET (60+ agent-tagged trades):
  MATH-5  Reduce n_prior to 20 in _bayesian_kelly()
  ARCH-6  IC layer weights in hold_monitor

ONGOING:
  MATH-4  Hurst DFA upgrade (or ADX threshold tightening interim)
  MATH-3  Hybrid normalization (requires per-symbol factor history cache)
  MATH-6  P1-9 Watchdog intraday (or delete)
  P2 hygiene items — any session, any order

FUTURE (500+ trades):
  ARCH-1  Non-linear factor model (LightGBM)
  ARCH-2  Full covariance Kelly
  ARCH-3  HMM regime model
  ARCH-4  Walk-forward backtest infrastructure
```

---

## MASTER STATUS TABLE

### P0 (Blockers)
| ID | Description | Status |
|----|-------------|--------|
| P0-1 | outcome_tracker dead | ✅ FIXED |
| P0-2 | Positions below stop, no exit | ✅ FIXED |
| P0-3 | Regime schema fractured | ✅ FIXED |
| P0-4 | daily_recap market_value field | ✅ FIXED |
| P0-5 | watchdog get_bars() missing | ✅ FIXED |
| P0-6 | _env vs .env | ✅ NOT A BUG |
| P0-7 | Sharpe/Sortino annualization wrong | ✅ FIXED |
| P0-8 | Two separate macro fetches | ✅ FIXED |

### P1 (Alpha Gaps — Original Audit)
| ID | Description | Status |
|----|-------------|--------|
| P1-1 | Kalman macro classifier | ✅ LIVE |
| P1-2 | Vol-regime hard stop | ✅ LIVE |
| P1-3 | OU trailing stop | ✅ LIVE |
| P1-4 | Bayesian Kelly (μ/σ² base) | ✅ LIVE (CRIT-1 upgrades this) |
| P1-5 | OU hold target | ✅ LIVE |
| P1-6 | IC layer weights | ⏳ GATED (60+ trades) |
| P1-7 | Continuous trim | ✅ LIVE |
| P1-8 | Regime-relative thesis threshold | ✅ LIVE |
| P1-9 | Watchdog intraday bars | ❌ MATH-6 |
| P1-10 | Composite velocity gate | ✅ LIVE |
| P1-11 | Re-entry cooldown | ✅ LIVE |
| P1-12 | Portfolio heat partial trim | ❌ MATH-7 |
| P1-13 | Multi-MA breadth | ✅ LIVE |
| P1-14 | Universe sensitivity sweep | ❌ FUTURE |
| P1-15 | Sentiment / dead path | ❌ HYGIENE |
| P1-16 | Afternoon rescore | ❌ MATH-8 |
| P1-17 | Conviction gradient sizing | ✅ COVERED BY P1-4 |

### New Items from External Reviews (Grok + ChatGPT)
| ID | Description | Category | Status |
|----|-------------|----------|--------|
| CRIT-1 | Bootstrap Kelly | 🔴 CRITICAL | ❌ NOT BUILT |
| CRIT-2 | Exponential decay on learning | 🔴 CRITICAL | ❌ NOT BUILT |
| CRIT-3 | Rank IC (Spearman) | 🔴 CRITICAL | ❌ NOT BUILT |
| CRIT-4 | Atomic JSON writes | 🔴 CRITICAL | ❌ NOT BUILT |
| CRIT-5 | Portfolio correlation gate | 🔴 CRITICAL | ❌ NOT BUILT |
| MATH-1 | Regime-conditional IC | 🟠 MATH | ❌ NOT BUILT |
| MATH-2 | Composite signal uncertainty (SNR) | 🟠 MATH | ❌ NOT BUILT |
| MATH-3 | Hybrid cross-sectional + time-series norm | 🟠 MATH | ❌ NOT BUILT |
| MATH-4 | Hurst DFA upgrade | 🟠 MATH | ❌ NOT BUILT |
| MATH-5 | n_prior reduction | 🟠 MATH | ⏳ GATED |
| ARCH-1 | Non-linear factor model | 🟡 ARCH | ⏳ FUTURE |
| ARCH-2 | Full covariance Kelly | 🟡 ARCH | ⏳ FUTURE |
| ARCH-3 | HMM regime model | 🟡 ARCH | ⏳ FUTURE |
| ARCH-4 | Walk-forward backtest | 🟡 ARCH | ⏳ FUTURE |
| ARCH-5 | Database replacing JSON | 🟡 ARCH | ⏳ FUTURE |
| ARCH-6 | IC layer weights hold monitor | 🟡 ARCH | ⏳ GATED |

---

## SESSION START CHECKLIST

Run these every session before touching any code:

```bash
# 1. Clone fresh from GitHub
git clone https://github.com/stevefirwin-svg/Raptor /home/claude/raptor

# 2. Check outcome data quality
python3 -c "
import json
ol = json.load(open('/home/claude/raptor/outcome_log.json'))
total = len([t for t in ol if t.get('actual_pnl_pct') is not None])
tagged = len([t for t in ol if t.get('entry_decision') not in [None,'no_record']])
print(f'Total trades: {total}')
print(f'Agent-tagged: {tagged}')
print(f'n_prior should be: {50 if tagged < 60 else 20}')
"

# 3. Check velocity cache
python3 -c "
import json; from pathlib import Path
p = Path('/home/claude/raptor/composite_cache.json')
if p.exists():
    d = json.loads(p.read_text())
    print(f'Velocity cache: {len(d)} days — gate active: {len(d) >= 3}')
"

# 4. Check cooldown log
python3 -c "
import json; from pathlib import Path; from datetime import date
p = Path('/home/claude/raptor/cooldown_log.json')
if p.exists():
    d = json.loads(p.read_text())
    today = date.today()
    active = {k: v for k,v in d.items() if date.fromisoformat(v) >= today}
    print(f'Active cooldowns: {active}')
"
```

---

## WHAT RAPTOR IS NOW vs WHAT IT NEEDS TO BECOME

**Current (as of 2026-05-22):**
> "A sophisticated rule engine with genuine but misspecified statistical adaptation."
> (ChatGPT review — accurate)

The math is real. The regime awareness is real. The OU theta, Kalman filter, Bayesian Kelly — all real. But the learning layer has statistical errors (binary IC, no decay, pooled across regimes) that make the adaptation less effective than it appears.

**Target:**
> "A self-calibrating quantitative system where every parameter has a derivation trail and updates from its own trade history."

The path from here to there is: CRIT items first (fix the math errors), then MATH items (improve the specifications), then ARCH items (upgrade the architecture when data supports it).

**The single most important sentence from the external reviews:**
> "Bootstrap Kelly P25 = -75.4%. The Bayesian prior saves you accidentally. Fix this by design."

---

*End of master plan. Supersedes RAPTOR_AUDIT_AND_PLAN.md.*
*Author: Claude. Reviewed with Grok and ChatGPT external audits incorporated.*
*Next session: CRIT-3 (Rank IC) → CRIT-4 (Atomic writes) → CRIT-1 (Bootstrap Kelly) → CRIT-2 (Decay)*
