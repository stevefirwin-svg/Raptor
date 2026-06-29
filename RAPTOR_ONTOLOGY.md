# Raptor v5.4 — Complete System Ontology
*Full decision logic, mathematics, and feedback loops. No code.*
*Last updated: 2026-06-19 | Reflects all session 4–8 changes*

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

Raptor is a quantitative swing trading system running on a single Alpaca paper account (~$107K equity). It holds between 0 and 10 positions at a time, with a typical hold duration of 5–20 trading days. It is designed for US equities only.

**Installation:** `C:\Raptor` (moved from OneDrive 2026-06-19 to eliminate sync conflict risk — see §18).

### 1.1 Core Design Principle

Every decision in Raptor is made by math first. An LLM agent (Ollama/llama3.2, running locally) observes the same decisions and provides reasoning in natural language, but the agent never executes a trade. The agent's output is recorded for calibration — to check whether its reasoning correlates with actual outcomes over time.

### 1.2 Five Stages of Every Trade

```
STAGE 1: MACRO GATE       — Is it safe to enter anything today?
STAGE 2: UNIVERSE SCAN    — Which ~120-150 symbols to score?
STAGE 3: SIGNAL ENGINE    — Which symbols have genuine edge (composite score > 0)?
STAGE 4: ENTRY FILTERS    — Pre-trade gates: margin, cooldown, velocity, agent veto
STAGE 5: EXECUTION        — Size, submit, record
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
Three moving averages computed per sector ETF (50/150/200 day). Composite:  
`breadth_composite = 0.25 × pct_above_50MA + 0.35 × pct_above_150MA + 0.40 × pct_above_200MA`  
Then: `score = clip((composite - 50) / 50, -1, 1)`  
50% of sectors above their weighted-average MA = neutral. 100% = +1.0. 0% = -1.0.  
*Longer MAs get higher weight because they are more predictive of sustained regime (Zweig 1986).*

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

### 5.2 Re-Entry Cooldown (P1-11)

After a `hard_stop` or `thesis_invalid` exit, the symbol is placed in a cooldown period. The cooldown duration scales with market conditions:

```
cooldown_days = clip(3 + ATR_percentile × 12, 3, 15)
```

`ATR_percentile` is where the symbol's ATR sat in its 60-day distribution at the time of the stop-out. A symbol stopped out during high volatility (ATR in 90th percentile) gets a 14-day cooldown. A symbol stopped out in low volatility gets a 3-day cooldown.

*Rationale: A stop-out during high volatility means the thesis was rejected by a volatile, potentially irrational market. The same signal re-appearing immediately is likely to be stopped again.*

### 5.3 Composite Velocity Gate (P1-10)

The composite score is tracked daily for every symbol in `composite_cache.json` (rolling 5-day window). Before entry, the velocity is computed:

```
velocity = composite_today - composite_3d_ago
```

If `velocity < -0.3` (score accelerating downward), the Kelly fraction for this signal is halved. The position still enters but at reduced size. The gate requires ≥ 3 days of cache history to activate.

*Rationale: Two stocks with the same composite score today are not equivalent — one accelerating from 0.5 last week represents a strengthening thesis; one decelerating from 2.5 represents a weakening one.*

### 5.4 Margin Guard

Before any entries, `margin_guard.py` checks whether the account has sufficient buying power and whether the current capital utilization is acceptable. If the account is too concentrated or approaching margin limits, all new entries are blocked for the session.

### 5.5 Entry Agent Veto

The EntryAgent (Ollama/llama3.2) reviews each signal and can record a veto in `entry_vetoes.json`. The veto is advisory — it blocks entry — but the agent's vetoes are tracked against subsequent outcomes to calibrate whether the agent's judgment adds value.

---

## 6. Position Sizing — Kelly Fraction

**All sizing math is in `signals.py`.**

### 6.1 Bayesian Kelly Derivation (P1-4)

Position size is derived from the Kelly criterion applied to the system's empirical track record. This is computed once per scan from `outcome_log.json`.

**Step 1 — Empirical Kelly:**  
```
f* = μ / σ²
```
Where `μ` = mean return per closed trade and `σ` = standard deviation of returns.  
With 79 current trades: μ ≈ 0.235%, σ ≈ 8.9% → f* ≈ 29.6%.

**Step 2 — Bayesian Shrinkage:**  
Full Kelly (29.6%) is dangerously aggressive when estimated from limited data. A Bayesian prior of `f_prior = 5%` is applied with `n_prior = 50` equivalent trades:
```
f_posterior = (N × f* + n_prior × f_prior) / (N + n_prior)
```
With N=79: f_posterior ≈ 20.1%.

*n_prior = 50 is deliberately heavy because all 79 current trades are from a pre-P0-fix period and lack agent tagging. Once 60+ clean agent-tagged trades exist, n_prior should be reduced to 20.*

**Step 3 — Half-Kelly Discount:**  
Standard practice under estimation uncertainty. The Kelly criterion assumes precise knowledge of edge; we don't have that.
```
f_half = f_posterior × 0.5 ≈ 10.0%
```

**Step 4 — Drawdown Constraint:**  
```
f_max_DD = max_drawdown_tolerance / (worst_5pct_loss × 2)
f_max_DD = 12% / (10.1% × 2) ≈ 59%   (non-binding currently)
```

**Step 5 — Final bounds:**  
```
f_base = min(f_half, f_max_DD, 0.15) = 10.0%
f_min  = f_base × 0.33 = 3.3%
```

### 6.2 Conviction Scaling

Kelly scales continuously with composite percentile rank:
```
kelly = f_min + (f_base - f_min) × composite_percentile_rank
```

A signal in the 50th percentile of today's composite distribution gets 6.7% Kelly.  
A signal in the 90th percentile gets 9.0% Kelly.  
A signal at the 0th percentile (just above threshold) gets 3.3% Kelly.

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

### 6.7 Hold Target (P1-5)

The intended hold duration is derived from the stock's own mean-reversion speed, not a fixed number of days:

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

*Interpretation: a stock with a 5-day half-life gets a 5-day base hold target. If it's in a trending micro-regime, we give it 10 days (let trends run).*

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
| 1. Composite slope | 25% | Is the fundamental signal strengthening or weakening? |
| 2. Factor agreement (FAR) | 20% | How many of the 16 factors agree with the thesis? |
| 3. Price momentum | 15% | Is the stock actually moving in our direction? |
| 4. Cluster health | 15% | Across 5 factor clusters, where is strength? |
| 5. Volume | 10% | Is institutional money flowing in the right direction? |
| 6. Volatility context | 8% | Is volatility expansion helping or hurting us? |
| 7. Stop distance | 5% | How close is the price to the stop? |
| 8. Hold duration | 2% | Are we overstaying a position that isn't working? |

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

### 9.2 Regime-Relative Thesis Thresholds (P1-8)

Before evaluating any positions, the cross-sectional composite distribution is computed across all scored symbols:

```
mu_universe  = mean(all composite scores)
sig_universe = std(all composite scores)
thesis_comp_threshold = mu_universe - 1.5 × sig_universe
thesis_pnl_threshold  = -5% (NEUTRAL) or -8% (RISK_OFF/CRISIS)
```

*Why relative thresholds: An absolute threshold of -1.5 is too aggressive in RISK_OFF markets where the entire universe compresses down. In a normal market, composite=-1.5 is far below average; in a RISK_OFF market, it might be average. The relative threshold always means "1.5σ below today's cross-section."*

### 9.3 Five Exit Conditions (Evaluated in Order)

Each position is evaluated against all five conditions. The first condition triggered determines the exit reason.

**EXIT 1 — Hard Stop (P1-2: Vol-Regime Aware)**

```
stop_multiplier = f(ATR_percentile_60d):
  ATR < 25th percentile (low vol):  2.5× ATR
  ATR 25th–75th percentile (normal): 3.0× ATR
  ATR > 75th percentile (high vol):  3.5× ATR

