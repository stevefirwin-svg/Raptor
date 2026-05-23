# Raptor Trading System — Master Skill & Architecture Ontology
*Last updated: 2026-05-23 | Version: 5.5*

---

## 1. SYSTEM IDENTITY

**Raptor v5.5** — dual-book quantitative swing trading system, Alpaca paper account (~$105K equity).
**Signal Books:** MOMENTUM (trend continuation) + MEAN_REVERSION (panic exhaustion). Separate logic, separate logs.
**Viper v2.0** — separate options engine, same Alpaca account, isolated logic.
**Agent Layer** — Ollama/llama3.2 advisory only. Math executes. Agents calibrate.
**Goal** — live trading performance. Backtest improvement is necessary but not sufficient.

---

## 2. ARCHITECTURE ONTOLOGY

### 2.1 — System Layers

```
┌─────────────────────────────────────────────────────────────┐
│  LAYER 4: SESSION GATE                                      │
│  market_agent.py → market_decision.json                     │
│  SCAN / REDUCE / STANDBY + risk_scalar                      │
├─────────────────────────────────────────────────────────────┤
│  LAYER 3: MACRO CONTEXT                                     │
│  macro_context.py → macro_context.json                      │
│  RISK_ON / NEUTRAL / RISK_OFF / CRISIS                      │
├─────────────────────────────────────────────────────────────┤
│  LAYER 2: DUAL-BOOK SIGNAL ENGINE (v5.5)                    │
│  universe_builder.py → signals.py                           │
│  ├─ MomentumSignalEngine    (8 factors, trend gate)         │
│  ├─ MeanReversionSignalEngine (7 factors, oversold gate)    │
│  ├─ BottomTopDetector       (10 Bulkowski patterns + RSI    │
│  │                           divergence)                    │
│  └─ CompositeRanker         (unified conviction ranking)    │
│  Signal.trade_type = MOMENTUM | MEAN_REVERSION              │
│  Signal.pattern_signal = detected candlestick/divergence    │
├─────────────────────────────────────────────────────────────┤
│  LAYER 1: EXECUTION                                         │
│  main.py (entries) + exit_monitor.py (exits/trims)          │
│  Separate logs: raptor.momentum / raptor.mean_reversion     │
│  margin_guard.py gates by capital utilization               │
│  ledger.py tracks positions (trade_type in metadata)        │
├─────────────────────────────────────────────────────────────┤
│  LAYER 0: POSITION HEALTH                                   │
│  hold_monitor.py → hold_health.json + hold_history.json     │
│  8-layer math scoring → compute_trim() → math trim orders   │
│  Exit ruleset differs by trade_type (MR: tight, MOM: wide)  │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 — Signal Engine Detail (v5.5)

**MomentumSignalEngine**
- Factors: ma_stack, adx_dir, rel_strength, obv_r2, accum_dist, price_cloud, vol_ratio
- Removed 2026-05-23: macd_accel (IC=−0.34, t=−3.42 — significant negative predictor)
- Gates: (Hurst H > 0.52 OR ADX > 22) AND price > 50 EMA AND rel_strength ≥ -0.01
- Entry timing: pullback to 8/21 EMA on declining volume
- Suppresses entry on: bearish_engulfing, evening_star, three_black_crows
- Stop: rcfg.initial_stop_atr_mult (wide). Hold target: 15 days. No fixed take-profit.

**MeanReversionSignalEngine — SUSPENDED (IC validation failed 2026-05-23)**
- Factors: rsi_mr, bollinger_z, crowd_panic, ma_distance, bb_squeeze, rev_momentum, atr_pctile
- IC study (94 obs): ma_distance=−0.54★, atr_pctile=−0.44★, bb_squeeze=−0.39★, bollinger_z=−0.31★
- All significant factors negative — book is selecting downtrends, not reversals
- Code preserved. Gate lifted when rolling IC turns positive across ≥2 factors
- Lift condition: ma_distance IC > +0.05 AND t-stat > 1.5 over 60+ observations

**BottomTopDetector** — Bulkowski (2008) validated patterns >60% reversal rate:
- Bottom: hammer (60.4%), bullish_engulfing (63.0%), morning_star (61.5%), piercing_line (62.1%), three_white_soldiers (65.4%), rsi_bull_divergence
- Top: shooting_star (60.8%), bearish_engulfing (60.6%), evening_star (61.8%), dark_cloud_cover (62.3%), three_black_crows (65.1%), rsi_bear_divergence

**CompositeRanker** — Grinold & Kahn: allocation proportional to conviction. No fixed per-book quotas. Each book's scores normalized to [0,1] within-book, then unified sorted ranking.

### 2.3 — Daily Execution Schedule

```
9:00 AM  macro_context.py → macro_context.json
9:15 AM  market_agent.py  → market_decision.json (SCAN/REDUCE/STANDBY)
9:30 AM  Start_Intraday_Monitor.bat launches (loops every 30 min until 3:50 PM)
9:35 AM  main.py — entry scan
         ├─ MomentumSignalEngine + MeanReversionSignalEngine score universe
         ├─ CompositeRanker unifies by conviction
         ├─ EntryAgent veto screen
         ├─ submit_order(BUY) + ledger.record_entry(trade_type in metadata)
         └─ logs: raptor.momentum or raptor.mean_reversion per trade type
