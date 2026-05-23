# Raptor v5.5 — Complete System Ontology
*Full decision logic, mathematics, and feedback loops. No code.*
*Last updated: 2026-05-23*

---

## Purpose of This Document

This document describes every decision Raptor makes, from the first market open scan to the final exit, including all the math underneath each decision. It is written for someone who wants to understand the logic completely, find flaws, or audit whether the system behaves as intended. No programming knowledge is required to read it.

Every number that is hardcoded is labeled as such. Every number derived from data is explained.

**v5.5 change summary:** The single blended composite signal engine has been replaced with two separate, non-conflicting signal books — MOMENTUM and MEAN_REVERSION — each with independent factors, gates, entry logic, and exit rules. A BottomTopDetector provides Bulkowski-validated candlestick pattern confirmation. A CompositeRanker unifies both books by conviction score.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [The Macro Layer — Daily Market Regime](#2-the-macro-layer--daily-market-regime)
3. [Universe Construction](#3-universe-construction)
4. [The Signal Engine — Dual-Book Architecture (v5.5)](#4-the-signal-engine--dual-book-architecture-v55)
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

Raptor is a quantitative swing trading system running on a single Alpaca paper account (~$105K equity). It holds between 0 and 10 positions at a time, with a typical hold duration of 2–20 trading days depending on trade type. It is designed for US equities only.

### 1.1 Core Design Principle

Every decision in Raptor is made by math first. An LLM agent (Ollama/llama3.2, running locally) observes the same decisions and provides reasoning in natural language, but the agent never executes a trade. The agent's output is recorded for calibration.

**Math-first mandate:** Every value in the system must be derivable from a formula, a distributional analysis, or an empirical measurement. Round numbers and intuitive guesses are explicitly prohibited. If the answer to "why that number?" is not a mathematical derivation, the number is wrong.

### 1.2 Five Stages of Every Trade

```
STAGE 1: MACRO GATE       — Is it safe to enter anything today?
STAGE 2: UNIVERSE SCAN    — Which ~120–181 symbols to score?
STAGE 3: SIGNAL ENGINE    — Which symbols have genuine edge per book?
                             Book 1: MOMENTUM (trend continuation)
                             Book 2: MEAN_REVERSION (panic exhaustion)
STAGE 4: ENTRY FILTERS    — Pre-trade gates (in order):
                             1. Already-held filter
                             2. Re-entry cooldown (5 days after hard_stop/trail_loss)
                             3. Margin guard (util >90% block, >85% reduce)
                             4. EntryAgent veto (Ollama/llama3.2)
STAGE 5: EXECUTION        — Conviction-scaled Kelly sizing, submit, record
                             trade_type stamped in ledger metadata
                             Per-book log written (raptor.momentum or raptor.mean_reversion)
```

Once in a position, two parallel systems run every 30 minutes during trading hours:

```
HOLD MONITOR   — Scores position health on 10 factors, recommends trim/hold
EXIT MONITOR   — Checks exit conditions (ruleset differs by trade_type)
```

### 1.3 Why Two Books (v5.5 Rationale)

The v5.4 single-composite engine blended mean-reversion and momentum factors in one score. These two factor families are structurally opposed:

- A stock scoring high on MR factors (oversold, below moving averages, panic volume) will score low on momentum factors (EMAs not stacked, weak MACD, underperforming SPY)
- On a concentrated 70-stock momentum universe the trend factors dominate by accident
- On a broad 181-stock universe the conflict averages to near-zero edge — confirmed by backtest showing 57.7% return vs SPY's 112.5% over 2020–2025

**Research basis:** Jegadeesh & Titman (1993) — momentum requires trend confirmation. De Bondt & Thaler (1985) — mean reversion requires overreaction and exhaustion. Same entry signal cannot reliably detect both.

---

## 2. The Macro Layer — Daily Market Regime

**File:** `macro_context.py`
**Output:** `macro_context.json`
**Runs:** Once per day, pre-market (~9:00 AM ET).

### 2.1 Six Input Signals

| Signal | Source | What It Measures |
|--------|--------|-----------------|
| SPY trend | Alpaca | 20-day return + MA relationship |
| VIX | Yahoo Finance (^VIX) | Implied volatility regime |
| Credit spread | FRED (HY-IG) | Credit stress |
| Sector breadth | Yahoo Finance (11 sector ETFs) | Market internals width |
| Yield curve | FRED (T10Y2Y) | Recession signal |
| Fed rate | FRED (FEDFUNDS) | Monetary policy stance |

### 2.2 Signal-to-Score Conversion (each → [-1, +1])

**SPY trend:**
`score = clip(20d_return / 5%, -1, 1) + (0.2 if price > 200MA else -0.2)`

**VIX:**
`score = -clip((VIX - 20) / 15, -1, 1)`
VIX=35 → -1.0. VIX=20 → 0. VIX=5 → +1.0.

**Credit spread:**
`score = -clip(spread / 3.5%, -1, 1)`

**Sector breadth (Zweig 1986):**
`breadth_composite = 0.40 × pct_above_50MA + 0.35 × pct_above_150MA + 0.25 × pct_above_200MA`
200MA breadth ≥70% sectors → BULL_MARKET → +1 extra vote. <50% → BEAR_MARKET → -1 extra vote.

**Yield curve:**
`score = clip(T10Y2Y / 1.5%, -1, 1)`

**Fed rate:**
`score = -clip((fed_funds - 2.5%) / 3%, -1, 1)`

### 2.3 Weighted Composite

```
raw_score = 0.30 × spy + 0.25 × vix + 0.20 × credit + 0.15 × breadth + 0.07 × yield_curve + 0.03 × fed
```

*⚠ Weights are hand-picked. Not calibrated against historical regime prediction accuracy. This is an open gap.*

### 2.4 Kalman Filter Smoothing (Target Design — not yet implemented)

```
x_prior = x_previous
P_prior = P_previous + Q        (Q = 0.05, process noise)
K = P_prior / (P_prior + R)     (R = 0.20, observation noise)
x_updated = x_prior + K × (raw_score - x_prior)
P_updated = (1 - K) × P_prior
```

*Current code uses vote-count scoring, not Kalman. Target architecture pending GAP A.*

### 2.5 Regime Classification with Hysteresis (Target Design)

| Smoothed Score | Regime |
|----------------|--------|
| ≥ 0.25 | RISK_ON |
| -0.25 to 0.25 | NEUTRAL |
| -0.70 to -0.25 | RISK_OFF |
| < -0.70 | CRISIS |

Hysteresis ±0.10 prevents flickering at boundaries.

### 2.6 Hard Overrides

- VIX raw > 35 → CRISIS regardless of score
- Credit spread "STRESS" → RISK_OFF regardless

### 2.7 Regime Effects Downstream

| Regime | Entries | Kelly | Book emphasis |
|--------|---------|-------|---------------|
| RISK_ON | Yes | 1.0× | MOMENTUM |
| NEUTRAL | Yes | 0.8× | Balanced |
| RISK_OFF | Yes | reduced_in_bearish × | MEAN_REVERSION |
| CRISIS | **No** | — | — |

---

## 3. Universe Construction

**File:** `universe_builder.py`
**Output:** Symbol list (~120–150 live screen, 181 locked backtest)

### 3.1 Live Screen Filters

| Filter | Threshold | Rationale |
|--------|-----------|-----------|
| Price | $5–$1,000 | Avoid penny stocks |
| Avg daily volume | ≥ 500,000 shares | Fill-ability |
| Dollar volume | ≥ $20M/day | Liquidity at $105K scale |
| Daily price range | ≥ 1.0% | Minimum volatility for signal |

*Known flaw: thresholds not sensitivity-tested. Parameter sweep ±50% not performed.*

### 3.2 Backtest Universe

**`backtest_universe.txt`** — 181 symbols, fixed and locked. Never replaced with live screen for analysis.

**Rationale:** Using today's live screen for backtesting produced non-reproducible results (70 symbols one day, 136 another). Backtests must use the same universe to be comparable. The 181-symbol file covers S&P 500 / NASDAQ 100 core + historically liquid mid-caps representative of the 2020–2025 opportunity set.

```powershell
# Regenerate only when universe scope changes — document reason in git commit
python generate_backtest_universe.py
```

### 3.3 Known Baseline (2026-05-23)

The model currently **underperforms SPY** on the locked 181-symbol universe (57.7% vs 112.5% over 2020–2025, Sharpe 0.473). This is the ground truth that v5.5 is designed to solve.

Root cause: single-composite blending of opposing factor families. V5.5 dual-book engine is the fix.

---

## 4. The Signal Engine — Dual-Book Architecture (v5.5)

**File:** `signals.py`
**Classes:** `MomentumSignalEngine`, `MeanReversionSignalEngine`, `BottomTopDetector`, `CompositeRanker`, `QuantSignalEngine`

### 4.1 Architecture Overview

```
Raw factor computation for all symbols
         │
         ▼
Cross-sectional z-scoring (MAD-robust, per factor)
         │
         ├─→ MomentumSignalEngine.score() per symbol
         │    Gate: trend confirmed + above 50 EMA + positive RS
         │    BottomTopDetector.detect_top() → suppress on exhaustion
         │
         ├─→ MeanReversionSignalEngine.score() per symbol
         │    Gate: RSI(5)<35 + below BB + panic + bottom pattern
         │    BottomTopDetector.detect_bottom() → REQUIRED
         │
         └─→ CompositeRanker.rank()
              Normalize each book [0,1] within-book conviction
              Unify into single ranked list
              Signal.trade_type = MOMENTUM | MEAN_REVERSION
              Signal.pattern_signal = candlestick pattern name
              Signal.book_conviction = [0,1]
```

### 4.2 Raw Factor Computation

All symbols with ≥ 80 bars of daily OHLCV data receive a full factor computation. Factors are separated by book — they are never blended.

#### MOMENTUM FACTORS (8)

**MA Stack (`ma_stack`)**
`order = (EMA8 > EMA21) + (EMA21 > EMA50) - 1` → -1, 0, +1
`slope = clip(avg 5d return of EMA8/21/50 × 50, -0.4, 0.4)`
`ma_stack = order × 0.6 + slope`
Positive = EMAs aligned upward with upward slope.

**MACD Acceleration (`macd_accel`)**
`macd_accel = polyfit_slope(MACD_histogram[-5:]) / current_price`
Positive = momentum building. Negative = dying.

**ADX Direction (`adx_dir`)**
`adx_dir = ADX_14 × (+1 if +DI > -DI else -1)`
Positive = structured uptrend.

**Relative Strength (`rel_strength`)**
`rel_strength = (sym[-1]/sym[-10]) - (SPY[-1]/SPY[-10])`
10-day alpha over SPY.

**OBV R² (`obv_r2`)**
`obv_r2 = slope × R²` of linear regression on normalized OBV over 10 days.
Volume confirming uptrend with confidence.

**Accumulation/Distribution (`accum_dist`)**
`CLV = ((close-low)-(high-close)) / (high-low)`
`AD = cumsum(CLV × volume)`
`accum_dist = slope × |R|` — institutional accumulation trend.

**Price Cloud (`price_cloud`)**
`price_cloud = (price - (EMA8+EMA50)/2) / |EMA8-EMA50|`
Positive = above EMA cloud midpoint.

**Volume Ratio (`vol_ratio`)**
`vol_ratio = log(today_volume / 21d_avg_volume)`
Log-normalized. Positive = above-average volume.

#### MEAN REVERSION FACTORS (7)

**RSI Mean Reversion (`rsi_mr`)**
`rsi_mr = (50 - RSI_5) / 50`
RSI_5=25 (deeply oversold) → +0.50. RSI_5=70 → -0.40.
*Uses 5-period RSI for fast response, not 14.*

**Bollinger Z-Score (`bollinger_z`)**
`bollinger_z = -(price - 20d_MA) / 20d_std`
Positive = price below lower Bollinger band.

**Crowd Panic (`crowd_panic`)**
`panic = Σ over last 3 days: if close[i] < close[i-1]: (vol[i]/avg_vol_21d) × |return[i]|`
High = institutional capitulation = potential reversal.

**MA Distance (`ma_distance`)**
`ma_distance = -(price - avg(EMA8,EMA21,EMA50)) / avg(EMA8,EMA21,EMA50)`
Positive = price below moving average composite.

**Bollinger Squeeze (`bb_squeeze`)**
`bb_squeeze = -(percentile_rank(BB_width in 60d history) / 100 - 0.5) × 2`
Tight bands (squeeze) = positive.

**Reversal Momentum (`rev_momentum`)**
`rev_momentum = (close - lowest_low_3d) / ATR_14`
How far price has bounced off 3-day low in ATR units. Confirms recovery.

**ATR Percentile (`atr_pctile`) — MR direction**
`atr_pctile = (percentile_rank(ATR_today in 60d) / 100 - 0.5) × 2`
*Note: direction reversed from v5.4. For MR, HIGH ATR = volatility spike = exhaustion = positive signal.*

### 4.3 Cross-Sectional Z-Score Normalization

All factors are normalized cross-sectionally in one pass:
1. Collect all non-NaN values for factor across all symbols
2. Robust location: **median** (not mean)
3. Robust scale: **MAD × 1.4826** (equivalent to σ for normal distribution)
4. `z = clip((raw - median) / (MAD × 1.4826), -3, 3)`
5. NaN → z = 0.0 (neutral, not penalized)

*Why MAD: a single extreme value (volume spike) inflates std and compresses everyone else. MAD is immune to outliers.*

### 4.4 MomentumSignalEngine — Gates and Scoring

**Hard gates (ALL must pass):**
1. Trend confirmed: Hurst H > 0.52 OR ADX_raw > 22
2. Price above 50 EMA
3. Relative strength ≥ -0.01 (not underperforming SPY badly)

**Entry timing refinement:**
- Pullback quality = `1 - min(dist_to_EMA8, dist_to_EMA21) / price`
- Higher pullback_quality = price closer to EMA = better entry timing
- `vol_ratio` weight reduced when pullback_quality high (want low vol on pullback)

**Top detection suppression:**
- `BottomTopDetector.detect_top()` runs on every candidate
- Bearish_engulfing, evening_star, three_black_crows → hard suppress (entry blocked)
- Other top patterns → no suppression (informational only)

**Composite:**
`comp = Σ(z[fn] × w[fn]) for fn in MOMENTUM_FACTORS`
Equal base weights adjusted by pullback_quality. `comp > 0` required.

**Exit ruleset (exit_monitor.py):**
- Wide trail stop (rcfg.initial_stop_atr_mult)
- Hold target: 15 days
- No fixed take-profit — trail exit only
- `momentum_break` exit: 2 consecutive closes below 8-EMA while profitable

### 4.5 MeanReversionSignalEngine — Gates and Scoring

**Hard gates (ALL must pass):**
1. RSI(5) < 35
2. `bollinger_z > 0` (price below lower Bollinger band)
3. `crowd_panic > 0.005` (volume spike on down days)
4. `BottomTopDetector.detect_bottom()` returns a pattern name (REQUIRED)
5. Distance to 20-day SMA > 0.5% (reversion room exists)

**Pattern boost:**
- `bullish_engulfing`, `morning_star`, `three_white_soldiers` → composite × 1.3
- Other patterns → composite × 1.0

**Composite:**
`comp = Σ(z[fn] × w[fn]) × pattern_boost for fn in MR_FACTORS`
Equal base weights. `comp > 0` required.

**Exit ruleset (exit_monitor.py):**
- Tight trail stop (1.5× ATR)
- Hold target: 2–5 days (derived from distance_to_mean / 0.005)
- Take-profit at 20-day SMA
- Hard time_stop at 5 days regardless of P&L

### 4.6 BottomTopDetector — Bulkowski (2008) Pattern Library

Only patterns with >60% confirmed reversal rate in Bulkowski's study of 4.7M candles are included.

**Bottom patterns (bullish reversals):**

| Pattern | Bulkowski Rate | Logic |
|---------|---------------|-------|
| `hammer` | 60.4% | Small body, lower shadow ≥ 2× body, tiny upper shadow, prior bearish candle |
| `bullish_engulfing` | 63.0% | Bullish body fully engulfs prior bearish body, volume ≥ avg |
| `morning_star` | 61.5% | Bearish → small body (star) → bullish closing above midpoint |
| `piercing_line` | 62.1% | Opens below prior low, closes above prior midpoint but below prior open |
| `three_white_soldiers` | 65.4% | 3 consecutive bullish candles, each higher open and close, closes near high |
| `rsi_bull_divergence` | — | Price: lower low, RSI(5): higher low, RSI still < 45 (Wilder 1978) |

**Top patterns (bearish reversals):**

| Pattern | Bulkowski Rate | Logic |
|---------|---------------|-------|
| `shooting_star` | 60.8% | Small body, upper shadow ≥ 2× body, tiny lower shadow, prior bullish |
| `bearish_engulfing` | 60.6% | Bearish body fully engulfs prior bullish body, volume ≥ avg |
| `evening_star` | 61.8% | Bullish → small body → bearish closing below midpoint |
| `dark_cloud_cover` | 62.3% | Opens above prior high, closes below prior midpoint but above prior open |
| `three_black_crows` | 65.1% | 3 consecutive bearish candles, each lower open and close, closes near low |
| `rsi_bear_divergence` | — | Price: higher high, RSI(14): lower high, RSI still > 60 |

*Patterns with <60% Bulkowski rate (harami, doji, spinning top) are deliberately excluded.*

### 4.7 CompositeRanker — Unified Conviction Scoring

**Grinold & Kahn principle:** Allocation proportional to information ratio. No fixed per-book quotas.

```
Step 1: Normalize each book's scores within-book to [0, 1]
  book_conviction = (comp - min_comp) / (max_comp - min_comp)

Step 2: Combine both books into one list sorted by book_conviction

Step 3: Return top max_orders_per_scan candidates
```

Result: A high-conviction MR setup competes for position slots on equal footing with a high-conviction momentum setup. The portfolio naturally reflects whichever book has more edge on a given day.

### 4.8 Regime and Market Scale

**Market scale** (applied to Kelly sizing, not signal gate):
```
roc_20 = SPY 20-day return
roc_5  = SPY 5-day return

Bull trend intact (roc_20 > 2%):              scale = 1.0
Bull trend breaking (roc_20 > 1%, roc_5 < -2%): scale = 0.5
Flat market (-2% ≤ roc_20 ≤ 2%):              scale = 0.8
Downtrend (roc_20 < -2%):                     scale = 0.5
```

**CRISIS:** All entries halted regardless of signal quality.

---

## 5. Entry Filters — Pre-Trade Gates

### 5.1 Already-Held Filter

Symbol already in portfolio → blocked. Raptor never adds to a position.

### 5.2 Re-Entry Cooldown ✅

After `hard_stop` or `trail_loss` exit: 5-day block.
`trail_profit` and `profit_target` exits: not blocked (thesis worked).
Sources checked: `outcome_log.json` (exit_path), `position_ledger.json` (exit_reason).

*Target design (ATR-scaled cooldown — not yet implemented):*
`cooldown_days = clip(3 + ATR_percentile × 12, 3, 15)`

### 5.3 Composite Velocity Gate ✅

Tracks composite score trajectory in `hold_history.json`:
```
velocity = composite_today - composite_3d_ago
kelly_modifier = clip(1.0 + velocity × 0.2, 0.80, 1.20)
effective_kelly = kelly_fraction × kelly_modifier
```
Requires ≥ 3 days of history. Falls back to 1.0× for new symbols.

### 5.4 Margin Guard ✅

| Condition | Action |
|-----------|--------|
| equity ≤ 0 | BLOCK |
| util > 90% | BLOCK |
| util > 85% | REDUCE — cap new positions at 1 |
| cash < 0 (on margin) | REDUCE — cap at 1 |
| util > 75% | WARNING, proceed |
| API error | BLOCK (fail closed) |

### 5.5 EntryAgent Veto

Ollama/llama3.2 reviews signal. Can block entry. Decision recorded in `entry_vetoes.json` for calibration.

---

## 6. Position Sizing — Kelly Fraction

### 6.1 Current Implementation

```
base_kelly = rcfg.kelly_fraction × (0.5 + min(|book_conviction| / 1.0, 1.0))
kelly = clip(base_kelly × market_scale × velocity_modifier, 0.02, 0.12)
```

**Book-specific caps:**
- MOMENTUM: kelly capped at 0.12 (full range)
- MEAN_REVERSION: kelly capped at 0.08 (smaller — binary outcome, tight stop)

**BEARISH regime:** kelly × reduce_in_bearish (config, typically 0.5–0.7)

### 6.2 Target Design — Conviction-Scaled Kelly (GAP 2, open)

```
kelly = f_min + (f_base - f_min) × book_conviction
```

`book_conviction` is the within-book normalized [0,1] score from CompositeRanker. Top of book gets full Kelly. Bottom of entry threshold gets minimum Kelly.

### 6.3 Target Design — Bayesian Kelly (GAP B, open)

```
f* = μ / σ²                     (empirical Kelly from outcome_log.json)
f_posterior = (N×f* + n_prior×f_prior) / (N + n_prior)   (n_prior=50)
f_half = f_posterior × 0.5      (half-Kelly discount)
f_base = min(f_half, f_max_DD, 0.15)
```

### 6.4 Stop Price by Trade Type

**MOMENTUM:**
```
stop_price = entry_price - rcfg.initial_stop_atr_mult × ATR_14
```
(stop_mult varies: TRENDING=initial_stop_atr_mult, REVERTING=2.0, MIXED=2.5)

**MEAN_REVERSION:**
```
stop_price = entry_price - 1.5 × ATR_14   (tighter — MR has defined target)
```

### 6.5 Hold Target by Trade Type

**MOMENTUM:** 15 days (fixed pending GAP C OU theta implementation)

**MEAN_REVERSION:**
```
hold_target = clip(int(distance_to_mean / 0.005), 2, 5)
```
Distance = (20d_SMA - entry_price) / entry_price. The further below the mean, the more hold time allocated, capped at 5 days.

---

## 7. Entry Execution

**File:** `main.py`

### 7.1 Execution Flow

```
market_decision.json → STANDBY? exit immediately
         ↓
margin_guard.check() → BLOCK? exit
         ↓
generate_signals() → MOM + MR candidates → CompositeRanker → top-N
         ↓
filter: already-held, cooldown, velocity
         ↓
EntryAgent.evaluate_batch()
         ↓
for each signal passing all filters:
    shares = floor(equity × kelly / entry_price)
    submit BUY order (limit or market per config)
    ledger.record_entry(trade_type, pattern_signal, conviction in metadata)
    log to raptor.momentum or raptor.mean_reversion logger
```

### 7.2 Trade Identity Logging

Every order writes two log entries:
1. Main logger (`raptor_DATE.log`): `ORDER [RAPTOR|MOMENTUM]: BUY 50 NVDA @ $875.20 pattern=hammer conviction=0.847 hold~15d`
2. Book logger (`momentum_DATE.log` or `mr_DATE.log`): full entry detail

This enables per-book P&L analysis over time.

---

## 8. Hold Monitor — Daily Position Health Scoring

**File:** `hold_monitor.py`
**Output:** `hold_health.json`, `hold_history.json`
**Runs:** Every 30 minutes 9:35–3:50 PM ET via `Start_Intraday_Monitor.bat`

### 8.1 Daily Snapshot

For each held position, a snapshot of ~25 metrics is recorded:

| Metric | How Computed |
|--------|-------------|
| `composite` | Today's composite from signal engine |
| `factor_scores` | All factor z-scores (split by book) |
| `factor_agreement` | Fraction of book-relevant factors positive |
| `roc_5d` | 5-day price return |
| `higher_highs/lows` | Price structure |
| `close_pos_5d` | Mean close position in day's range |
| `vol_ratio` | Today's volume / 20d avg |
| `obv_slope` | OBV trend over 5 days |
| `atr_expansion` | ATR_14 / ATR_10 |
| `stop_dist_atr` | (price − stop) / ATR |
| `hold_ratio` | days_held / hold_target |
| `trade_type` | MOMENTUM or MEAN_REVERSION |

### 8.2 Ten Scoring Layers

| Layer | Weight | Measures |
|-------|--------|---------|
| 1. Composite slope | 23% | Is signal strengthening or weakening? |
| 2. Factor agreement (FAR) | 18% | How many book-relevant factors agree? |
| 3. Price momentum | 14% | Price moving in thesis direction? |
| 4. Cluster health | 13% | Strength across factor clusters? |
| 5. Volume | 9% | Institutional flow direction? |
| 6. Volatility context | 7% | Volatility helping or hurting? |
| 7. Stop distance | 5% | How close to stop? |
| 8. Hold duration | 2% | Overstaying a non-working position? |
| 9. Anchored VWAP | 5% | Price above/below entry VWAP? ✅ |
| 10. Shannon entropy | 4% | Price action directional or chaotic? ✅ |

*Weights are hand-picked. Target: Spearman IC of each layer against realized PnL, rolling 60-day window. Requires 60+ clean outcome records.*

**Layer 1 — Composite Slope:**
`slope = polyfit of composite over last 5 snapshots`
`score = clip(slope / 0.10, -1, 1)`

**Layer 2 — Factor Agreement (FAR):**
`FAR = book_factors_positive / total_book_factors`
`base = (FAR - 0.5) × 2.0`
`trend = clip(FAR_today - FAR_3d_ago, -0.3, 0.3) × 3.0`
*Reference: Frazzini & Pedersen (2014) on factor breadth.*

**Layer 3 — Price Momentum (rolling trend added 2026-05-17):**
- ROC(5d): `clip(roc_5d / 10%, -1, 1)` × 0.35
- Structure: `(higher_highs + higher_lows - 1.0)` × 0.30
- Close position: `clip((close_pos_5d - 0.5) × 4, -1, 1)` × 0.20
- ROC trend: `clip(roc_delta_3d / 5%, -1, 1)` × 0.15

**Layer 5 — Volume (rolling OBV trend added 2026-05-17):**
- OBV slope: `sign(slope) × min(1, |slope| / max(|slope|, 1000))` × 0.35
- UD ratio: nonlinear (>1.5 positive, <0.67 negative) × 0.35
- OBV trend: linear fit of OBV slopes over 3 snapshots × 0.30

**Layer 9 — Anchored VWAP (✅ 2026-05-22):**
```
VWAP_anchored = Σ(price × vol_ratio) / Σ(vol_ratio) over all held days
score = clip((current_price - VWAP_anchored) / ATR, -1, 1)
```

**Layer 10 — Shannon Entropy (✅ 2026-05-22):**
```
H = -Σ(p × log(p)) over 5-bin histogram of last 10 daily returns
score = clip(1 - 2 × H/log(5), -1, 1)
```
H=0 (directional) → +1.0. H=log(5) (random) → -1.0.
Rising entropy trend over 3 snapshots adds penalty.
*Reference: Shannon (1948)*

### 8.3 Health Score and Tier

```
health = clip(Σ(layer_score × weight), -1, 1)
```

| Score | Tier |
|-------|------|
| ≥ +0.20 | STRENGTHENING |
| -0.15 to +0.20 | STABLE |
| < -0.15 | DECAYING |
| < 3 snapshots | INSUFFICIENT_DATA |

### 8.4 Trim Recommendation — Kelly-Anchored

Generated only when tier = DECAYING:

```
health_norm = clip((health + 1.0) / 0.85, 0, 1)
trim_pct = 1 - health_norm
```

- health = -0.50 → trim_pct = 41%
- health = -0.90 → trim_pct = 88%
- health = -1.00 → trim_pct = 100% (EXIT)

Action labels: TRIM_MINOR (<25%), TRIM_MODERATE (25–50%), TRIM_MAJOR (50–90%), EXIT (≥90%)

---

## 9. Exit Monitor — Exit and Trim Decisions

**File:** `exit_monitor.py`
**Runs:** Every 30 minutes 9:35–3:50 PM ET (intraday monitor loop)

**Critical change in v5.5:** Exit rules differ by `trade_type`. The monitor reads the trade_type from the position ledger and applies the appropriate ruleset.

### 9.1 Signal Engine Re-Run

Exit monitor runs the full dual-book signal engine to get today's composite for all held symbols. Uses `_last_full_signals` — includes all scored symbols before top-N filter.

### 9.2 Exit Ruleset — MOMENTUM Positions

**EXIT 1 — Hard Stop (vol-regime aware ✅)**
```
ATR_pctile = percentile_rank(ATR_14 in 60d ATR history)
stop_mult = 2.5 if ATR_pctile < 25th else 3.0 if < 75th else 3.5
hard_stop = entry_price - stop_mult × ATR_14
```
Triggered: `price ≤ hard_stop`

**EXIT 2 — Trailing Stop (signal-aware ✅)**
Time-based base multiplier:
```
days ≤ early: trail_base = trail_early_atr
days ≤ mid:   trail_base = trail_mid_atr
days ≤ late:  trail_base = trail_late_atr
else:         trail_base = trail_final_atr
```
Profit tightener:
```
profit > 4 ATR: p = 1.0
profit > 2 ATR: p = 1.5
profit > 1 ATR: p = 2.0
else:           p = 99.0
base = min(trail_base, p)
```
Signal modifier:
```
signal_strength = (composite + health) / 2
modifier = 1.3 if > +0.3 else 0.75 if < -0.3 else 1.0
trail_price = high_water - (base × modifier × ATR)
```

**EXIT 3 — Thesis Invalidation (regime-scaled ✅)**
```
RISK_ON:  comp < -2.0 AND pnl < -5%
NEUTRAL:  comp < -1.5 AND pnl < -5%
RISK_OFF: comp < -2.0 AND pnl < -5%
CRISIS:   comp < -2.5 AND pnl < -5%
```

**EXIT 4 — Momentum Break**
2 consecutive closes below 8-EMA while position is profitable.

**EXIT 5 — Leveraged ETF Cap**
3× ETF: max 3 days. 2× ETF: max 10 days.

**EXIT 6 — Portfolio Heat**
Portfolio unrealized P&L < -max_portfolio_drawdown → exit weakest position (by composite).

**EXIT 7 — Time Decay**
Held ≥ 12 days AND pnl < -1% AND price flat (5d OR 20d return within ±2%) AND comp < 0 AND health < 0.

### 9.3 Exit Ruleset — MEAN_REVERSION Positions

**EXIT 1 — Profit Target**
`price ≥ 20-day SMA` → exit. This is the primary exit for MR — reversion to mean is the thesis.

**EXIT 2 — Hard Stop (tighter)**
`hard_stop = entry_price - 1.5 × ATR_14`
MR trades have a defined thesis: either the panic reverses within a few days or the stop-out confirms the thesis was wrong. Tight stop preserves capital.

**EXIT 3 — Time Stop**
Held ≥ 5 days → forced exit regardless of P&L.
*Rationale: mean reversion either happens quickly (1–5 days) or doesn't happen at all. A 10-day MR hold is a failed thesis being kept alive.*

**EXIT 4 — Thesis Invalidation**
`comp < -1.5 AND pnl < -3%` (tighter PnL threshold than momentum).

### 9.4 Math Trim (both books)

After evaluating all exit conditions, reads `hold_health.json`. Positions with trim recommendations (TRIM_MINOR/MODERATE/MAJOR/EXIT) are partially or fully sold. Trim capped at `min(trim_shares, total_shares - 1)`.

### 9.5 Post-Exit Recording

1. `ledger.record_exit(trade_type, exit_reason, price, date)`
2. `trim_log.json` updated with math reasoning + agent cross-reference
3. `outcome_tracker.run_tracker()` → `outcome_log.json`
4. Cooldown added for hard_stop / thesis_invalid exits

---

## 10. The Feedback Loop — Learning from Outcomes

**Files:** `outcome_tracker.py`, `outcome_log.json`

### 10.1 Data Flow

```
exit_monitor → ledger.record_exit() → outcome_tracker.run_tracker()
     ↓
outcome_log.json: symbol, entry/exit dates and prices, realized PnL,
                  exit_path, trade_type, pattern_signal, book_conviction,
                  entry_decision (EntryAgent), hold_decision (HoldAgent)
```

### 10.2 Per-Book Learning

Each outcome record includes `trade_type`. This enables:
- Separate win rate, expectancy, profit factor per book
- Book-specific IC calibration for AdaptiveWeights
- Identifying which book is generating alpha and which is not

### 10.3 Downstream Uses

1. **Kelly recalibration:** `outcome_log.json` feeds `f*` calculation
2. **Adaptive weights:** factor z-scores + realized return → Ridge regression
3. **Agent calibration (planned):** 30+ tagged trades → `prompt_calibrator.py`
4. **Cooldown trigger:** hard_stop / thesis_invalid → `cooldown_log.json`

---

## 11. Adaptive Weight System

**File:** `signals.py` → `AdaptiveWeights`
**Storage:** `adaptive_weights.json`

### 11.1 Two Learning Layers

**Layer 1 — Ridge Regression (activates at 30+ trades)**
```
y = X @ beta + epsilon
beta = solve(X.T @ X + λI, X.T @ y)   (λ = 1.0)
alpha = min(30%, 30% × (N-30)/60)
final_weight = (1-alpha) × base + alpha × ridge_weight
```

In v5.5, ridge weights are computed separately per book — MOMENTUM factors are trained only on MOMENTUM trade outcomes, and vice versa.

**Layer 2 — IC Boost (activates at 20+ trades)**
```
IC[fn] = fraction of trades where sign(z[fn]) == sign(return) - 0.5
weight[fn] × (1 + IC[fn])
```
Computed once per scan, cached. Per-book: MOM trades calibrate MOM factors; MR trades calibrate MR factors.

---

## 12. Agent Layer — LLM Advisory

**File:** `agent_layer.py`
**Model:** Ollama/llama3.2 (local, private)
**Timeout:** 45s per call, 5s health ping

### 12.1 Three Agent Roles

| Agent | When | Output |
|-------|------|--------|
| MarketAgent | Pre-market | SCAN/REDUCE/STANDBY → market_decision.json |
| EntryAgent | Per signal | PASS/VETO → entry_vetoes.json |
| HoldAgent | Each monitor run | HOLD/TRIM/EXIT → hold_decisions.json (advisory only) |

### 12.2 What Agents Do and Don't Do

**Do:** Natural language reasoning. Record decisions for calibration. EntryAgent can veto entries.
**Don't:** Execute trades. Override math exits. Set stops. Choose sizes.

HoldAgent skips Ollama entirely when `days_history < 5` — writes HOLD directly. Fast-fail passthrough if Ollama unreachable.

### 12.3 Agent Awareness of Trade Type

In v5.5, agent prompts include `trade_type` so the agent reasons about the trade in the correct context — a MR trade being held for 4 days is normal; a momentum trade being held for 4 days may need different commentary.

---

## 13. Daily Schedule and File Topology

### 13.1 Daily Schedule

| Time ET | Script | Action |
|---------|--------|--------|
| 9:00 AM | `macro_context.py` | FRED + SPY → macro_context.json |
| 9:15 AM | `market_agent.py` | SCAN/REDUCE/STANDBY → market_decision.json |
| 9:30 AM | `Start_Intraday_Monitor.bat` | Launches 30-min exit+hold loop |
| 9:35 AM | `main.py` | Dual-book signal engine → BUY orders → per-book logs |
| 9:35–3:50 PM | `exit_monitor.py` + `hold_monitor.py` | Every 30 min via intraday loop |
| 3:50 PM | `Start_Afternoon_Monitor.bat` | Final exit+hold + recap email |
| 4:30 PM | `daily_recap.py` | Standalone recap at closing prices |

**Key scheduling rule:** MR positions are checked every 30 minutes because reversion can complete intraday. Momentum positions could tolerate end-of-day checks but benefit from intraday health monitoring.

### 13.2 Key Files

| File | Contents | Written by | Read by |
|------|----------|-----------|---------|
| `macro_context.json` | Regime label, signal scores, breadth | macro_context.py | market_agent, agent_layer |
| `market_decision.json` | SCAN/REDUCE/STANDBY | market_agent.py | main.py |
| `position_ledger.json` | Entry/exit metadata incl. trade_type, pattern, conviction | main.py, exit_monitor | hold_monitor, exit_monitor, recap |
| `hold_health.json` | Today's 10-layer scores + trim recs | hold_monitor.py | exit_monitor, recap |
| `hold_history.json` | Full daily trajectory | hold_monitor.py | hold_monitor (velocity, entropy) |
| `hold_decisions.json` | HoldAgent advisory decisions | agent_layer.py | exit_monitor (log only) |
| `entry_vetoes.json` | EntryAgent PASS/VETO | agent_layer.py | main.py |
| `outcome_log.json` | Closed trade records + exit_path + trade_type | outcome_tracker | main.py (cooldown), AdaptiveWeights |
| `trim_log.json` | Partial trims + math reasoning | exit_monitor | recap |
| `adaptive_weights.json` | Ridge beta + IC cache | AdaptiveWeights | AdaptiveWeights |
| `backtest_universe.txt` | 181 locked symbols | generate_backtest_universe.py | backtest.py |

---

## 14. Known Gaps and Open Problems

*Status: ✅ Done | 📋 Queued | 🔴 Blocked*

### 14.1 Closed Gaps

| Gap | Fix |
|-----|-----|
| ✅ GAP 1 | Signal-aware trailing stop (composite + health modifier) |
| ✅ GAP 3 | Vol-regime hard stop (2.5/3.0/3.5× ATR by 60d percentile) |
| ✅ GAP 4 | Regime-scaled thesis invalidation |
| ✅ GAP 5 | Composite velocity Kelly modifier (±20%) |
| ✅ GAP 6 | Re-entry cooldown (5-day hard_stop/trail_loss block) |
| ✅ GAP G | Sector breadth 50/150/200MA (Zweig 1986) |
| ✅ GAP H | margin_guard 4 bugs (fail-closed, on-margin reduce, dead code, sentinel) |
| ✅ Layer 9 | Anchored VWAP distance (5% weight) |
| ✅ Layer 10 | Shannon entropy trend (4% weight) |
| ✅ P2-7 | OBV rolling normalization — self-calibrating |
| ✅ P2-8 | Volatility layer continuous — linear interpolation |
| ✅ P2-9 | Stop dist zero = -1.0 (not 0.0 neutral) |
| ✅ P1-15 | Sentiment pipeline removed — zero alpha |
| ✅ UNIVERSE | Locked backtest_universe.txt — reproducible backtests |
| ✅ DUAL-BOOK | Single composite replaced with MomentumSignalEngine + MeanReversionSignalEngine |
| ✅ PATTERNS | BottomTopDetector — 10 Bulkowski-validated patterns + RSI divergence |

### 14.2 Queued — Next Session

**📋 SIM FIDELITY — Backtest exit checks run every bar (CRITICAL)**
Exit monitor runs every 30 minutes live. Backtest checks exits on every bar. This makes backtest results non-reproducible in live trading — positions that exit mid-day in the backtest would survive until the next 30-min check live. Fix: backtest should only check exits at simulated 30-minute intervals (9:35, 10:05, 10:35... 3:50 ET) matching the live schedule.

**📋 MR EXIT SPLIT — exit_monitor.py not yet split by trade_type**
The MR-specific exit rules (1.5× ATR stop, 5-day time_stop, profit_target at 20d SMA) are designed but not yet implemented in exit_monitor.py. Currently all positions use momentum-style exit logic. This must be built and backtested independently.

**📋 HOLD MONITOR SPLIT — health scoring not book-aware**
Hold monitor scores all positions identically. MR positions should score on MR factors (RSI recovery, distance to mean, panic subsiding). Momentum positions should score on momentum factors (EMA stack intact, RS holding, MACD not rolling over). Factor agreement (FAR) layer especially needs book-specific factor set.

**📋 GAP 2 — Conviction-scaled Kelly**
`kelly = f_min + (f_base - f_min) × book_conviction`
Currently: velocity modifier only. Book_conviction from CompositeRanker is available — compose with velocity modifier.

**📋 GAP B — Bayesian Kelly from outcome data**
0.02/0.12 caps and t/3.0 normalization are hand-picked. Derive from `outcome_log.json` once 30+ clean records exist.

**📋 GAP C — OU theta hold target**
`hold_target = ceil(log(2)/theta)` per stock from 30-day rolling OLS.
High-vol stocks currently get longer holds — should be shorter. (Leung & Zhang 2019)

**📋 GAP F — Universe filter sensitivity sweep**
All four live-screen thresholds ($5, 500K vol, $20M ADV, 1% range) are hand-picked. Parameter sweep ±50% on each.

### 14.3 Gated — Needs Data

**🔴 GAP A — Kalman regime classifier**
Continuous weighted score + Kalman + hysteresis is the target. Do not implement until vote-count stability has been profiled. HMM overfits — do not use.

**🔴 Hold monitor layer weight calibration**
Spearman IC of each layer against realized PnL over rolling 60-day window. Requires 60+ clean outcome records. Currently ~0 clean records.

**🔴 Layer 3 (prompt_calibrator.py)**
Do not start until 30+ agent-tagged trades in outcome_log.json.

### 14.4 Simulation Fidelity Mandate

The backtest must simulate exactly what the live system does. Any divergence is architectural slippage — invisible and systematic — more damaging than price slippage.

| Live system | Backtest (current) | Status |
|-------------|-------------------|--------|
| Exits checked every 30 min | Exits checked every bar | ❌ Not fixed |
| Entry fills at next-day open | Entry fills at next-day open | ✅ |
| MR exits use tight trail + 5d cap | All exits use momentum ruleset | ❌ Not fixed |
| Universe = live screen (~120–150 sym) | Universe = backtest_universe.txt (181) | ✅ |
| GAP1 uses real composite + health | GAP1 uses entry composite_score (proxy) | ⚠ Acceptable proxy |

---

*This document describes Raptor v5.5 as of 2026-05-23. Authoritative implementation: github.com/stevefirwin-svg/Raptor*
*Sections marked ⚠ describe intended architecture. Sections marked ✅ are implemented. No marker = matches code exactly.*