hard_stop = entry_price - stop_multiplier × ATR_14
```

Triggered if: `current_price ≤ hard_stop`

*Rationale for wider stops in high vol: In high-volatility regimes, normal intraday noise is larger. A fixed 3× ATR stop would be triggered by noise rather than genuine adverse movement. Wider stops in high-vol reduce whipsaw while maintaining equivalent statistical protection.*

**EXIT 2 — Trailing Stop (time/profit-tiered — see DISCREPANCY note in §16)**

The trail width is derived from time held and unrealized profit (NOT from θ — see §16
for a documentation-accuracy correction; this section previously described an
OU-theta-derived trail that was never implemented):

```
trail_base = time-tier ATR multiple, by days_held (early/mid/late/final tiers — config.py)
profit_floor = 2.5x if profit_atr>=4, 2.0x if >=2, 2.5x if >=1, else uncapped (99.0)
trail = min(trail_base, profit_floor)
```

Then modified for profit state:
```
profit_tightener:
  unrealized profit > 4 ATR: trail = min(trail_base, 1.0)  (very tight)
  unrealized profit > 2 ATR: trail = min(trail_base, 1.5)
  unrealized profit > 1 ATR: trail = min(trail_base, 2.0)
  no profit yet:             trail = trail_base             (no tightening)
```

Then a signal-quality modifier:
```
signal_strength = (composite + health) / 2   (combined conviction)
if signal_strength ≥ 0: modifier = 1.0 + 0.3 × signal_strength  (wider trail for strong signals)
if signal_strength < 0: modifier = 1.0 + 0.25 × signal_strength (narrower trail for weak signals)