9:35–3:50 PM  exit_monitor.py + hold_monitor.py every 30 min
              MR positions: tight trail, 5-day cap, target=20d SMA
              MOM positions: wide trail, momentum_break path
3:50 PM  daily_recap.py — email with per-book summary
```

---

## 3. FILE ONTOLOGY

### 3.1 — Code Files

| File | Role | Key Output |
|------|------|-----------|
| `config.py` | All parameters — single source of truth | — |
| `data_feeds.py` | Alpaca bars, FRED macro, order submission | — |
| `universe_builder.py` | Screens ~6800 assets → tradeable symbols | `cache/universe/` |
| `signals.py` | Dual-book engine: MOM + MR + BottomTopDetector | `_last_full_signals` |
| `main.py` | Entry scanner, BUY orders, per-book logging | `position_ledger.json` |
| `exit_monitor.py` | All exit/trim logic, trade_type-aware ruleset | `trim_log.json` |
| `hold_monitor.py` | 8-layer health scoring, rolling trend components | `hold_health.json` |
| `agent_layer.py` | EntryAgent + HoldAgent Ollama wrappers (advisory) | `entry_vetoes.json` |
| `market_agent.py` | Session gate | `market_decision.json` |
| `macro_context.py` | FRED macro regime classifier | `macro_context.json` |
| `margin_guard.py` | Capital utilization gate | — |
| `ledger.py` | Position tracking open/closed | `position_ledger.json` |
| `outcome_tracker.py` | Tags closed trades with agent decisions | `outcome_log.json` |
| `daily_recap.py` | Dark-theme HTML email — per-book analytics | — |
| `backtest.py` | Walk-forward backtester, per-book P&L report | `backtest_results/` |
| `backtest_universe.txt` | LOCKED 181-symbol universe for reproducible backtests | — |
| `generate_backtest_universe.py` | One-time script to regenerate backtest_universe.txt | — |
| `options_engine.py` | Viper v2.0 — 3 options strategies | `logs/viper_*.csv` |

### 3.2 — JSON State Files

| File | Writer | Reader | Resets |
|------|--------|--------|--------|
| `macro_context.json` | macro_context.py 9:00 AM | market_agent, agent_layer | Daily |
| `market_decision.json` | market_agent.py 9:15 AM | main.py | Daily |
| `hold_health.json` | hold_monitor.py | exit_monitor.py, daily_recap | Each run |
| `hold_history.json` | hold_monitor.py | hold_monitor (trajectory) | Append-only |
| `hold_decisions.json` | agent_layer (HoldAgent) | exit_monitor (log only) | Append-only |
| `entry_vetoes.json` | agent_layer (EntryAgent) | outcome_tracker | Append-only |
| `position_ledger.json` | main.py + exit_monitor | exit_monitor, hold_monitor, recap | Grows |
| `adaptive_weights.json` | signals.AdaptiveWeights | signals | Per closed trade |
| `outcome_log.json` | outcome_tracker | prompt_calibrator (planned) | Append-only |
| `trim_log.json` | exit_monitor | prompt_calibrator (planned) | Append-only |

### 3.3 — Log Files

| File | Writer | Purpose |
|------|--------|---------|
| `raptor_YYYYMMDD.log` | main.py | Entry scan: signals, orders, vetoes |
| `exits_YYYYMMDD.log` | exit_monitor.py | Exit decisions, trail prices, trims |
| `momentum_YYYYMMDD.log` | main.py (raptor.momentum logger) | MOMENTUM trades only |
| `mr_YYYYMMDD.log` | main.py (raptor.mean_reversion logger) | MEAN_REVERSION trades only |
| `auto_start.log` | bat files | Task scheduler timestamps |

---

## 4. DATA FLOW

### 4.1 — Signal Pipeline (v5.5)

```
Alpaca API → bars per symbol
    │
    ▼
