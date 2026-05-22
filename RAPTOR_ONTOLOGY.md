# Raptor v5.4 — Complete System Ontology
*Full decision logic, mathematics, and feedback loops. No code.*
*Last updated: 2026-05-22*

---

## Purpose of This Document

This document describes every decision Raptor makes, from the first market open scan to the final exit, including all the math underneath each decision. It is written for someone who wants to understand the logic completely, find flaws, or audit whether the system behaves as intended. No programming knowledge is required to read it.

Every number that is hardcoded is labeled as such. Every number derived from data is explained.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [The Macro Layer — Daily Market Regime](#2-the-macro-layer--daily-market-regime)
3. [Universe Construction](#3-universe-construction)
4. [The Signal Engine — Generating Entry Candidates](#4-the-signal-engine--generating-entry-candidates)
5. [Entry Filters — Pre-Trade Gates](#5-entry-filters--pre-trade-gates)
6. [Position Sizing — Kelly Fraction](#6-position-sizing--kelly-fraction)
7. [Entry Execution](#7-entry-execution)
8. [Hold Monitor — Daily Position Health Scoring](#8-hold-monitor--daily-position-health-scoring)
9. [Exit Monitor — Exit and Trim Decisions](#9-exit-monitor--exit-and-trim-decisions)
10. [The Feedback Loop — Learning from Outcomes](#10-the-feedback-loop--learning-from-outcomes)
11. [Adaptive Weight System](#11-adaptive-weight-system)
12. [Agent Layer — LLM Advisory](#12-agent-layer--llm-advisory)
13. [Daily Schedule and File Topology](#13-daily-schedule-and-file-topology)
14. [Known Gaps and Open Problems](#14-known-gaps-and-open-problems)

---

## 1. System Overview

Raptor is a quantitative swing trading system running on a single Alpaca paper account (~$105K equity). It holds between 0 and 10 positions at a time, with a typical hold duration of 5–20 trading days. It is designed for US equities only.

### 1.1 Core Design Principle

Every decision in Raptor is made by math first. An LLM agent (Ollama/llama3.2, running locally) observes the same decisions and provides reasoning in natural language, but the agent never executes a trade. The agent's output is recorded for calibration — to check whether its reasoning correlates with actual outcomes over time.

### 1.2 Five Stages of Every Trade

```
STAGE 1: MACRO GATE       — Is it safe to enter anything today?
STAGE 2: UNIVERSE SCAN    — Which ~120-150 symbols to score?
STAGE 3: SIGNAL ENGINE    — Which symbols have genuine edge (composite score > 0)?
STAGE 4: ENTRY FILTERS    — Pre-trade gates (in order):
                             1. Already-held filter
                             2. Re-entry cooldown (hard_stop/trail_loss within 5 days)
                             3. Margin guard (util >90% block, >85% or on-margin reduce)
                             4. EntryAgent veto (Ollama/llama3.2)
STAGE 5: EXECUTION        — Velocity-adjusted Kelly sizing, submit, record
```

Once in a position, two parallel systems run daily:

```
HOLD MONITOR   — Scores position health on 8 factors, recommends trim/hold
EXIT MONITOR   — Checks 5 exit conditions, executes exits and trims
```

These feed back into the learning loop via `outcome_log.json`.

---

## 2. The Macro Layer — Daily Market Regime

**File:** `macro_context.py`  
**Output:** `macro_context.json` with a single regime label and a continuous risk score.  
**Runs:** Once per day, pre-market (~8:00 AM).

### 2.1 Six Input Signals

The macro classifier ingests six market signals, each converted to a continuous score in the range [-1.0, +1.0]:

| Signal | Data Source | What It Measures |
|--------|------------|-----------------|
| **SPY trend** | Yahoo Finance | 20-day return + MA relationship |
| **VIX** | Yahoo Finance (^VIX) | Implied volatility regime |
| **Credit spread** | FRED (HY-IG spread or ICE BofA) | Credit stress |
| **Sector breadth** | Yahoo Finance (11 sector ETFs) | Market internals width |
| **Yield curve** | FRED (T10Y2Y) | Recession signal |
| **Fed rate** | FRED (FEDFUNDS) | Monetary policy stance |

### 2.2 Signal-to-Score Conversion

Each raw signal is converted to [-1, +1] without quantization:

**SPY trend:**  
`score = clip(20d_return / 5%, -1, 1) + (0.2 if price > 200MA else -0.2)`  
A 5% SPY move over 20 days normalizes to ±1. Being above the 200MA adds ±0.2 on top.

**VIX:**  
`score = -clip((VIX - 20) / 15, -1, 1)`  
VIX=20 is neutral (score=0). VIX=35 is score=-1.0 (extreme fear). VIX=5 is score=+1.0.

**Credit spread:**  
`score = -clip(spread / 3.5%, -1, 1)`  
A 3.5% HY-IG spread normalizes to -1.0 (extreme stress). Near-zero spread = +1.0.

**Sector breadth (P1-13, Zweig 1986):**  
Three moving averages computed per sector ETF (50/150/200 day). ✅ **IMPLEMENTED 2026-05-22.**  
Composite breadth score (weighted blend):  
`breadth_composite = 0.40 × pct_above_50MA + 0.35 × pct_above_150MA + 0.25 × pct_above_200MA`  

*Note: ontology weight order differs from code — code weights 50/150/200 as 40/35/25% (short-term most responsive gets highest weight). Longer MAs get lower weight in the composite but drive the `structural` field (BULL/NEUTRAL/BEAR) separately, which adds an extra vote to `classify_macro()`. Net effect: 200MA still most influential via the structural bonus vote.*

Structural classification from 200MA breadth:
- ≥70% of sectors above 200MA → `BULL_MARKET` → +1 extra vote  
- <50% of sectors above 200MA → `BEAR_MARKET` → -1 extra vote  

Score mapped to regime: composite ≥70% → `BROAD_STRENGTH`, ≥50% → `MIXED`, ≥30% → `WEAKENING`, else → `BROAD_WEAKNESS`

**Yield curve:**  
`score = clip(T10Y2Y_spread / 1.5%, -1, 1)`  
A spread of +1.5% = +1.0 (normal, positive-sloping). Inverted at -1.5% = -1.0.

**Fed rate:**  
`score = -clip((fed_funds - 2.5%) / 3%, -1, 1)`  
Rates above 5.5% score -1.0 (restrictive). Rates near 2.5% score 0. Near 0% score +1.0.

### 2.3 Weighted Composite Risk Score

```
raw_score = 0.30 × spy + 0.25 × vix + 0.20 × credit + 0.15 × breadth + 0.07 × yield_curve + 0.03 × fed
```

**Weights sum to 1.0.** SPY gets the highest weight because it is the most direct real-time signal of equity regime. VIX second because it captures forward-looking fear. Credit third because high-yield stress is a leading indicator of drawdown. Breadth fourth because it measures participation width.

*Known flaw: weights are hand-picked and not calibrated against historical regime prediction accuracy. This is logged as an open gap.*

**⚠ IMPLEMENTATION NOTE (2026-05-22):** The current `macro_context.py` uses a vote-count scoring system (each signal votes +1/0/-1) rather than the weighted continuous score described above. The Kalman filter (§2.4) and hysteresis (§2.5) are also not yet implemented — the code classifies directly from vote totals with fixed thresholds. The architecture described in §2.3–2.5 is the *target design*, not the current implementation. GAP A (HMM/Kalman regime classifier) covers this but is currently gated — do not implement until regime stability requirements are clearer.

### 2.4 Kalman Filter Smoothing

The raw score is smoothed using a scalar Kalman filter to prevent a single bad day from flipping the regime label.

**State equations:**
```
x_prior = x_previous                  (regime drifts slowly, no drift term)
P_prior = P_previous + Q              (Q = 0.05, process noise)

K = P_prior / (P_prior + R)           (Kalman gain; R = 0.20, observation noise)
x_updated = x_prior + K × (raw_score - x_prior)
P_updated = (1 - K) × P_prior
```

The Kalman state `(x, P)` is persisted in `macro_context.json` and loaded on the next run.  
*Interpretation: R=0.20 means the filter trusts observed signals at about 20% weight per step. The filter will take approximately 5 days of persistent signal before fully updating the state.*

### 2.5 Regime Classification with Hysteresis

The smoothed score is discretized into four regime labels. Hysteresis of ±0.10 prevents rapid flickering when the score sits near a boundary:

| Smoothed Score | Regime Label |
|----------------|-------------|
| ≥ 0.25 | **RISK_ON** |
| -0.25 to 0.25 | **NEUTRAL** |
| -0.70 to -0.25 | **RISK_OFF** |
| < -0.70 | **CRISIS** |

### 2.6 Hard Overrides

Before the weighted calculation, two hard conditions bypass the smoothed score entirely:

- If VIX raw regime = "CRISIS" (VIX > ~35): output is **CRISIS** regardless of other signals.
- If credit spread regime = "STRESS" (spread > threshold): output is **RISK_OFF** regardless.

These exist because credit and volatility blowups can precede price moves by hours to days.

### 2.7 Regime Effects on the Rest of the System

The regime label propagates into every downstream decision:

| Regime | Entry allowed | Kelly multiplier | Factor weights |
|--------|--------------|-----------------|----------------|
| RISK_ON | Yes | 1.0× | TREND-heavy |
| NEUTRAL | Yes | 0.8× (market_scale) | Balanced |
| RISK_OFF | Yes | Reduced by `reduce_in_bearish` config | MR-heavy |
| CRISIS | **No** (all entries halted) | — | — |

---

## 3. Universe Construction

**File:** `universe_builder.py`  
**Output:** A list of ~120–150 symbols to score.  
**Filters applied:**

| Filter | Threshold | Rationale |
|--------|-----------|-----------|
| Price range | $5 – $1,000 | Avoid penny stocks and micro-cap distortion |
| Average daily volume | ≥ 500,000 shares | Ensures fill-ability |
| Dollar volume | ≥ $20M/day | Ensures liquidity at $105K scale |
| Daily price range | ≥ 1.0% | Minimum volatility to generate signal |

The universe is rebuilt daily. Held symbols are always appended to the universe even if they fall below filters — we need their data to make exit decisions.

*Known flaw: none of these thresholds have been sensitivity-tested. A parameter sweep across each threshold ±50% has not been performed. The Sharpe-optimal filter frontier is unknown.*

---

## 4. The Signal Engine — Generating Entry Candidates

**File:** `signals.py` → `QuantSignalEngine.generate_signals()`  
**Input:** Bar data for ~120-150 symbols + macro regime  
**Output:** Ranked list of Signal objects with composite scores, stop prices, Kelly fractions

### 4.1 Raw Factor Computation

For each symbol with ≥ 80 bars of daily OHLCV data, 16 raw factors are computed. They are grouped into five clusters:

#### Cluster 1: Mean Reversion (MR)

**RSI Mean Reversion (`rsi_mr`)**  
5-period EMA RSI converted to a mean-reversion signal:  
`rsi_mr = (50 - RSI) / 50`  
RSI=30 (oversold) → rsi_mr = +0.4. RSI=70 (overbought) → rsi_mr = -0.4.  
*Positive = oversold, good for mean-reversion entry.*

**Bollinger Band Z-Score (`bollinger_z`)**  
`bollinger_z = -(price - 20d_MA) / 20d_std`  
Negative sign: price below band = positive score (buying the dip).  
Price 2σ below band → bollinger_z ≈ +2.0 (before cross-sectional normalization).

**Crowd Panic (`crowd_panic`)**  
Measures panic-driven volume on recent down days:  
`panic = sum over last 3 days: if close[i] < close[i-1]: (volume[i] / avg_volume_21d) × |return[i]|`  
High panic = institutional capitulation = potential reversal setup. Raw values ~0.0–0.5.

**MA Distance (`ma_distance`)**  
`ma_distance = -(price - avg(EMA8, EMA21, EMA50)) / avg(EMA8, EMA21, EMA50)`  
Negative sign: price below average MA = positive (mean-reversion opportunity).

**Hurst Exponent (`hurst`)**  
Measures whether the price series is mean-reverting (H < 0.5) or trending (H > 0.5):  
Uses rescaled range (R/S) analysis over lags 2–20. The returned value is transformed:  
`hurst_signal = 0.5 - H`  
H=0.3 → signal = +0.2 (mean-reverting). H=0.7 → signal = -0.2 (trending, bad for MR).

#### Cluster 2: Trend

**MA Stack (`ma_stack`)**  
Two components combined:  
- Order: `(EMA8 > EMA21) + (EMA21 > EMA50) - 1` → -1, 0, or +1  
- Slope: average 5-day return of EMA8, EMA21, EMA50 scaled to ±0.4  
`ma_stack = order × 0.6 + slope × 1.0` then clipped.

**MACD Acceleration (`macd_accel`)**  
Slope of the MACD histogram over the last 5 bars, normalized by price:  
`macd_accel = polyfit_slope(MACD_histogram[-5:]) / current_price`  
Positive slope = momentum building. Negative = momentum dying.

**ADX Direction (`adx_dir`)**  
Standard 14-period ADX signed by trend direction:  
`adx_dir = ADX × (+1 if +DI > -DI else -1)`  
Strong uptrend: ADX=30, +DI>-DI → adx_dir = +30. Raw values ~-50 to +50.

**Price Cloud (`price_cloud`)**  
Price position relative to the midpoint of EMA8 and EMA50, normalized by their spread:  
`price_cloud = (price - (EMA8 + EMA50)/2) / |EMA8 - EMA50|`  
Price above the cloud midpoint = positive. Compressed cloud (EMA8≈EMA50) → signal near 0.

#### Cluster 3: Volume

**Volume Ratio (`vol_ratio`)**  
`vol_ratio = log(today_volume / 21d_avg_volume)`  
Log-normalized to handle the skewed distribution of volume spikes. Positive = above-average volume.

**OBV R² (`obv_r2`)**  
On-Balance Volume slope weighted by R² of the regression:  
`obv_r2 = slope × R²` of linear regression on normalized OBV over 10 days.  
Positive = OBV trending up with high confidence. R² weighting penalizes noisy OBV.

**Accumulation/Distribution (`accum_dist`)**  
Similar to OBV but uses the Close-Location Value (CLV) which weights by where price closed within the day's range:  
`CLV = ((close - low) - (high - close)) / (high - low)`  
`AD = cumsum(CLV × volume)`  
`accum_dist = slope × |R|` of linear regression on normalized AD over 10 days.

#### Cluster 4: Volatility Context

**ATR Percentile (`atr_pctile`)**  
Where today's 14-day ATR sits in its own 60-day distribution:  
`atr_pctile = -(percentile_rank(ATR_today in ATR_60d) / 100 - 0.5) × 2`  
High ATR (extreme volatility) → negative signal. Low ATR → positive.  
*Rationale: low-volatility setups have better risk-adjusted returns.*

**Bollinger Band Squeeze (`bb_squeeze`)**  
Where today's band width sits in its own 60-day distribution:  
`bb_squeeze = -(percentile_rank(BB_width_today in BB_width_60d) / 100 - 0.5) × 2`  
Tight bands = squeeze = positive. Wide bands = negative.

**Relative Strength (`rel_strength`)**  
`rel_strength = (sym_price[-1] / sym_price[-10]) - (SPY_price[-1] / SPY_price[-10])`  
10-day return of the stock minus SPY's 10-day return. Alpha over the market.

#### Cluster 5: Reversal

**Reversal Momentum (`rev_momentum`)**  
`rev_momentum = (close - lowest_low_3d) / ATR_14`  
Measures how far price has bounced off its 3-day low, measured in ATR units.  
rev_momentum > 0.5 → confirms a "reversal" entry type (vs "adaptive").

### 4.2 Cross-Sectional Z-Score Normalization

Raw factor values are not comparable across factors or symbols. All 16 factors are normalized cross-sectionally in a single step:

**For each factor across all symbols in today's universe:**
1. Collect all non-NaN values
2. Compute robust location: **median** (not mean — avoids outlier distortion)
3. Compute robust scale: **MAD** (Median Absolute Deviation) × 1.4826 (this gives the same scale as standard deviation for a normal distribution)
4. Z-score each symbol: `z = clip((raw - median) / (MAD × 1.4826), -3, 3)`

Missing values (NaN) → z-score of 0.0 (neutral, not penalized).

*Why MAD instead of standard deviation: A single extreme value in a factor (e.g., a stock with a massive volume spike) would otherwise inflate the standard deviation and compress everyone else's z-scores toward zero. MAD is immune to outliers.*

### 4.3 Inverse-Volatility Weighting

Before applying regime multipliers, each factor gets a base weight inversely proportional to its cross-sectional dispersion. Factors that are very noisy (high cross-sectional spread) get lower base weight:

```
factor_dispersion[fn] = std(z_scores across all symbols for this factor) + ε
inverse_vol_weight[fn] = 1 / factor_dispersion[fn]
```

Normalized so all inverse-vol weights sum to 1.

### 4.4 Micro-Regime Detection

Each symbol is independently classified into one of three micro-regimes based on its own price behavior:

| Micro-Regime | Condition | Factor Emphasis |
|-------------|-----------|----------------|
| **TRENDING** | Hurst H > 0.55 AND ADX > 25 | Trend factors get 1.5×, MR factors get 0.6× |
| **REVERTING** | Hurst H < 0.45 AND ADX < 20 | MR factors get 1.5×, Trend factors get 0.6× |
| **MIXED** | Everything else | All factors equal weight |

*Hurst H is computed as 0.5 − hurst_signal (reversing the transformation). A stock with H=0.65 is trending; H=0.35 is mean-reverting.*

### 4.5 Weight Blending — Regime × Micro × Adaptive

The final weight for each factor per symbol is the product of three multiplier tables:

```
weight[fn] = inverse_vol_weight[fn] × macro_regime_mult[cluster] × micro_regime_mult[cluster]
```

**Macro regime multipliers by cluster:**

| Regime | MR | Trend | Volume | Volatility | Reversal |
|--------|-----|-------|--------|-----------|---------|
| BULLISH | 0.9 | 1.2 | 1.0 | 0.9 | 0.8 |
| NEUTRAL | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| BEARISH | 1.3 | 0.7 | 1.1 | 1.2 | 1.3 |
| CRISIS | 1.5 | 0.5 | 1.2 | 1.4 | 1.5 |

**Micro regime multipliers by cluster:**

| Micro | MR | Trend | Volume | Volatility | Reversal |
|-------|-----|-------|--------|-----------|---------|
| TRENDING | 0.6 | 1.5 | 1.0 | 0.8 | 0.5 |
| REVERTING | 1.5 | 0.6 | 1.1 | 1.2 | 1.5 |
| MIXED | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |

After multiplication, weights are renormalized to sum to 1.

A third adaptive layer (ridge regression) is then blended in — described in Section 11.

### 4.6 Active Factor Selection

Rather than using all 16 factors, factors with |z-score| < 0.10 are considered uninformative and dropped for this symbol's composite calculation. If fewer than 3 factors pass, all 16 are used.

*Rationale: factors near zero add noise to the composite without adding information. This is equivalent to a soft sparsity constraint.*

### 4.7 Composite Score Calculation

```
composite = sum over active factors: z[fn] × weight[fn] / sum_of_active_weights
```

This is a weighted average of z-scores across active factors. Scores typically range from -2 to +2. Only symbols with `composite > 0` pass to the next stage.

### 4.8 T-Statistic

```
t = composite / (std(all_z_scores_for_this_symbol) + 0.5)
```

The 0.5 is a shrinkage term that prevents symbols with very low factor dispersion from getting artificially high t-statistics. This is used for the leverage qualification check (see Section 5.4) but does not drive sizing directly.

---

## 5. Entry Filters — Pre-Trade Gates

Signals that pass `composite > 0` go through five sequential filters before entry is submitted. Each filter is independent and can block entry without affecting the others.

### 5.1 Already-Held Filter

If the symbol is already in the portfolio, it is blocked immediately. Raptor never adds to a position.

### 5.2 Re-Entry Cooldown (GAP 6) ✅ IMPLEMENTED 2026-05-22

After a `hard_stop` or `trail_loss` exit, the symbol is blocked from re-entry for a cooldown period.

**Current implementation (flat 5-day cooldown):**
```
cooldown_days = 5  (fixed)
Sources checked: outcome_log.json (exit_path field), then position_ledger.json (exit_reason field)
Blocked exit types: hard_stop, trail_loss, trailing_stop
NOT blocked: trail_profit, profit_target (thesis worked — re-entry permitted)
```

**Target design (ATR-scaled cooldown — not yet implemented):**
```
cooldown_days = clip(3 + ATR_percentile × 12, 3, 15)
```
A symbol stopped out during high volatility (ATR in 90th percentile) would get 14 days. Low vol stop-out gets 3 days. This is more principled but requires the ATR at exit time to be recorded in the cooldown log — currently not stored. Implement when `outcome_log.json` is enriched with ATR_percentile at exit.

### 5.3 Composite Velocity Gate (GAP 5) ✅ IMPLEMENTED 2026-05-22

The composite score trajectory is tracked daily in `hold_history.json` (rolling snapshots). Before entry, velocity is computed:

```
velocity = composite_today - composite_3d_ago   (from hold_history.json snapshots)
```

**Current implementation (continuous Kelly modifier):**
```
kelly_modifier = max(0.80, min(1.20, 1.0 + velocity × 0.2))
effective_kelly = kelly_fraction × kelly_modifier
```
- velocity = +0.5 (accelerating) → kelly × 1.10 (size up 10%)
- velocity = -0.5 (decelerating) → kelly × 0.90 (size down 10%)
- Modifier capped at ±20% to bound impact

Requires ≥ 3 days of history in `hold_history.json` to compute. Falls back to 1.0× for new symbols with no prior snapshots. The velocity value is recorded in `position_ledger.json` metadata at entry.

**Target design (hard gate on strong deceleration):**  
The original design called for halving Kelly when `velocity < -0.3`. The continuous modifier is more nuanced — it avoids a binary cliff and degrades smoothly. The flat 5-day cooldown captures the hard-block case for stop-outs; velocity handles the sizing-down case for decelerating-but-not-stopped signals.

### 5.4 Margin Guard ✅ UPDATED 2026-05-22

`margin_guard.py` checks capital utilization before any entries. Rules (in order):

| Condition | Action |
|-----------|--------|
| `equity ≤ 0` | BLOCK all entries |
| `util > 90%` | BLOCK all entries |
| `util > 85%` | REDUCE — cap new positions at 1 |
| `cash < 0` (on margin) | REDUCE — cap new positions at 1 |
| `util > 75%` | WARNING log, proceed normally |
| API error / exception | BLOCK all entries (**fail closed**) |

**Key fix 2026-05-22:** Prior implementation returned `(True, 99)` on any API error — silently allowing unlimited entries if Alpaca was unreachable. Now returns `(False, 0)` — fail closed. On-margin condition now caps entries at 1 rather than just logging a warning.

### 5.5 Entry Agent Veto

The EntryAgent (Ollama/llama3.2) reviews each signal and can record a veto in `entry_vetoes.json`. The veto is advisory — it blocks entry — but the agent's vetoes are tracked against subsequent outcomes to calibrate whether the agent's judgment adds value.

---

## 6. Position Sizing — Kelly Fraction

**All sizing math is in `signals.py`.**

### 6.1 Bayesian Kelly Derivation (Target Design — GAP B)

**⚠ IMPLEMENTATION NOTE:** The Bayesian Kelly derivation below is the *target architecture*. The current `signals.py` uses a simpler formula:
```
base_kelly = rcfg.kelly_fraction × (0.5 + min(|t_statistic| / 3.0, 1.0))
kelly = clip(base_kelly × market_scale, 0.02, 0.12)
```
The 0.02/0.12 caps and `t/3.0` normalization are hand-picked — this is GAP B. The target Bayesian derivation below is not yet implemented but is the correct mathematical framework.

---

**TARGET DESIGN:**

Position size is derived from the Kelly criterion applied to the system's empirical track record. This is computed once per scan from `outcome_log.json`.

**Step 1 — Empirical Kelly:**  
```
f* = μ / σ²
```
Where `μ` = mean return per closed trade and `σ` = standard deviation of returns.

**Step 2 — Bayesian Shrinkage:**  
A Bayesian prior of `f_prior = 5%` is applied with `n_prior = 50` equivalent trades:
```
f_posterior = (N × f* + n_prior × f_prior) / (N + n_prior)
```

*n_prior = 50 is deliberately heavy because most current trades predate the outcome_tracker fix and lack exit path metadata. Once 60+ clean agent-tagged trades exist, n_prior should be reduced to 20.*

**Step 3 — Half-Kelly Discount:**  
```
f_half = f_posterior × 0.5
```

**Step 4 — Drawdown Constraint:**  
```
f_max_DD = max_drawdown_tolerance / (worst_5pct_loss × 2)
```

**Step 5 — Final bounds:**  
```
f_base = min(f_half, f_max_DD, 0.15)
f_min  = f_base × 0.33
```

### 6.2 Conviction Scaling (GAP 2 — Not Yet Implemented)

**Target design:** Kelly scales continuously with composite percentile rank:
```
kelly = f_min + (f_base - f_min) × composite_percentile_rank
```
A signal in the 90th percentile gets full Kelly. A signal just above the entry threshold gets minimum Kelly.

**Current implementation:** Velocity modifier only (GAP 5, implemented 2026-05-22):
```
effective_kelly = kelly_fraction × velocity_modifier   (±20% range)
```
The full conviction-scaled Kelly (GAP 2) is queued but not implemented. When implemented, velocity modifier and conviction scaling will compose: `effective_kelly = conviction_kelly × velocity_modifier`.

### 6.3 Market Regime Scale

The Kelly fraction is multiplied by a market-conditions scalar derived from SPY momentum:

```
roc_20 = (SPY_today / SPY_21_days_ago) - 1
roc_5  = (SPY_today / SPY_6_days_ago) - 1

if roc_20 > +2% and roc_5 > -2%:   market_scale = 1.0  (bull trend)
if roc_20 > +1% and roc_5 < -2%:   market_scale = 0.5  (bull trend breaking)
if -2% ≤ roc_20 ≤ +2%:             market_scale = 0.8  (flat market)
if roc_20 < -2%:                    market_scale = 0.5  (downtrend)
```

### 6.4 Regime Reduction

If the macro regime is BEARISH or RISK_OFF, Kelly is further multiplied by `reduce_in_bearish` (configured value, typically 0.5–0.7).

### 6.5 Leverage Qualification

A signal qualifies for leverage (2×) if all four conditions are met simultaneously:
1. SPY is above its 200-day MA and the 200-day MA itself is rising (5-day slope positive)
2. RSI < 30 (deeply oversold)
3. Bollinger z-score > 2.0 (price > 2σ below band)
4. Price is below the Keltner channel lower band
5. Volume today is ≥ 1.5× the 21-day average

If qualified AND t-statistic ≥ 2.0: `kelly = min(kelly × 2.0, 0.20)`.

### 6.6 Shares and Stop Price

```
shares = floor(equity × kelly / entry_price)
stop_price = entry_price - stop_multiplier × ATR_14

stop_multiplier by micro-regime:
  TRENDING:  initial_stop_atr_mult (config, default ~3.0)
  REVERTING: 2.0
  MIXED:     2.5
```

### 6.7 Hold Target (GAP C — Target Design, Not Yet Implemented)

**⚠ IMPLEMENTATION NOTE:** OU theta-derived hold targets are the target design. Current code:
```
hold = max(1, min(30, int(16 + 14 × atr_pctile)))
```
This conflates volatility (ATR) with mean-reversion speed (OU theta) — high-vol stocks get longer hold targets when they should get shorter ones. This is GAP C.

**Target design (OU theta per stock):**

**OU Theta Estimation:**  
```
theta = -slope of OLS regression: delta_log_price ~ lagged_deviation_from_mean
theta is capped to [log(2)/15, log(2)/2]  (half-life range: 2–15 days)
```

**Hold target:**  
```
base_hold = ceil(log(2) / theta)   (one full OU half-life)
hold_target = base_hold × (2 if TRENDING else 1)
hold_target = clip(hold_target, 3, 30)
```

*Reference: Leung & Zhang (2019) arXiv:1701.03960 — optimal holding period under OU dynamics.*

---

## 7. Entry Execution

**File:** `main.py`

### 7.1 Market Session Gate

Before any scanning, `market_agent.py` classifies the session as SCAN, REDUCE, or STANDBY. If STANDBY, no entries are submitted. This is the first hard gate.

### 7.2 Signal Generation and Filtering

```
signals = generate_signals(bars, macro, sentiment, spy_bars)
→ remove already-held symbols
→ prune cooldown symbols (cooldown_log.json)
→ apply velocity gate (composite_cache.json)
→ apply entry agent vetoes (entry_vetoes.json)
```

### 7.3 Margin Guard

If the margin guard blocks entry, the session exits with a warning log.

### 7.4 Order Submission

For each signal passing all filters:
```
shares = floor(equity × kelly / entry_price)
submit MARKET BUY order to Alpaca
```

On fill confirmation:
- Record in `position_ledger.json`: entry price, date, stop, kelly_fraction, composite
- Record in `ledger.py`: running position book

---

## 8. Hold Monitor — Daily Position Health Scoring

**File:** `hold_monitor.py`  
**Output:** `hold_health.json` (today's scores), `hold_history.json` (full trajectory)  
**Runs:** Twice daily — morning (~9:52 AM) and close (~4:15 PM)

### 8.1 Daily Snapshot

For each held position, a snapshot of ~25 metrics is recorded each day. Key fields:

| Metric | How Computed |
|--------|-------------|
| `composite` | Today's composite score from signal engine |
| `factor_scores` | All 16 factor z-scores |
| `factor_agreement` | Fraction of 16 factors with positive z-score |
| `cluster_scores` | Average z-score per cluster (MR, TREND, VOL, VOLAT, REV) |
| `roc_5d` | 5-day price return |
| `higher_highs` | 1 if HH[−1] > HH[−2] > HH[−3] else 0 |
| `higher_lows` | 1 if HL[−1] > HL[−2] > HL[−3] else 0 |
| `close_pos_5d` | Mean close position within day's range over last 5 days |
| `vol_ratio` | Today's volume / 20-day average volume |
| `obv_slope` | Slope of OBV over last 5 days |
| `ud_ratio` | Up-day volume / down-day volume over 5 days |
| `atr_expansion` | ATR_14 / ATR_10 (expanding or contracting volatility) |
| `stop_dist_atr` | (current_price − stop_price) / ATR |
| `hold_ratio` | days_held / hold_target_days |
| `pnl_pct` | Unrealized P&L percentage |

### 8.2 The Eight Scoring Layers

The health score is a weighted average of eight independent layers. Each layer returns a score in [-1.0, +1.0].

**Weights:**
| Layer | Weight | What It Measures |
|-------|--------|-----------------|
| 1. Composite slope | 23% | Is the fundamental signal strengthening or weakening? |
| 2. Factor agreement (FAR) | 18% | How many of the 16 factors agree with the thesis? |
| 3. Price momentum | 14% | Is the stock actually moving in our direction? |
| 4. Cluster health | 13% | Across 5 factor clusters, where is strength? |
| 5. Volume | 9% | Is institutional money flowing in the right direction? |
| 6. Volatility context | 7% | Is volatility expansion helping or hurting us? |
| 7. Stop distance | 5% | How close is the price to the stop? |
| 8. Hold duration | 2% | Are we overstaying a position that isn't working? |
| 9. Anchored VWAP | 5% | Is price above or below the entry VWAP? ← NEW 2026-05-22 |
| 10. Shannon entropy | 4% | Is price action becoming more or less directional? ← NEW 2026-05-22 |

**Layer 1 — Composite Slope (25% weight):**  
Linear regression slope of composite score over the last 5 daily snapshots:  
`slope` = polyfit coefficient. Score = `clip(slope / 0.10, -1, 1)`.  
A slope of +0.10/day (composite rising by 0.1 per day) = score of +1.0.

**Layer 2 — Factor Agreement / FAR (20% weight):**  
`FAR = factors_positive / 16`  
Base score: `(FAR - 0.5) × 2.0`  
Trend adjustment: `clip(FAR_today - FAR_3d_ago, -0.3, 0.3) × 3.0`  
If FAR < 6/16 (less than 38% of factors agree), this layer scores close to -1.0.  
*Reference: Frazzini & Pedersen (2014) on factor breadth.*

**Layer 3 — Price Momentum (15% weight):**  
Four sub-components combined:  
- ROC(5d): `clip(roc_5d / 10%, -1, 1)` × 0.35  
- Structure: `(higher_highs + higher_lows - 1.0)` × 0.30  
- Close position: `clip((close_pos_5d - 0.5) × 4, -1, 1)` × 0.20  
- ROC trend: `clip(roc_delta_3d / 5%, -1, 1)` × 0.15 (is momentum accelerating?)

**Layer 4 — Cluster Health (15% weight):**  
Cluster scores (average z-score per cluster) normalized and weighted:  
TREND: 35%, MR: 30%, VOL: 15%, VOLAT: 12%, REV: 8%

**Layer 5 — Volume (10% weight):**  
Three sub-components:  
- OBV slope score: `sign(slope) × min(1, |slope| / max(|slope|, 1000))` × 0.35  
- UD ratio: nonlinear mapping (>1.5 = positive, <0.67 = negative) × 0.35  
- OBV trend: linear fit of OBV slopes over 3 days × 0.30

**Layer 6 — Volatility (8% weight):**  
If ATR expanding >20%: score = +0.5 if profitable, -0.8 if losing.  
If ATR contracting <20%: score = 0.0.  
+0.1 bonus if BB width > 10% and we're profitable.

**Layer 7 — Stop Distance (5% weight):**  
`score = clip((stop_dist_atr - 1.5) / 1.5, -1, 1)`  
Stop distance < 1.5 ATR → score < 0 (danger zone). Stop distance > 3.0 ATR → score ≈ +1.

**Layer 8 — Hold Duration (2% weight):**  
Maps hold_ratio (days_held / hold_target) to a score with PnL context:  
`hold_ratio < 0.5`: neutral (0.0)  
`hold_ratio 0.5–0.8`: slight positive if profitable, slight negative if losing  
`hold_ratio 0.8–1.5`: moderate positive if profitable, -0.4 if losing  
`hold_ratio > 1.5`: positive only if profitable AND composite ≥ -0.5; else -0.6

**Layer 9 — Anchored VWAP (5% weight) ✅ IMPLEMENTED 2026-05-22:**  
VWAP anchored to entry date. Measures whether price has traded above or below the volume-weighted average since entry — proxy for institutional accumulation vs distribution.
```
VWAP_anchored = sum(price × vol_ratio) / sum(vol_ratio)  over all held days
score = clip((current_price - VWAP_anchored) / ATR, -1, 1)
```
Price consistently above anchored VWAP → +1.0. Below → -1.0. Approximated from daily snapshots — no new API calls.

**Layer 10 — Shannon Entropy (4% weight) ✅ IMPLEMENTED 2026-05-22:**  
Measures disorder in the distribution of daily returns. Low entropy = price action directional and predictable (thesis clarity). High entropy = chaotic (thesis uncertainty).
```
H = -sum(p × log(p))  over 5-bin histogram of last 10 daily returns
score = clip(1 - 2 × H/log(5), -1, 1)
```
H=0 (perfectly directional) → +1.0. H=log(5) (uniform/random) → -1.0.  
Rising entropy trend over last 3 snapshots adds additional penalty.  
*Reference: Shannon (1948)*

### 8.3 Health Score and Tier

```
health = clip(sum over layers: layer_score × layer_weight, -1, 1)
```

Minimum 3 days of snapshots required for classification:

| Health Score | Tier |
|-------------|------|
| ≥ +0.20 | **STRENGTHENING** |
| -0.15 to +0.20 | **STABLE** |
| < -0.15 | **DECAYING** |
| < 3 snapshots | **INSUFFICIENT_DATA** |

### 8.4 Trim Recommendation — Kelly-Anchored (P1-7)

The trim recommendation is only generated when tier = **DECAYING**.

**Primary path (when entry_kelly is known):**  
The health score is mapped to a fraction of the original Kelly conviction:
```
health_norm = clip((health - (-1.0)) / (TIER_STABLE - (-1.0)), 0, 1)
            = clip((health + 1.0) / 0.85, 0, 1)
current_kelly = entry_kelly × health_norm
trim_pct = 1 - (current_kelly / entry_kelly) = 1 - health_norm
```

*Interpretation:*  
- Health = -0.15 (just crossed into DECAYING): health_norm = 1.0 → trim_pct = 0% (no trim yet, just flagged)  
- Health = -0.50: health_norm = 0.59 → trim_pct = 41%  
- Health = -0.90: health_norm = 0.12 → trim_pct = 88%  
- Health = -1.00: health_norm = 0 → trim_pct = 100% (full exit)

**Fallback path (backfill positions with no entry_kelly):**  
Legacy formula using severity, stop proximity, factor agreement, slope, and PnL multipliers. Will eventually be retired as backfill positions close.

**Action labels (display only, derived from trim_pct):**  
- trim_pct < 25%: TRIM_MINOR  
- trim_pct 25–50%: TRIM_MODERATE  
- trim_pct 50–90%: TRIM_MAJOR  
- trim_pct ≥ 90%: EXIT

### 8.5 Pre-Entry Tracking

Hold monitor also tracks the top 10 signal candidates *before* they enter. This builds a 5-day pre-entry trajectory for each candidate. When the symbol eventually enters, the system already has a baseline to compare against.

Symbols are pruned from pre-entry tracking if: they enter the portfolio, or their last snapshot is > 7 days old.

---

## 9. Exit Monitor — Exit and Trim Decisions

**File:** `exit_monitor.py`  
**Runs:** Morning (~9:40 AM), after hold monitor.

### 9.1 Signal Engine Re-Run

Exit monitor runs the full signal engine again to get *today's* composite scores for all held symbols. It uses `_last_full_signals` which includes all scored symbols, not just the top-N — so a held symbol that has decayed out of the entry candidates still gets its real score rather than a default.

### 9.2 Thesis Invalidation Thresholds (GAP 4) ✅ IMPLEMENTED 2026-05-22

The thesis invalidation threshold scales with macro regime. Two approaches:

**Current implementation (fixed thresholds per regime label):**
```
RISK_ON:  comp < -2.0 AND pnl < -5%
NEUTRAL:  comp < -1.5 AND pnl < -5%   (baseline)
RISK_OFF: comp < -2.0 AND pnl < -5%
CRISIS:   comp < -2.5 AND pnl < -5%
```
Regime read live from `macro_context.json` at exit time. Prevents mass exits during regime-wide drawdowns where the entire cross-section compresses.

**Target design (regime-relative threshold — more principled):**
```
mu_universe  = mean(all composite scores)
sig_universe = std(all composite scores)
thesis_comp_threshold = mu_universe - 1.5 × sig_universe
thesis_pnl_threshold  = -5% (NEUTRAL) or -8% (RISK_OFF/CRISIS)
```
This dynamically adjusts to the daily cross-sectional distribution rather than fixed labels. GAP 4 is implemented at the simpler fixed-label level; the cross-sectional version is the correct long-term target and requires the full composite distribution to be available at exit time (it is — `_last_full_signals` has all scores).

### 9.3 Five Exit Conditions (Evaluated in Order)

Each position is evaluated against all five conditions. The first condition triggered determines the exit reason.

**EXIT 1 — Hard Stop (GAP 3) ✅ IMPLEMENTED 2026-05-22**

```
stop_multiplier = f(ATR_percentile_60d):
  ATR < 25th percentile (low vol):    2.5× ATR
  ATR 25th–75th percentile (normal):  3.0× ATR
  ATR > 75th percentile (high vol):   3.5× ATR

hard_stop = entry_price - stop_multiplier × ATR_14
```

Triggered if: `current_price ≤ hard_stop`

ATR percentile is computed from rolling 60-day ATR history using `bar_data["close"].diff().abs().rolling(14).mean()`. Logger prints `vol_pctile` on every hard stop exit.

*Rationale for wider stops in high vol: In high-volatility regimes, normal intraday noise is larger. A fixed 3× ATR stop would be triggered by noise rather than genuine adverse movement.*

**EXIT 2 — Trailing Stop (GAP 1 ✅ DONE + GAP C ⏳ PENDING)**

**Current implementation (GAP 1 applied, GAP C pending):**

Time and profit-based base multiplier:
```
days_held ≤ early_days:  trail_base = trail_early_atr
days_held ≤ mid_days:    trail_base = trail_mid_atr
days_held ≤ late_days:   trail_base = trail_late_atr
else:                    trail_base = trail_final_atr

profit_tightener:
  profit > 4 ATR: p = 1.0
  profit > 2 ATR: p = 1.5
  profit > 1 ATR: p = 2.0
  else:           p = 99.0  (no tightening)

base = min(trail_base, profit_tightener)
```

Signal-quality modifier (GAP 1):
```
signal_strength = (composite + health) / 2
if signal_strength > +0.3: modifier = 1.3   (wider — let winners run)
if signal_strength < -0.3: modifier = 0.75  (tighter — protect profits)
else:                       modifier = 1.0  (neutral)

final_trail_mult = base × modifier
trail_price = high_water_price - final_trail_mult × ATR
```

Thresholds (0.3/−0.3, 1.3/0.75) are round numbers pending calibration. Run `python calibrate_gap1.py` after backtest finishes to derive data-driven values.

**Target design (GAP C — OU theta derived trail base, not yet implemented):**
```
theta = OU mean-reversion speed (30-day rolling OLS on log-price)
trail_base = clip(1 / sqrt(theta), 1.0, 3.0)
```
Fast-reverting stocks (high theta) get tight trail; trending stocks (low theta) get room. This replaces the fixed ATR step table.

**EXIT 3 — Thesis Invalidation (GAP 4) ✅ IMPLEMENTED 2026-05-22**

Triggered if: `composite < thesis_threshold AND unrealized_pnl < -5%`

Where `thesis_threshold` is regime-scaled (see §9.2). Both conditions must be met simultaneously.

**EXIT 4 — Leveraged ETF Hold Cap**

3× leveraged ETFs: max 3 days held.  
2× leveraged ETFs: max 10 days held.  
Standard: no time-based cap.

*Rationale: Leveraged ETFs suffer volatility decay (daily rebalancing eats into returns) that makes multi-week holds mathematically punishing regardless of direction.*

**EXIT 5 — Time Decay**

Triggered only when all three are true:
1. Position is losing money (pnl < -1%)
2. Held for ≥ 12 days
3. Price is flat (either 20-day return or 5-day return is within ±2%)
4. AND composite score < 0 AND health score < 0

*Rationale: A flat position that is losing money with a deteriorating thesis after 12 days is not going to recover. The market has decided.*

### 9.4 Portfolio Heat Exit

If total portfolio unrealized P&L drops below -`max_portfolio_drawdown` (configured, typically -8%), the single weakest position (by composite score) among those not already flagged for exit is force-liquidated.

*Current implementation: binary "kick the weakest one." Known gap (P1-12): a continuous partial trim proportional to heat magnitude would be more precise.*

### 9.5 Math Trim Execution

After evaluating all five exit conditions, the system reads `hold_health.json`. Any position with a trim recommendation (TRIM_MINOR/MODERATE/MAJOR/EXIT from the hold monitor) is added to the exit queue as a partial or full trim.

The trim is capped to `min(trim_shares, total_shares - 1)` — we never trim 100% of a position this way unless hold_monitor recommended EXIT explicitly.

### 9.6 Post-Exit Recording

After each successful sell order:
1. **Ledger update:** `ledger.py` records exit price, date, and reason.
2. **Outcome pending:** `outcome_pending.json` is updated with a sidecar keyed by Alpaca order ID, containing: symbol, exit_reason, composite, agent decisions.
3. **Cooldown:** If exit reason is `hard_stop` or `thesis_invalid`, symbol is added to `cooldown_log.json` with a duration scaled by ATR percentile.

---

## 10. The Feedback Loop — Learning from Outcomes

**Files:** `outcome_tracker.py`, `outcome_log.json`, `outcome_pending.json`

### 10.1 How Outcome Data Flows

```
exit_monitor.py  →  outcome_pending.json  (keyed by Alpaca order ID)
         ↓
outcome_tracker.py  reads outcome_pending.json when trade settles
         ↓
builds complete record: entry metadata + exit metadata + realized P&L
         ↓
outcome_log.json  (permanent record, 85 trades currently)
```

### 10.2 What Each Outcome Record Contains

| Field | Source |
|-------|--------|
| `symbol` | Trade |
| `entry_date`, `exit_date` | Timestamps |
| `entry_price`, `exit_price` | Alpaca fills |
| `actual_pnl_pct` | Realized return |
| `actual_exit_path` | exit_monitor reason |
| `entry_decision`, `entry_confidence` | EntryAgent decision at time of entry |
| `hold_decision`, `hold_confidence` | HoldAgent last decision before exit |
| `composite_at_entry` | composite score when entered |

### 10.3 How Outcomes Feed Back

Outcome records drive four downstream systems:

**1. Bayesian Kelly (Section 6.1):** `_bayesian_kelly()` reads `outcome_log.json` on every scan to update `f*` from realized returns.

**2. Adaptive Weights (Section 11):** `AdaptiveWeights.record_trade()` is called after each outcome, feeding factor z-scores and realized return into the Ridge regression and IC calibrator.

**3. Agent Calibration (planned):** Once 30+ agent-tagged trades exist, `prompt_calibrator.py` (not yet built) will analyze whether agent decisions correlate with outcome quality.

**4. Cooldown Logic:** Hard stops and thesis invalidations trigger cooldown regardless of outcome sign.

---

## 11. Adaptive Weight System

**File:** `signals.py` → `AdaptiveWeights`  
**Storage:** `adaptive_weights.json`

### 11.1 Two Learning Layers

**Layer 1 — Ridge Regression (activates at 30+ trades)**

After each trade closes, the 16 factor z-scores at entry and the realized return are recorded. When 30+ trades are available, a Ridge regression is fit:

```
y = X @ beta + epsilon
beta = solve(X.T @ X + λI, X.T @ y)   (λ = 1.0, Ridge regularization)
```

Ridge regularization prevents overfitting by shrinking coefficients toward zero. The resulting `beta` represents how much each factor's z-score at entry predicted the return.

The Ridge weights are blended with the base weights at a rate that grows with trade count:
```
alpha = min(30%, 30% × (N - 30) / 60)
final_weight = (1 - alpha) × base_weight + alpha × ridge_weight
```

At 30 trades: alpha = 0% (pure base). At 90 trades: alpha = 30% (30% Ridge influence).

**Layer 2 — Information Coefficient (IC) Boost (activates at 20+ trades)**

For the last 50 trades, the IC of each factor is estimated as:
```
IC[fn] = fraction of trades where sign(z_score[fn]) == sign(realized_return) - 0.5
```

IC = 0.0 means random. IC = +0.1 means the factor's direction predicted returns 60% of the time.

Weights are then adjusted: `weight[fn] × (1 + IC[fn])`

This is computed once per scan (not per symbol) and cached.

*Known flaw: The IC calibrator uses last 50 trades without decay weighting. Older trades may have been generated under different market regimes and could dilute the IC signal.*

---

## 12. Agent Layer — LLM Advisory

**File:** `agent_layer.py`  
**Model:** Ollama/llama3.2 (local, private)

### 12.1 Three Agent Roles

| Agent | When runs | Decision recorded in |
|-------|-----------|---------------------|
| **MarketAgent** | Pre-market | `market_decision.json` |
| **EntryAgent** | Per signal, pre-entry | `entry_vetoes.json` |
| **HoldAgent** | Daily per position | `hold_decisions.json` |

### 12.2 What Agents Do and Don't Do

**Do:** Provide reasoning in natural language. Record their decisions for calibration. Can veto entries (EntryAgent).

**Don't:** Execute trades. Override math exits. Set stop prices. Choose position sizes.

The agents observe the same data as the math system — composite scores, macro regime, factor details, health scores, PnL — and produce HOLD/TRIM/EXIT decisions with confidence scores and reasoning.

The gap between agent decision and math decision is logged. Over time, this gap should narrow — either the agents learn (through prompt calibration) or the math catches the patterns the agents see.

### 12.3 Calibration Plan

Once 30+ agent-tagged trades exist, a `prompt_calibrator.py` module (not yet built) will:
1. Compute correlation between agent confidence and outcome quality
2. Identify which reasoning patterns correlate with wins vs losses
3. Update the agent prompts accordingly

*Current status: 0 agent-tagged trades exist because P0-1 (outcome_pending sidecar) was only fixed on 2026-05-20. All prior trades have `entry_decision=None`.*

---

## 13. Daily Schedule and File Topology

### 13.1 Daily Schedule

| Time | Script | Action |
|------|--------|--------|
| 9:00 AM | `macro_context.py` | Fetch macro data (FRED + yfinance), classify regime, update `macro_context.json` |
| 9:15 AM | `market_agent.py` | MarketAgent classifies session: SCAN/REDUCE/STANDBY |
| 9:35 AM | `main.py` | Generate signals, apply cooldown/velocity/margin/agent filters, submit entry orders |
| 9:52 AM | `exit_monitor.py` | Morning mechanical exits + math trims |
| 9:52 AM | `hold_monitor.py` | Morning health scores, trim recommendations |
| 3:50 PM | `exit_monitor.py` | Afternoon exits + trims |
| 3:50 PM | `hold_monitor.py` | Afternoon health re-score |
| 3:50 PM | `daily_recap.py` | Recap email via afternoon monitor |
| 4:30 PM | `daily_recap.py` | Standalone recap email at closing prices |
| Continuous | `watchdog.py` | Intraday stop monitoring (⚠ currently uses daily bars — known gap P1-9) |

### 13.2 Key Files and What They Contain

| File | Contents | Written by | Read by |
|------|----------|-----------|---------|
| `macro_context.json` | Regime label, signal scores, breadth (50/150/200MA), agent summary | `macro_context.py` | `market_agent.py`, `agent_layer.py`, `exit_monitor.py` |
| `market_decision.json` | SCAN/REDUCE/STANDBY + reasoning | `market_agent.py` | `main.py` |
| `hold_history.json` | Full daily snapshot trajectory including composite | `hold_monitor.py` | `hold_monitor.py`, `main.py` (velocity gate) |
| `position_ledger.json` | Entry metadata: price, date, stop, kelly, composite, composite_velocity | `main.py`, `ledger.py`, `exit_monitor.py` | `hold_monitor.py`, `exit_monitor.py`, `outcome_tracker.py` |
| `hold_health.json` | Today's health scores + stop_dist_atr | `hold_monitor.py` | `exit_monitor.py`, `daily_recap.py` |
| `hold_decisions.json` | HoldAgent decisions (advisory) | `agent_layer.py` | `exit_monitor.py` (log only) |
| `entry_vetoes.json` | EntryAgent vetoes | `agent_layer.py` | `main.py` |
| `outcome_log.json` | Complete closed trade records + exit_path | `outcome_tracker.py` | `main.py` (cooldown check), `AdaptiveWeights` |
| `trim_log.json` | Partial trims: reason, composite, agent decision, trim_detail | `exit_monitor.py` | `daily_recap.py` |
| `adaptive_weights.json` | Ridge beta + IC cache + trade history | `AdaptiveWeights` | `AdaptiveWeights` |

---

## 14. Known Gaps and Open Problems

This section documents every known flaw in the system's logic as of 2026-05-22. Status: ✅ Done | ⏳ In Progress | 📋 Queued | 🔴 Blocked.

### 14.1 Closed Gaps (Full Session 2026-05-22)

| Gap | Fix Applied |
|-----|------------|
| ✅ GAP 1 | Signal-aware trailing stop — calibrated 0.2/1.6/0.80 from 1565-trade sweep |
| ✅ GAP 2 | Conviction-scaled Kelly — 40% t-stat + 60% percentile rank, range [1.71%, 5.17%] |
| ✅ GAP 3 | Vol-regime hard stop — 2.5/3.0/3.5× ATR by 60d percentile |
| ✅ GAP 4 | Regime-scaled thesis invalidation — RISK_ON/OFF→-2.0, NEUTRAL→-1.5, CRISIS→-2.5 |
| ✅ GAP 5 | Composite velocity sizing — ±20% Kelly modifier from hold_history.json |
| ✅ GAP 6 | Re-entry cooldown — 5-day block for hard_stop/trail_loss |
| ✅ GAP 7 | Portfolio heat trim-all — proportional trim of all health<0, scaled by excess DD |
| ✅ GAP 9 | Afternoon composite rescore — fresh composites written to hold_health.json at 3:50PM |
| ✅ GAP B | Kelly caps from Thorp 2006 — 0.0171/0.0517 from backtest drawdown analysis |
| ✅ GAP C | OU theta hold target — per-stock 30d OLS, replaces ATR-pctile formula |
| ✅ GAP D | calibrate_gap1.py — parameter sweep tool ready, run after backtest |
| ✅ GAP E | Backtest composite proxy — enables GAP 1 validation |
| ✅ GAP F | Universe filter sweep — vol 500K→750K, dollar vol $20M→$30M |
| ✅ GAP G | Sector breadth 50/150/200MA — Zweig 1986, structural bull/bear field |
| ✅ GAP H | margin_guard 4 bugs — fail-closed, on-margin reduce, sentinel, dead code |
| ✅ P1-9 | Watchdog live SPY price + hold_health high_water + remove daily EMA |
| ✅ P1-15 | Sentiment pipeline removed — consumed API calls, zero alpha |
| ✅ P2-7 | OBV rolling normalization — self-calibrating, no magic constant |
| ✅ P2-8 | Volatility layer continuous — linear interpolation replaces binary 0.2 |
| ✅ P2-9 | Stop dist zero = -1.0 — not 0.0 neutral |
| ✅ Layer 9 | Anchored VWAP distance (5% weight) added to hold monitor |
| ✅ Layer 10 | Shannon entropy trend (4% weight) added to hold monitor |
| ✅ log(0) | vol_ratio, hurst, OU theta all guarded against zero/negative inputs |
| ✅ P2-12 | Prompt snapshot lazy — no longer runs filesystem glob on every import |
| ✅ GAP 3 — Hard stop fixed regardless of vol regime | Vol-regime ATR multiplier (2.5/3.0/3.5×) based on 60-day ATR percentile |
| ✅ GAP 4 — Thesis invalidation threshold static | Regime-scaled: RISK_ON/OFF→-2.0, NEUTRAL→-1.5, CRISIS→-2.5 |
| ✅ GAP 5 — No momentum acceleration on entry | Composite velocity from hold_history.json. Kelly modifier ±20%. |
| ✅ GAP 6 — No re-entry cooldown after stop-out | 5-day block for hard_stop/trail_loss. trail_profit not blocked. |
| ✅ GAP G — Sector breadth only 50MA | 50/150/200MA per Zweig (1986). Structural field from 200MA adds extra vote. |
| ✅ GAP H — margin_guard.py unanalyzed | 4 bugs fixed: fail-closed, on-margin reduce, dead code, sentinel |
| ✅ GAP E — Backtest couldn't validate GAP 1 | composite_proxy + health_proxy in backtest. GAP 1 validation section in report. |
| ✅ GAP 1 — Trailing stop signal-blind | signal_strength modifier in _trail_mult(). Done 2026-05-17. |

### 14.2 In Progress

**⏳ GAP D — Trail modifier thresholds are round numbers**  
0.3/−0.3 and 1.3/0.75 are hand-picked. `calibrate_gap1.py` sweeps 125 combinations and derives optimal values from backtest trade population. Run after backtest finishes.

**⏳ Backtest running** — GAP 1 + composite/health proxy. Results pending.

### 14.3 Queued — Next Session

**📋 GAP B — Kelly caps unjustified (Thorp 2006)**  
`0.02/0.12` caps and `t/3.0` normalization in `signals.py` are hand-picked. Derive from backtest drawdown analysis. `f_max = 1 - sqrt(1 - 2×target_return/variance)`.

**📋 GAP C — Hold target conflates volatility with OU speed (Leung & Zhang 2019)**  
`hold = 16 + 14×atr_pctile` is wrong — high-vol stocks get longer holds when they should get shorter. Fix: OU theta per stock from 30-day rolling OLS. `hold_target = ceil(log(2)/theta) × (2 if TRENDING)`.

**📋 GAP F — Universe filters never sensitivity-tested**  
Price $5-$1000, volume ≥500K, dollar volume ≥$20M, daily range ≥1% — all hand-picked. Parameter sweep ±50% on each threshold required.

**📋 GAP 2 — Entry sizing conviction gradient**  
Current: velocity modifier only (±20%). Target: Kelly scales with composite percentile rank. `kelly = f_min + (f_base - f_min) × percentile_rank`. Composes with velocity modifier when implemented.

**📋 GAP 7 — Portfolio heat exit is binary**  
Single weakest position fully exited when portfolio_dd < threshold. Should: trim ALL positions with health < 0 by a size proportional to heat magnitude.

### 14.4 Gated — Needs Data

**🔴 GAP A — Macro regime classifier uses vote-count (Hamilton 1989)**  
Continuous weighted score + Kalman smoothing + hysteresis (see §2.3–2.5) is the target. Do not implement until vote-count stability has been profiled. HMM overfits. Kalman is correct direction but requires regime validation data first.

**🔴 Hold monitor layer weight calibration (needs 60+ agent-tagged trades)**  
8 layer weights (25/20/15/15/10/8/5/2%) are hand-picked. Fix: Spearman IC of each layer score against realized PnL over rolling 60-day window. Requires 60+ clean outcome records. Currently ~0 clean records (pre-fix trades lack exit_path).

### 14.5 Infrastructure Gaps

**P1-9 — Watchdog uses daily bars, not intraday**  
`watchdog.py` fetches 5 daily bars and computes EMA8 — this is a 5-day EMA, not intraday monitoring. Genuine intraday stop monitoring requires 15-minute bars from Alpaca. `intraday_return < -3σ_daily_vol` is the correct trigger (not -3% absolute).

**P1-15 — Sentiment pipeline produces zero alpha**  
Lexicon sentiment is computed from news headlines (26 positive + 26 negative words) and injected into agent prompts. The `sentiment_score` field is 0.0 on every Signal object. API calls and parse time with no contribution. Either fix or remove.

**P1-16 — No afternoon rescore of held positions**  
Signal engine runs once at 9:35 AM. Afternoon monitor at 3:50 PM re-runs exit/health but not signals. A lightweight rescore of held symbols + top 30 candidates would identify intraday deterioration.

**P2-7 — OBV normalization magic constant**  
OBV slope normalized by `max(|slope|, 1000)`. The 1000 floor is uncalibrated.

**P2-8 — Volatility layer binary in normal range**  
ATR expansion 80%–120% returns exactly 0.0. Loses information in the normal range. Should be a continuous function.

**P2-9 — Stop distance returns neutral at zero**  
Zero stop distance (price at stop) should be maximum negative signal, not 0.0 (neutral).

---

*This document describes Raptor v5.4 as of 2026-05-22. The authoritative implementation is in the GitHub repository: github.com/stevefirwin-svg/Raptor.*  
*Sections marked ⚠ describe the intended architecture. Sections marked ✅ are implemented. Sections with no marker match the code exactly.*