final_trail_mult = min(trail_base, profit_tightener) × modifier
trail_price = high_water_price - final_trail_mult × ATR
```

Triggered if: `current_price ≤ trail_price AND trail_price > hard_stop`

**EXIT 3 — Thesis Invalidation (P1-8: Regime-Relative)**

Triggered if: `composite < thesis_comp_threshold AND unrealized_pnl < thesis_pnl_threshold`

Both conditions must be met simultaneously. Composite weakness alone doesn't trigger exit (the position might just be temporarily out of favor). A loss alone doesn't trigger exit (drawdowns are part of a position's life). Both together = the market has rejected the thesis AND we are losing money.

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
| 8:00 AM | `macro_context.py` | Fetch macro data, classify regime, update `macro_context.json` |
| 8:30 AM | `market_agent.py` | MarketAgent classifies session: SCAN/REDUCE/STANDBY |
| 9:35 AM | `main.py` | Generate signals, apply filters, submit entry orders |
| 9:52 AM | `hold_monitor.py` | Morning health scores, trim recommendations |
| 9:55 AM | `exit_monitor.py` | Evaluate exit conditions, submit exits/trims |
| 4:00 PM | `hold_monitor.py` | Afternoon health re-score |
| 4:15 PM | `daily_recap.py` | Performance summary email |
| Continuous | `watchdog.py` | Intraday stop monitoring (currently using daily bars — known gap) |

### 13.2 Key Files and What They Contain

| File | Contents | Written by | Read by |
|------|----------|-----------|---------|
| `macro_context.json` | Regime, Kalman state, signal scores | `macro_context.py` | `main.py`, `exit_monitor.py` |
| `market_decision.json` | SCAN/REDUCE/STANDBY + reasoning | `market_agent.py` | `main.py` |
| `composite_cache.json` | Daily composite scores, 5-day rolling | `main.py` | `main.py` (velocity gate) |
| `cooldown_log.json` | Symbol → cooldown expiry date | `exit_monitor.py`, `watchdog.py` | `main.py` |
| `position_ledger.json` | Entry metadata per position | `main.py`, `ledger.py` | `hold_monitor.py`, `exit_monitor.py` |
| `hold_history.json` | Full daily snapshot trajectory | `hold_monitor.py` | `hold_monitor.py` |
| `hold_health.json` | Today's health scores + trim recs | `hold_monitor.py` | `exit_monitor.py` |
| `hold_decisions.json` | HoldAgent decisions (advisory) | `agent_layer.py` | `exit_monitor.py` (log only) |
| `entry_vetoes.json` | EntryAgent vetoes | `agent_layer.py` | `main.py` |
| `outcome_pending.json` | Exit sidecar keyed by order ID | `exit_monitor.py` | `outcome_tracker.py` |
| `outcome_log.json` | Complete closed trade records | `outcome_tracker.py` | `_bayesian_kelly()`, `AdaptiveWeights` |
| `adaptive_weights.json` | Ridge beta + IC cache + trade history | `AdaptiveWeights` | `AdaptiveWeights` |

---

## 14. Known Gaps and Open Problems

This section documents every known flaw in the system's logic as of 2026-05-22. They are listed in priority order.

### 14.1 Gated (cannot build until more data)

**P1-6 — Static layer weights in hold_monitor (GATED: need 60+ agent-tagged trades)**  
The 8 health layer weights (25%, 20%, 15%, etc.) are hand-picked with no calibration. The correct weights would be derived from which layers best predicted subsequent trade outcomes.  
Fix: Information Coefficient calibration of each layer's score against realized PnL. Gate: 60+ tagged trades needed for statistical significance.

### 14.2 Infrastructure gaps

**P1-9 — Watchdog uses daily bars claiming intraday monitoring**  
The watchdog is described as an intraday stop checker, but it fetches 5 daily bars and computes an EMA8 on them. This is a 5-day EMA, not intraday monitoring. A genuine intraday stop would require minute or 15-minute bar data.  
Fix: Use Alpaca's bar endpoint for 15-minute data, compute intraday realized volatility = `std(5-min returns) × sqrt(78)`. SPY circuit breaker should trigger on `intraday_return < -3σ_daily_vol`, not `-3%` absolute.

**P2-12 — Agent prompt versioning runs on every import**  
Agent layer checks and writes prompt version files on every module import. This is slow and creates unnecessary I/O.

### 14.3 Math precision gaps

**P1-12 — Portfolio heat exit is binary**  
When total portfolio drawdown exceeds the threshold, the single weakest position is fully exited. A more precise approach: partial trim proportional to heat magnitude, targeting a specific drawdown level.

**P1-14 — Universe filters are untested**  
Price range $5-$1000, volume ≥ 500K, dollar volume ≥ $20M, daily range ≥ 1% — all hand-picked with no sensitivity analysis. A 10% change in any threshold might significantly alter the opportunity set.

**P1-15 — Sentiment is computed but unused — FIXED 2026-06-29**  
A lexicon-based sentiment score is computed from news headlines (26 positive + 26 negative words). It was injected into agent prompts but the `sentiment_score` field was 0.0 on every Signal object, since the sentiment pipeline was disabled 2026-05-22. Fix: removed the dead `sentiment_score` field from the `Signal` dataclass entirely (confirmed via grep: no downstream consumer ever read it). Not one of the 16 protected factors, so removal does not conflict with "do not modify factors."

**P1-17 — `vol_ratio` factor has a statistically significant negative IC — MITIGATED 2026-06-29**  
`factor_ic_report.json` (generated 2026-06-26) shows `vol_ratio` IC = -0.1692, t=-3.11, n=331 — i.e. the factor's signal is reliably backwards. Caveat: `n_outcome=0` for every factor in that report, meaning this rests entirely on the noisier `hold_history.json` mid-hold snapshots, not yet confirmed against a single realized closed-trade outcome. Steve's decision: do not remove the factor (preserves the 16-factor "do not modify factors" structure / 208% backtest shape) — instead halve its weight in `signals.py::generate_signals` and redistribute the freed share proportionally to the 5 current top-IC factors (`accum_dist`, `adx_dir`, `rel_strength`, `price_cloud`, `rev_momentum`). Re-evaluate once `n_outcome` evidence accumulates.

**P0-9 — FRED API key logged in plaintext — FIXED 2026-06-29**  
`data_feeds.py::FREDDataFeed._fetch_series` logged `str(e)` directly on any failed request. `requests`' `HTTPError`/`Timeout`/`ConnectionError` embed the full request URL — including the plaintext `api_key` query param — in their string representation, so every failed FRED fetch wrote the live API key to `logs/`. Fix: added `_redact_api_key()`, called before the `logger.error` in the exception handler.

**P2-10 — EXIT5 thesis check had no `hold_health.json` freshness check — FIXED 2026-06-29**  
See §11 / `exit_monitor.py`. `hold_monitor.py` runs only at 9:28 AM and 3:50 PM; `exit_monitor.py`'s 30-minute loop reads whatever snapshot is currently on disk with no staleness check. A crashed or skipped `hold_monitor` run left EXIT5 silently judging "thesis dead" off hours-old composite/health numbers. Fix: per-symbol staleness check against each record's `timestamp` field (not dated today → stale); stale symbols skip the EXIT5 deterioration check and default to hold.

**P2-11 — Missing-stop ATR display indistinguishable from "at the stop" — FIXED 2026-06-29**  
`hold_monitor.py`'s log line and recap HTML table both rendered `stop_dist_atr is None` (no real stop in ledger metadata) as `0.00 ATR`, identical to `stop_dist_atr == 0.0` (price genuinely at/through the stop — the dangerous case the layer-7 scorer treats as worst-case). Fix: both display sites now render `—` when there's no real stop.

**P1-16 — No afternoon rescore of held positions**  
The signal engine runs once at 9:35 AM. Composite scores do not update during the day. A 3:50 PM rescore of held positions + top 30 candidates would identify positions whose thesis has deteriorated intraday.

**P2-7 — OBV slope magic constant — FIXED (verified in code 2026-06-28)**  
The hardcoded `max(|slope|, 1000)` floor was replaced with normalization by the rolling std of OBV slopes across that position's own snapshots (self-relative, no cross-sectional bias toward high-volume names). See `hold_monitor.py::_score_volume`.

**P2-8 — Volatility layer returns flat 0.2 for ATR expansion 0.80–1.20 (corrected 2026-06-28)**  
Previously documented as returning exactly 0.0 in this range — that was wrong. Current code (`hold_monitor.py::_score_volatility`) returns 0.0 only when ATR is *contracting* (<0.80) and a flat 0.2 for the normal 0.80–1.20 band. Still not continuous and still loses information within that band, so the underlying gap is real, just not as previously described.

**P2-9 — Stop distance layer returns 0 if dist is exactly 0 — FIXED 2026-06-28**  
`hold_monitor.py::_score_stop_distance` previously used `if not dist: return 0.0`, which treated `dist == 0` (price at/through the stop — the worst case) identically to missing data (neutral). Fixed: `dist <= 0` now returns -1.0 explicitly; only `dist is None` returns neutral with a `no_stop_data` label.

### 14.4 Data quality gaps

**n_prior = 50 in Bayesian Kelly is too conservative**  
Once 60+ clean agent-tagged trades exist (post 2026-05-20), the prior should be reduced from 50 to 20 equivalent trades. The heavy prior is currently appropriate because all 79 trades in outcome_log predate the P0-1 fix and lack exit path metadata.

**Composite cache requires 3 days before velocity gate activates**  
Until Tuesday 2026-05-26, the velocity gate will log "skipped — only N days of history" and all signals will enter at full size regardless of velocity.

---

*This document describes Raptor v5.4 as of 2026-05-22. The authoritative implementation is in the GitHub repository: github.com/stevefirwin-svg/Raptor.*

---

## 15. Session 4–5 Additions (2026-06-10 to 2026-06-12)

### 15.1 Cross-Sectional Sector Neutralization
Before composite scoring, each factor's z-score is demeaned by its sector median.
Math: for each factor f and sector S, z[sym][f] -= median({z[m][f] : m in S}).
Median (not mean) for outlier robustness. Sectors with <3 members use universe median.
Effect: stock selection alpha decoupled from sector beta. IC now measures stock-level skill.
Reference: Grinold & Kahn 2000, ch. 5.

### 15.2 OU Hold Target (corrected 2026-06-17 — see §16 for full derivation)
**Original formula (2026-06-11, now superseded):**
hold_target = ln(2) / θ where θ = -b/dt, b from OLS: dX_t = a + b*X_t + eps, X = raw log(price).
This was citation-mismatched (see below) and had three live defects: θ fit on raw log-price
(which is closer to a random walk than to stationary OU for most equities — risk of pure
small-sample spurious mean reversion), no unit-root pre-test (a trending/random-walk stock
could still emit a specific numeric hold_target instead of "no detectable reversion"), and a
bare point estimate with no uncertainty band, sized by an OLS estimator known to be biased
in exactly the near-unit-root regime equities live in.

**Corrected formula (`signals.py::QuantSignalEngine._estimate_ou_hold_target`):**
1. Fit θ on the **market-residual** log-price (stock log-price minus OLS-beta × SPY
   log-price), not raw log-price — removes the I(1) market factor before testing for
   reversion in what's left.
2. **ADF-style unit-root pre-test** (1-lag, MacKinnon 5% critical value ≈ -2.86) gates
   whether θ>0 is credible at all. If the test fails to reject a unit root, hold_target
   reverts to the trending/time-stop branch (30 days) with `reliable=False` rather than
   emitting a fabricated number.
3. φ̂ from OLS is **bias-corrected toward the unit root** via the Marriott-Pope (1941)
   first-order finite-sample correction before converting to θ̂ — OLS φ̂ is biased toward
   zero in finite samples, which biases θ̂ upward and the half-life downward (early-exit bias).
4. A **parametric bootstrap** (400 simulated OU paths at θ̂/σ̂ over the same sample length,
   refit each, 5th/95th percentile of ln(2)/θ̂) replaces the delta-method CI. The delta method
   is anti-informative near the unit root — its variance formula goes to *zero* as θ→0, which
   is backwards (uncertainty should blow up there, not vanish).

`Signal` dataclass now carries `hold_target_low`, `hold_target_high` (90% bootstrap interval,
clamped to [3,30]) and `hold_target_reliable` (bool) alongside the existing `hold_target_days`
point estimate. Downstream consumers (`hold_monitor.py`, `backtest.py`, `main.py`) all read
`hold_target_days` via `getattr(..., 15)` and are unaffected by the new fields; they do not
yet *consume* the CI or reliability flag — see §16 for that as an open follow-on.

**Citation note:** ln(2)/θ is the **half-life heuristic** from the practitioner literature,
not the optimal-stopping exit boundary that Leung & Zhang (2019) actually derive in their
paper. We continue citing Leung & Zhang for the general OU-hold-target *framing* but the
half-life formula itself should be understood as a heuristic, not that paper's main result.
Evaluating the actual optimal-stopping boundary remains a future upgrade (see §16).

TODO:DERIVE min/max bounds (3, 30) at DATA-40 gate — regress realized hold_days vs θ̂ estimate.

### 15.3 Implementation Shortfall Tracking
Every BUY and SELL fill records decision price vs Alpaca filled_avg_price.
IS_bps = side_sign * (fill - decision) / decision * 10,000.
Positive IS = cost against us. Accumulated in slippage_log.json.
Reference: Perold 1988, Almgren & Chriss 2000.

### 15.4 Deflated Sharpe Ratio
DSR = Φ((SR_obs - SR*) / sqrt(V[SR])) where:
  SR* = expected maximum SR from N_trials independent random strategies
  V[SR] = Mertens (2002) variance accounting for skewness and fat tails
Computed on position-level returns (position_outcomes.json), not trim events.
Current: SR=1.42, SR*=1.22, DSR=59.8% WEAK (n=24 independent positions).
Reference: Bailey & López de Prado 2014.

### 15.5 Position-Level Outcome Aggregation
outcome_log.json records every trim event. Multiple trims from one position entry
share entry signal, regime, and factor scores — they are NOT independent.
position_outcomes.json: one record per (symbol, entry_price) group.
position_pnl_pct = dollar-weighted return across all trims.
All gating, IC, DSR calculations use position_outcomes.json exclusively.

### 15.6 Deterministic Entry Gate
Six entry veto rules evaluated as exact boolean predicates in _eval_entry_rules().
Rule 1: regime=="MIXED" AND composite < 1.0
Rule 2: kelly > 0.10 AND atr_pct > 3.5
Rule 3: days_since_earnings < 5
Rule 4: vix_regime=="SPIKE" AND mms < 0.6
Rule 5: macro_regime=="CRISIS"
Rule 6: macro_regime=="RISK_OFF" AND kelly > 0.07
LLM veto (Ollama) demoted to advisory. Disagreements logged as AGENT_OVERRIDE.

### 15.7 Leveraged/Inverse ETP Exclusion
k-times daily-rebalanced ETPs bleed ≈ (k²-k)/2·σ² per day to variance drain.
This invalidates multi-day hold assumptions (ATR stops, hold targets, momentum persistence).
Excluded via name-pattern matching in universe_builder._get_tradeable_assets().
1x sector ETFs (XLE, KRE, SPY) remain eligible.
Reference: Cheng & Madhavan 2009.

---

## 16. Session 6 — OU Hold Target Rework (2026-06-17)

### 16.1 What changed and why
The original OU hold target (§15.2, S5-1) was a working point estimate but had three
defects identified via first-principles derivation (not a logged failure — a desk-check
of the math before any bug surfaced in production):

1. **Series choice.** θ was fit on raw log-price. Raw equity log-price is generally much
   closer to a random walk (I(1), θ=0 in the limit) than to a stationary OU process. Fitting
   AR(1) OLS there risks pure small-sample spurious mean reversion — a finite θ̂ that reflects
   sampling noise, not a real reverting force. **Fix:** fit θ on the market-residual log-price
   (stock log-price minus OLS-beta × SPY log-price) instead.
2. **No stationarity gate.** A trending or random-walk name could still emit a specific
   numeric hold_target (e.g. "9 days") with no signal that the number was statistically
   meaningless. **Fix:** ADF-style unit-root pre-test; if it fails to reject, hold_target
   falls back to the time-stop branch (30d) with `reliable=False` rather than a fabricated
   number.
3. **Point estimate only, with a biased estimator.** OLS φ̂ is known to be biased toward
   zero in finite samples (Marriott-Pope 1941), which propagates to bias θ̂ upward and the
   half-life downward — the system would tend to tell itself to exit too early, and the bias
   is largest exactly in the near-unit-root regime equities live in. There was also no
   confidence interval, so a "12-day" hold target looked precise when the honest uncertainty
   (back-of-envelope delta method, away from the unit root) is roughly ±70% relative SE at
   typical sample sizes. **Fix:** Marriott-Pope bias correction on φ̂ before the θ conversion,
   plus a parametric bootstrap CI (the delta method itself is unreliable/anti-informative
   near the unit root — its variance formula goes to zero as θ→0, backwards from reality).

### 16.2 Citation correction
ln(2)/θ is the **half-life heuristic** from the practitioner literature. Leung & Zhang
(2019)'s actual contribution is an **optimal-stopping exit boundary**, a different and
generally more sophisticated object. We keep citing Leung & Zhang for the general framing
of OU-based hold/exit logic but the half-life formula itself is a heuristic, not that paper's
result. The optimal-stopping boundary is a candidate future upgrade (§16.4).

### 16.3 Implementation
`signals.py::QuantSignalEngine._estimate_ou_hold_target(bars, spy_bars)`. Returns
`{point, low, high, reliable, theta, n_obs}`. `Signal` dataclass extended with
`hold_target_low`, `hold_target_high`, `hold_target_reliable` (all with defaults, so
existing `getattr(signal, "hold_target_days", 15)` call sites in `hold_monitor.py`,
`backtest.py`, `main.py` are unaffected). Verified via synthetic-data unit tests in the
same session (Rule 11): a true-θ=0.10 OU process recovered θ̂≈0.109 with the true half-life
inside the bootstrap interval; a pure random walk correctly triggered `reliable=False`;
a fast-reverting θ=0.30 process clamped correctly at the 3-day floor; a too-short series
hit the documented fallback.

### 16.4 Open follow-ons (not yet built)
- **Consume the reliability flag and CI downstream.** `hold_monitor.py` and `daily_recap.py`
  currently only read the point estimate. A position flagged `hold_target_reliable=False`
  is currently treated identically to a high-confidence one — the time-exit layer doesn't
  yet widen its tolerance or flag the uncertainty in the recap.
- **Documentation/code drift on the trailing stop.** §9 previously described and the master
  plan's P1-3 row claimed an "OU-theta derived" trailing stop (`trail_base = 1/sqrt(theta)`)
  that does not exist in `exit_monitor.py`. The live trail is purely time-tier + profit-ATR
  based (`_trail_mult`). Corrected in §9 above; master plan P1-3 row needs the same correction.
- **Real optimal-stopping boundary.** Evaluate replacing the half-life heuristic with the
  actual Leung & Zhang (2019) / Leung & Li optimal-stopping exit boundary once θ estimation
  is trustworthy at scale — this is a strictly more principled object than ln(2)/θ.
- **Median-unbiased estimation (Andrews 1993)** as a sharper bias correction than the
  first-order Marriott-Pope plug-in used here, if the plug-in proves inadequate once real
  trade data accumulates.
- **TODO:DERIVE min/max bounds (3,30)** — unchanged from §15.2, still gated at DATA-40.

## 17. Session 7 — Kelly Drawdown-Budget Rework (2026-06-17)

### 17.1 What changed and why
`kelly_engine.py::_dd_constrained_f` previously used `f_max = dd_tolerance / (σ√252)` —
a volatility-scaling heuristic with no probabilistic interpretation. It said roughly
"how big can the bet be relative to typical dispersion," but never specified what
*probability* of breaching the drawdown tolerance that bet size actually implies. There
was no way to know whether the constraint was conservative or aggressive relative to a
stated risk budget, because it wasn't built from one.

**Fix (derived from first principles, worked by hand before being coded — see chat log
2026-06-17):** in the continuous (GBM) approximation, betting fraction λ of full Kelly
makes log-wealth a drifted Brownian motion with drift `m = γλ(1−λ/2)` and variance rate
`v = γλ²` where `γ = μ²/σ²`. The probability that an equity excursion from a new peak
ever reaches drawdown depth `d = −ln(β)` (β = 1 − dd_tolerance), via the exponential
martingale and optional stopping, collapses to:

```
P(drawdown episode reaches β) = β^((2−λ)/λ)
```

Note that μ, σ, and γ all cancel — the drawdown *profile* of fractional-Kelly betting
depends only on λ, not on the size of the edge. This is inverted to solve directly for
the λ consistent with a stated risk budget: given a tolerance β and a target ceiling on
the probability of ever breaching it, p_tol:

```
λ* = 2 / (1 + ln(p_tol)/ln(β))
```

`f_dd_constrained = λ* × f_star` (full-Kelly empirical estimate, not the Bayesian-shrunk
`f` — λ is defined relative to full Kelly by construction, so scaling the shrunk value
would make the result uninterpretable against its own derivation).

### 17.2 What this revealed
Worked example with dd_tolerance=0.12, p_tol=0.05: λ* ≈ 0.082. Half-Kelly (λ=0.5) is
roughly 6× too aggressive for a hard 12% drawdown cap at any reasonable breach tolerance.
**Half-Kelly and a tight drawdown cap are mathematically inconsistent with each other.**

Running the new formula against the live MOMENTUM book (53 trades, 2026-06-17):
at `MAX_DD=0.15`, `P_TOL=0.05`, λ* ≈ 0.103 — i.e. the drawdown-consistent fraction is
roughly a fifth of half-Kelly. The Bayesian prior shrinkage (`F_PRIOR=0.02`, `N_PRIOR=50`)
was doing the real risk-limiting work by accident — the system looked growth-constrained
(bounded mainly by half-Kelly) when it was actually drawdown-constrained, with the prior
masking that fact. That protection decays mechanically as n grows past N_PRIOR and the
prior's grip on the blended estimate weakens (already happening: n=53 vs N_PRIOR=50).
This rewrite makes the drawdown budget an explicit, persistent constraint rather than an
artifact of small-sample shrinkage that will quietly loosen over the next 1–3 months of
trading without anyone changing a line of code.

### 17.3 Diagnostic addition — fat-tail correction to f*
A fourth-order Taylor expansion of `E[ln(1+fR)]` gives
`f* ≈ f_kelly_naive × (1 + s·η − κ·η²)`, where s = skewness, κ = full kurtosis,
η = μ/σ (per-trade Sharpe). Positive skew raises true Kelly above the naive estimate;
fat tails lower it; they cross at η* = s/κ. This is now computed and reported per book
as `f_star_correction_factor_DIAGNOSTIC_ONLY` — **not used in production sizing.** At
κ≈8–10 (the live MOMENTUM book's measured kurtosis), the expansion's own convergence
condition (|fR| < 1) is marginal and the 5th moment barely exists, so a 4th-order
polynomial near its own breakdown point is not trustworthy for sizing — only for
direction. Production sizing continues to rely entirely on the bootstrap, which captures
these moments empirically by resampling actual bounded (post-stop) outcomes rather than
extrapolating from a moment expansion.

### 17.4 Implementation
`kelly_engine.py::_lambda_for_drawdown_budget(dd_tolerance, p_tol)` — closed-form solve,
guards `0 < p_tol < β < 1` and returns `None` (fails open to unconstrained `f_star`, not
to zero) if the inputs are out of range, so a misconfiguration is visible in the output
rather than silently producing a number that looks plausible but doesn't mean what it
claims. `_dd_constrained_f` calls it and scales `f_star`. `kelly_estimates.json` gained
`dd_budget_lambda`, `dd_budget_inputs`, `max_dd_tolerance`, `p_tol`, `p_tol_status` —
all additive; zero existing fields removed or reshaped. Sole downstream consumer
(`get_recommended_kelly()`) reads only `f_recommended`/`mode`, unaffected.

Verified same session (Rule 11): lambda formula reproduces the hand-derived session
value (0.0819 for 12%/5% inputs) to 3 decimal places; boundary guard correctly rejects
`p_tol ≥ β`; fail-open behavior confirmed against a deliberately invalid input; full run
against live `outcome_log.json` (53 trades) moved `f_dd_constrained` from 3.83% (old
heuristic) to 5.07% (new formula) while remaining the binding constraint ahead of
half-Kelly's 13.17%. Kelly remains SHADOW mode throughout — this change affects only the
number being logged, not live sizing (Rule 5, RAPTOR_SKILL.md).

### 17.5 Open follow-ons (not yet built)
- **P_TOL is not yet derived**, only flagged. It is currently a conventional 5%-tail
  placeholder, not fit to Raptor's own equity curve or an explicit ruin-cost function from
  Steve. Gated at DATA-60 — need enough independent positions to observe real multi-trade
  excursion behavior, not just per-trade return dispersion, before this can be calibrated
  honestly rather than guessed.
- **Skew/kurtosis correction stays diagnostic-only.** Revisit promoting it into the
  production pipeline only if the bootstrap's empirical capture of these moments proves
  inadequate at higher n — there is no evidence of that yet, and the moment-expansion
  route carries real truncation risk at this kurtosis level.
- **CPCV / purge-embargo (item #2 from the same derivation session) and HMM macro regime
  (item #1) were both scoped in the same session as this Kelly rework but deliberately
  NOT built yet** — CPCV is provably not meaningful below n≈100 (own derivation: SE(IC)
  at n=27 is ~0.19, swamping any IC in the range Raptor is trying to detect), and HMM is
  a larger build queued separately (ARCH-2, no data gate, but not started this session).

---

## 18. Infrastructure — OneDrive Migration (2026-06-19)

### 18.1 Root cause of ledger corruptions

Between May and June 2026, Raptor experienced three silent ledger corruption events where
`position_ledger.json` reverted to a stale version with no exception thrown and no log entry.
Root cause: Raptor ran from `C:\Users\steve\OneDrive\Desktop\Raptor`, which was inside
OneDrive's file-system watch scope.

`exit_monitor.py` writes `position_ledger.json` up to 5 times in a single 30-second execution
window (once per exit/trim, each via `os.replace(tmp → file)`). OneDrive's sync agent watches
for file modifications and attempts an upload on each write. When writes arrive faster than
OneDrive can complete an upload cycle, it detects a conflict between its in-progress upload
and the new local version. Its conflict resolution silently reverts the local file to the
cloud version. The correct data is written to disk, then immediately overwritten by stale data.
No `WinError 32` is thrown because the revert happens after the lock is released — only the
cases where OneDrive held the lock during the `os.replace()` call produced visible errors
(seen in `recap_errors.log` on 2026-05-29 and 2026-06-11).

The three corruption events:
1. **May 2026:** 8 positions disappeared from ledger while remaining open on Alpaca (the
   `record_exit`-for-trims bug also contributed, but OneDrive amplified it)
2. **June 18, 2026:** AAL trim (117sh) confirmed executed, ledger reverted to pre-trim state
3. **June 18, 2026:** KDP/PFE/SQQQ exits confirmed in logs, ledger not updated

### 18.2 Fix

Raptor moved to `C:\Raptor` — outside OneDrive's sync scope. Git is the sole sync mechanism:
- `Daily_GitHub_Push.bat` runs at 6 PM daily (`git add -A && git push`)
- `sync_to_claude.py` produces the upload manifest for the Claude Project
- OneDrive continues to sync Desktop, Documents, etc. — just not `C:\Raptor`

**Files patched:** 22 (all `.bat`, `.ps1`, `.py`, `.md` files containing the old path)
**Task Scheduler tasks:** 19 re-registered/updated, all verified `[OK]` pointing to `C:\Raptor`
**Commit:** `f3a6ab8` (2026-06-19)

### 18.3 What to check if a ledger discrepancy is suspected

1. Run `python diagnose_system.py` — section 6 checks Alpaca/ledger sync
2. Run `python raptor_monitor.py` — L2-Reconciliation checks qty mismatches and ghost positions
3. Compare `trim_log.json` trim_qty entries against ledger `trims[]` arrays per position
4. If discrepancy found: run `python backfill_ledger.py --write` then `python outcome_tracker.py`

The OneDrive revert pattern is identifiable: trim/exit appears in `exits_YYYYMMDD.log` with
`OK: PENDING_NEW` and slippage backfill, but ledger shows pre-event state. No `Ledger record
failed` warning appears because the write succeeded — it was the subsequent revert that
corrupted the file.

---

*This document describes Raptor v5.4 as of 2026-06-19 (session 8 — OneDrive migration and ledger repair).*
*Authoritative implementation: github.com/stevefirwin-svg/Raptor*