signals._compute_raw() — all 15 factors + intermediates per symbol
    │
    ▼
_crosssectional_z() — MAD-robust z-score per factor across universe
    │
    ├─→ MomentumSignalEngine.score() per symbol
    │   Gate: trend confirmed + above 50 EMA + positive RS
    │   BottomTopDetector.detect_top() — suppress on exhaustion
    │   Returns scored momentum candidate or None
    │
    ├─→ MeanReversionSignalEngine.score() per symbol
    │   Gate: RSI(5)<35 + below BB + panic volume + bottom pattern
    │   BottomTopDetector.detect_bottom() — REQUIRED for entry
    │   Returns scored MR candidate or None
    │
    └─→ CompositeRanker.rank() — unified conviction list
        Signal.trade_type = MOMENTUM | MEAN_REVERSION
        Signal.pattern_signal = pattern name or ""
        Signal.book_conviction = normalized [0,1]
        self._last_full_signals ← ALL scored symbols
        Returns top-N Signal objects
```

### 4.2 — Exit Logic by Trade Type

```
MomentumSignalEngine exits (exit_monitor.py):
    EXIT 1: hard_stop        price ≤ entry - 3.0×ATR
    EXIT 2: trail_loss       price ≤ high_water - trail×ATR (wide, signal-aware)
    EXIT 3: trail_profit     trail tightens when profit > 2×ATR
    EXIT 4: thesis_invalid   comp < -1.5 AND pnl < -5%
    EXIT 5: momentum_break   2 closes below 8-EMA while profitable
    EXIT 6: portfolio_heat   portfolio_dd < -12%
    EXIT 7: time_decay       flat AND losing AND held ≥ 10 days

MeanReversionSignalEngine exits (exit_monitor.py):
    EXIT 1: profit_target    price ≥ 20-day SMA (take-profit target)
    EXIT 2: hard_stop        price ≤ entry - 1.5×ATR (tighter)
    EXIT 3: time_stop        held ≥ 5 days (MR positions must resolve)
    EXIT 4: thesis_invalid   comp < -1.5 AND pnl < -3%

Math trim (hold_monitor → exit_monitor):
    compute_trim() → TRIM_MINOR/MODERATE/MAJOR/EXIT
    Applies to both books. EXIT → full exit. TRIM → partial sell.
```

---

## 5. AGENT ONTOLOGY

| Agent | Executes | Purpose | Output |
|-------|----------|---------|--------|
| MarketAgent | Rule-based SCAN/REDUCE/STANDBY | Session gate | market_decision.json |
| EntryAgent | VETO blocks orders | Structural risk filter | entry_vetoes.json |
| HoldAgent | Nothing (advisory only) | Calibration data | hold_decisions.json |

- Model: llama3.2 (3.2B). Timeout: 45s. Health ping: 5s `_ollama_alive()`.
- Fast-fail passthrough if Ollama unreachable.
- HoldAgent skips Ollama if `days_history < 5` — writes HOLD directly.
- **Math governs execution. Agents advise.**

---

## 6. TASK SCHEDULER

| Time ET | Task | Script | Action |
|---------|------|--------|--------|
| 9:00 AM | Raptor_MacroContext | macro_context.py | FRED + SPY → macro_context.json |
| 9:15 AM | Raptor_MarketAgent | market_agent.py | SCAN/REDUCE/STANDBY |
| 9:30 AM | Raptor Intraday Monitor | Start_Intraday_Monitor.bat | exit + hold monitor loop every 30 min |
| 9:35 AM | Raptor Bot | Start_Entry.bat → main.py | Entry scan + BUY orders |
| 3:50 PM | Raptor Afternoon Monitor | Start_Afternoon_Monitor.bat | exit + hold + recap email |
| 4:30 PM | Raptor_DailyRecap | Start_Recap.bat | Recap at closing prices |

**Intraday Monitor:** `Start_Intraday_Monitor.bat` loops `exit_monitor.py` + `hold_monitor.py` every 30 min, 9:35–3:50 PM ET, self-terminates. Registered via `Register_Intraday_Monitor.ps1`.

---

## 7. BACKTEST RULES

### 7.1 — Locked Universe
`backtest_universe.txt` — 181 symbols, fixed. DO NOT use live universe cache for backtesting. Regenerate only with `generate_backtest_universe.py` and document the reason in the git commit.

### 7.2 — Reproducibility
```powershell
python backtest.py --no-gap1    # baseline (GAP1 disabled)
python backtest.py              # current model
```
Both runs use `backtest_universe.txt`. Results are reproducible across sessions.

### 7.3 — Known Baselines
| Run | Universe | Return | Sharpe | PF | Notes |
|-----|----------|--------|--------|-----|-------|
| no-GAP1, 70 sym (live cache) | 70 live | 253.6% | 1.412 | 1.98 | Non-reproducible — cache artifact |
| no-GAP1, 181 sym locked | 181 locked | 57.7% | 0.473 | 1.34 | **True baseline — underperforms SPY** |
| GAP1 active, 181 sym | 181 locked | TBD | TBD | TBD | Run after v5.5 signals.py |

**The model currently underperforms SPY on a broad universe. This is the problem being solved.**

### 7.4 — Root Cause (diagnosed 2026-05-23)
1. Old signals.py blended momentum AND mean-reversion factors in one composite — they cancel on a broad universe
2. GAP1 trail modifier bucketed 99.8% of trades as "Strong" — every trade got 1.3x trail width regardless of signal quality
3. Live universe cache produced non-reproducible results — inflated prior backtests
4. Regime classifier output NEUTRAL/MIXED for 100% of trades — micro-regime gate was never firing

### 7.5 — Simulation Fidelity (non-negotiable)
Backtest exit checks must match live monitor frequency. If `exit_monitor.py` runs every 30 min, backtest checks exits at 30-min intervals only — not every bar. **This is not yet implemented — next priority after v5.5 validation.**

---

## 8. PERFORMANCE STANDARDS

### 8.1 — Target Metrics (live trading)
- Sharpe > 1.5, Sortino > 2.5
- Max drawdown < 15%
- Profit factor > 1.8
- Win rate > 45% (acceptable with Avg Win / Avg Loss ≥ 2:1)
- trail_loss < 50% of exits (currently 55% — target improvement via MR book)

### 8.2 — Per-Book Targets
- **MOMENTUM:** High win rate (>50%), large avg win, wide trail exits dominant
- **MEAN_REVERSION:** Lower win rate acceptable (45%), small avg loss (tight stop), profit_target dominant exit

---

## 9. CRITICAL RULES

1. **Dual-book architecture is the foundation.** Never blend MOM and MR factors into one composite.
2. **MR book is SUSPENDED.** IC data shows all significant MR factors predict losses. Do not lift gate without IC evidence (ma_distance IC > 0.05, t > 1.5, n ≥ 60).
3. **Backtest universe = backtest_universe.txt.** Never use live cache for analysis.
4. **str_replace for edits, create_file for new files only.**
5. **Backtest both books independently before combining** — validate each has edge on its own.
6. **Ledger must match Alpaca.** Resync: `python backfill_ledger.py --write`
7. **Exit path labels:** hard_stop, trail_profit, trail_loss, profit_target, momentum_break, thesis_invalid, portfolio_heat, time_decay, time_stop, math_exit, math_trim_X%
8. **Clear pycache before every test:** `Remove-Item -Recurse -Force __pycache__`
9. **Never use defaults in agent context.** Missing data → skip position entirely.
10. **Math trim governs execution.** HoldAgent is advisory only.
11. **signals.py must set self._last_full_signals** before top-N filter.
12. **Use account["buying_power"] not account["cash"]** for capital checks.
13. **exit_monitor never calls Ollama.** Reads hold_decisions.json advisory only.
14. **Do NOT start Layer 3** until 30+ agent-tagged trades in outcome_log.json.
15. **PowerShell only** (not CMD).
16. **Read file + callers + shared utilities before any edit.**

---

## 10. IC VALIDATION FINDINGS (2026-05-23, 94 observations)

Factor IC measured via Spearman correlation against realized returns (hold_history + outcome_log, Option C weighted).

**Momentum factors — keep:**
| Factor | IC | t-stat | Status |
|--------|----|--------|--------|
| adx_dir | +0.44 | +4.68 | ✅ Strong |
| ma_stack | +0.33 | +3.34 | ✅ Strong |
| accum_dist | +0.20 | +1.94 | ✅ Marginal |
| price_cloud | +0.19 | +1.83 | ✅ Marginal |
| rel_strength | +0.09 | +0.86 | ⚠ Weak — watch |
| obv_r2 | +0.06 | +0.61 | ⚠ Weak — watch |
| vol_ratio | −0.05 | −0.45 | ⚠ Noise — watch |

**Momentum factors — removed:**
| Factor | IC | t-stat | Action |
|--------|----|--------|--------|
| macd_accel | −0.34 | −3.42 | ❌ Removed — significant negative predictor |

**MR factors — all negative (book suspended):**
| Factor | IC | t-stat | Note |
|--------|----|--------|------|
| ma_distance | −0.54 | −6.15 | Strongest signal — predicts losses |
| atr_pctile | −0.44 | −4.70 | |
| bb_squeeze | −0.39 | −3.74 | |
| bollinger_z | −0.31 | −3.07 | |
| rsi_mr | −0.00 | −0.02 | Not significant |
| crowd_panic | +0.03 | +0.25 | Not significant |
| rev_momentum | −0.05 | −0.46 | Not significant |

**Factor covariance condition number: 271.8** — HIGH COLLINEARITY. Orthogonalization active and critical.

**Kelly Engine (shadow mode, 73 equity trades):**
- μ = 3.49%, σ = 25.90%, Win rate = 47.9%, Sharpe = 0.135
- DD-constrained Kelly recommendation: **3.65% per trade**
- Active mode at 100 trades. Current sizing range 2–12% — positions above 5–6% are above data-supported levels.

---

## 11. WHAT NOT TO DO

- Don't blend MOM and MR factors into one composite score — proven to cancel
- Don't use live universe cache for backtesting — non-reproducible
- Don't add hand-picked thresholds without distributional justification
- Don't propose HMM regime detection — overfits
- Don't parallelize Ollama calls — causes timeouts
- Don't set default composite to -1.0 for unscored symbols — use 0.0
- Don't remove _last_full_signals from signals.py
- Don't use market_value from Alpaca positions — field doesn't exist (use qty × price)
- Don't add Ollama calls to exit_monitor
- Don't re-introduce the single-composite GAP1 implementation (confirmed regression)

---

## 12. MATH-FIRST PRINCIPLES

Every decision — buy, hold, trim, exit, size, weight — must be derived from mathematics, not intuition or round numbers.

**Before any value is chosen, answer:**
1. What mathematical framework governs this?
2. What does empirical research say?
3. Can this be derived from existing data (hold_history.json, backtest results, signal ICs)?
4. Is this an optimization output or a guess?

**Key derivation standards:**
- **Weights** → IC mean / ICIR over rolling 60–90 day window from trade data (Spearman rank IC)
- **Thresholds** → distributional percentile cutoffs, not fixed constants
- **Trim %** → Kelly criterion: `trim_pct = 1 - (current_kelly / entry_kelly)`
- **Trail multiplier** → OU mean-reversion speed (theta) per stock (Leung & Zhang 2019)
- **Position size** → conviction-scaled Kelly: `size = base_kelly × composite_percentile_rank`
- **Hard stop** → volatility-regime adjusted: `stop_mult = base × atr_percentile_scalar`

**Research domains (draw from before proposing any change):**
- Optimal stopping: Bertsimas & Lo (1998), Leung & Zhang (2019), Baviera (2017)
- Factor investing: Grinold & Kahn, Fama-French, AQR (Asness et al.)
- Momentum: Jegadeesh & Titman (1993), Asness, Moskowitz & Pedersen (2013)
- Mean reversion: De Bondt & Thaler (1985), Leung & Li (2015)
- Candlestick reliability: Bulkowski (2008)
- Behavioral finance: Kahneman, Thaler, Wilder (1978)
- Market microstructure: Kyle (1985), Glosten-Milgrom
- Risk: Kelly (1956), Markowitz, Black-Litterman

---

## 13. MATH GAPS — OPEN ITEMS

| Gap | File | Problem | Status |
|-----|------|---------|--------|
| GAP 2 | signals.py | Entry sizing ignores conviction gradient | Open |
| GAP 3 | exit_monitor.py | Hard stop fixed, not volatility-regime aware | Open |
| GAP 4 | exit_monitor.py | Thesis invalidation threshold static | Open |
| GAP 5 | signals.py | No momentum acceleration on entry | Open |
| GAP 6 | main.py | No re-entry cooldown after stop-out | Open |
| GAP 7 | exit_monitor.py | Portfolio heat exit too blunt | Open |
| GAP 9 | signals.py | Universe scored once/day only | Open |
| SIM FIDELITY | backtest.py | Exit checks run every bar not every 30 min | **Next priority** |
| MR EXIT RULES | exit_monitor.py | MR-specific tight trail + 5-day cap not yet split by trade_type | **Next priority** |
| HOLD MONITOR | hold_monitor.py | Health scoring not yet split by trade_type | Pending |

---

## 14. SESSION PROTOCOLS (MANDATORY)

### Start of every conversation
1. Read RAPTOR_SKILL.md via project_knowledge_search before any technical work
2. Load userMemories for current state
3. Never assume prior session context without searching

### After every file change (no prompting required)
1. Verify syntax
2. `present_files` automatically
3. Post exact git commands:
```bash
git pull origin main
git add -A
git commit -m "describe what changed"
git push origin main
```

### GitHub reconciliation
- GitHub is source of truth
- Every session ends with a push
- Always `git pull origin main` before starting laptop work after a Claude session

### Ontology sync
Any architectural change MUST be reflected in Section 2 of this file in the same session.

---

## 15. PROACTIVE THINKING STANDARDS

Claude is a quantitative research partner, not a code generator.

**Pre-build checklist (answer before writing code):**
1. Does this exist in the live system at the right time and frequency?
2. Does this improve live edge or just the backtest number?
3. Does this add free parameters without distributional justification?
4. Can the marginal value be stated in one sentence grounded in theory?
5. What does this break — trace the data flow end to end?

**If the model's backtest PnL declines as features are added:**
1. Stop adding features
2. Strip to last known good baseline
3. Identify exactly which addition caused regression
4. Test one change at a time

**If Claude finds itself building something that improves the backtest without a clear live-trading mechanism — stop and say so before committing a line of code.**

---

## 16. INFRASTRUCTURE NOTES

- Task Scheduler edits require elevated (admin) PowerShell: `Set-ScheduledTask`
- Regular PowerShell returns "Access is denied" for scheduler tasks
- `Start_Afternoon_Monitor.bat` ExecutionTimeLimit = PT0S (unlimited) — previously PT15M caused kills before recap ran
- `daily_recap.py` dark theme: outer `<div style="background:#0a0a1a">` wraps inner 720px card — body tag is stripped by Gmail

---

## 17. LAYER 3 READINESS CHECK

```powershell
python -c "
import json, os
d = json.load(open('outcome_log.json'))
real = [t for t in d if t.get('entry_agent') not in [None, 'no_record']]
print(f'{len(real)} agent-tagged trades (need 30+)')
trims = json.load(open('trim_log.json')) if os.path.exists('trim_log.json') else []
print(f'{len(trims)} trim records')
"
```

Do NOT start Layer 3 (prompt_calibrator.py) until 30+ agent-tagged trades in outcome_log.json.
