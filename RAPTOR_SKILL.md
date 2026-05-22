# Raptor Trading System — Master Development Skill & Architecture Ontology
*Last updated: 2026-05-22*

---

## 1. SYSTEM IDENTITY

**Raptor v5.4** — quantitative swing trading system, Alpaca paper account (~$105K equity).
**Viper v2.0** — separate options engine, same Alpaca account, isolated logic.
**Agent Layer** — Ollama/llama3.2 local LLM, advisory + calibration. Never sole decision-maker.
**Goal** — fully autonomous adaptive system: math drives execution, agents learn from outcomes.

---

## 2. ARCHITECTURE ONTOLOGY

### 2.1 — System Layers (top to bottom)

```
┌─────────────────────────────────────────────────────────────┐
│  LAYER 4: SESSION GATE                                      │
│  market_agent.py → market_decision.json                     │
│  SCAN / REDUCE / STANDBY + risk_scalar                      │
│  Rule-based authoritative. LLM adds reasoning only.         │
├─────────────────────────────────────────────────────────────┤
│  LAYER 3: MACRO CONTEXT                                     │
│  macro_context.py → macro_context.json                      │
│  RISK_ON / NEUTRAL / RISK_OFF / CRISIS                      │
│  Injected into every agent prompt via _load_macro_summary() │
├─────────────────────────────────────────────────────────────┤
│  LAYER 2: SIGNAL ENGINE                                     │
│  universe_builder.py → signals.py → QuantSignalEngine       │
│  16 factors, 5 clusters, cross-sectional z-score            │
│  Outputs ranked Signal objects + _last_full_signals map     │
├─────────────────────────────────────────────────────────────┤
│  LAYER 1: EXECUTION                                         │
│  main.py (entries) + exit_monitor.py (exits/trims)          │
│  margin_guard.py gates entries by capital utilization       │
│  ledger.py tracks positions, data_feeds.py talks to Alpaca  │
├─────────────────────────────────────────────────────────────┤
│  LAYER 0: POSITION HEALTH                                   │
│  hold_monitor.py → hold_health.json + hold_history.json     │
│  8-layer math scoring → compute_trim() → math trim orders   │
│  HoldAgent logs decisions for calibration (advisory only)   │
├─────────────────────────────────────────────────────────────┤
│  LEARNING LAYER: OUTCOME COLLECTION                         │
│  outcome_tracker.py → outcome_log.json (full exits)         │
│  trim_log.json (partial trims with agent cross-ref)         │
│  hold_history.json (daily factor snapshots)                 │
│  entry_vetoes.json + hold_decisions.json (agent decisions)  │
│  → feeds Layer 3 prompt_calibrator.py (PLANNED)             │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 — Decision Tree (daily execution)

```
9:00 AM  macro_context.py
         │  Sector breadth: 50/150/200MA (Zweig 1986) ← UPDATED 2026-05-22
         └─→ macro_context.json
                 │
9:15 AM  market_agent.py reads macro_context.json
         Rule-based: CRISIS/VIX>35 → STANDBY
                     RISK_OFF/VIX>25 → REDUCE (scalar 0.5-0.75)
                     else → SCAN (scalar 1.0)
         LLM: adds reasoning string only, cannot override
         │
         └─→ market_decision.json
                 │
9:35 AM  main.py reads market_decision.json
         │
         ├─ STANDBY? → return immediately, no scan
         │
         ├─ margin_guard.check_margin_safety(dm)  ← UPDATED 2026-05-22
         │   util >90% → BLOCK (return, fail closed)
         │   util >85% OR on margin → cap new entries at 1
         │   util >75% → WARNING, proceed
         │   API error → BLOCK (fail closed, not open)
         │
         ├─ universe_builder.build() → ~130 symbols
         │
         ├─ signals.generate_signals()
         │   ├─ _raw() — compute 16 factors per symbol
         │   ├─ cross-sectional z-score (MAD-robust)
         │   ├─ inverse-vol weights × macro_mult × micro_mult
         │   ├─ adaptive ridge regression blend
         │   ├─ self._last_full_signals ← ALL scored symbols (before top-N)
         │   └─ returns top-N Signal objects (composite > 0)
         │
         ├─ filter out already-held symbols
         │
         ├─ GAP 6: cooldown filter ← NEW 2026-05-22
         │   _get_cooldown_symbols() checks outcome_log + position_ledger
         │   hard_stop/trail_loss within 5 days → symbol blocked
         │   trail_profit exits NOT blocked (thesis worked)
         │
         ├─ EntryAgent.evaluate_batch(candidates)
         │   ├─ _ollama_alive() ping — fast-fail if down
         │   ├─ numbered veto rules 1-6
         │   ├─ RISK_ON/NEUTRAL/BULLISH → explicit PASS
         │   └─ writes entry_vetoes.json
         │
         ├─ for each signal that passes:
         │   ├─ GAP 5: composite_velocity from hold_history.json ← NEW 2026-05-22
         │   │   velocity = composite_today - composite_3d_ago
         │   │   kelly_modifier = max(0.80, min(1.20, 1.0 + velocity × 0.2))
         │   │   effective_kelly = kelly_fraction × kelly_modifier
         │   ├─ size = my_equity × effective_kelly / entry_price
         │   ├─ buying_power check (95% of Alpaca buying_power)
         │   ├─ submit_order(BUY, limit, client_order_id)
         │   └─ ledger.record_entry(v5.4, symbol, shares, price, date,
         │       {stop, regime, t_stat, composite_score, composite_velocity, kelly_fraction})
         │
9:52 AM  exit_monitor.py (Morning Monitor)
3:50 PM  exit_monitor.py (Afternoon Monitor)
         │
         ├─ signals.generate_signals() → _last_full_signals
         │   scores = {sym: real_composite for all held symbols}
         │   default 0.0 for genuinely unscored (not -1.0)
         │
         ├─ for each position:
         │   ├─ EXIT 1: hard_stop — GAP 3 vol-regime aware ← UPDATED 2026-05-22
         │   │   ATR pctile < 25th → 2.5× ATR (low vol, tighter)
         │   │   ATR pctile 25-75th → 3.0× ATR (normal baseline)
         │   │   ATR pctile > 75th → 3.5× ATR (high vol, avoid whipsaw)
         │   ├─ EXIT 2: trailing_stop — GAP 1 signal-aware ← UPDATED 2026-05-17
         │   │   trail multiplier: f(days_held, profit_atr, composite, health)
         │   │   signal_strength = (composite + health) / 2
         │   │   strength > +0.3 → ×1.3 (wider, let winners run)
         │   │   strength < -0.3 → ×0.75 (tighter, protect profits)
         │   │   Thresholds to calibrate via calibrate_gap1.py after backtest
         │   ├─ EXIT 3: thesis_invalid — GAP 4 regime-scaled ← UPDATED 2026-05-22
         │   │   RISK_ON:  comp < -2.0 AND pnl < -5%
         │   │   NEUTRAL:  comp < -1.5 AND pnl < -5% (baseline)
         │   │   RISK_OFF: comp < -2.0 AND pnl < -5%
         │   │   CRISIS:   comp < -2.5 AND pnl < -5%
         │   ├─ EXIT 4B: leveraged_3x_cap — 3x ETF held > 3 days
         │   │           leveraged_2x_cap — 2x ETF held > 10 days
         │   └─ EXIT 5: time_decay — flat(5d OR 20d) AND losing AND
         │              composite < 0 AND health < 0 AND held ≥ 12 days
         │
         ├─ EXIT 4: portfolio_heat — portfolio_dd < -12%, exit weakest composite
         │
         ├─ MATH TRIM — reads hold_health.json
         │   compute_trim() result: TRIM_MINOR/MODERATE/MAJOR/EXIT
         │   ├─ EXIT action → full exit order
         │   └─ TRIM action → partial sell (trim_shares, capped at qty-1)
         │   writes trim_log.json with agent cross-reference
         │
         ├─ AGENT ADVISORY — reads hold_decisions.json (no Ollama calls here)
         │   logs HOLD/TRIM/EXIT decisions for calibration
         │   NEVER executes — math trim governs
         │
         ├─ submit_order(SELL, market, client_order_id=reason_symbol_timestamp)
         ├─ ledger.record_exit(v5.4, symbol, price, date, reason)
         └─ outcome_tracker.run_tracker() → tags closed trades → outcome_log.json

9:52 AM  hold_monitor.py (Morning Monitor)
3:50 PM  hold_monitor.py (Afternoon Monitor)
         │
         ├─ build_snapshot() per position
         │   ├─ price momentum (ROC trend over 3 snapshots), OBV trend, ATR
         │   ├─ Bollinger, volume ratios, factor_scores from _last_full_signals
         │   └─ stop_dist_atr from ledger metadata
         │
         ├─ compute_health_score(trajectory)
         │   8 layers: composite_slope, factor_agreement, price_momentum,
         │             cluster_health, volume, volatility, stop_distance, hold_duration
         │   → health score [-1, 1], tier: STRENGTHENING/STABLE/DECAYING/INSUFFICIENT_DATA
         │
         ├─ compute_trim(health_result, position)
         │   severity × stop_mult × FAR_mult × pnl_mult + slope_adj
         │   → trim_pct, trim_shares, action: HOLD/TRIM_MINOR/MODERATE/MAJOR/EXIT
         │
         ├─ saves hold_health.json (today's scores — read by exit_monitor)
         ├─ saves hold_history.json (full trajectory — daily composite snapshots)
         │
         └─ HoldAgent.evaluate_batch() [ADVISORY — calibration only]
             ├─ _ollama_alive() ping
             ├─ numbered decision rules 1-4
             ├─ days_history < 5 → HOLD always (short-circuit, saves 8min)
             └─ writes hold_decisions.json (append-only)

3:50 PM  daily_recap.py (via afternoon monitor)
4:30 PM  daily_recap.py (standalone task)
         └─ email: account summary, holdings, health monitor,
                   top signals, SPY benchmark, portfolio analytics,
                   exit reason breakdown, rolling 10-trade WR,
                   trim efficiency, agent vs math disagreement,
                   position composite Δ & regime intelligence ← ALL NEW 2026-05-22
```

---

## 3. FILE ONTOLOGY

### 3.1 — Code Files

| File | Role | Writes | Reads |
|------|------|--------|-------|
| `config.py` | All parameters — single source of truth | — | everything |
| `data_feeds.py` | Alpaca bars, FRED macro, news sentiment, order submission | — | Alpaca API, FRED API |
| `universe_builder.py` | Screens ~6800 assets → ~130 tradeable symbols | `cache/universe/` | Alpaca API |
| `signals.py` | 16-factor signal engine, z-scoring, Kelly sizing | `self._last_full_signals` | bars, macro |
| `main.py` | Entry scanner — sizes and submits BUY orders. GAP 5 velocity + GAP 6 cooldown. | `position_ledger.json`, `logs/raptor_DATE.log` | market_decision.json, outcome_log.json, hold_history.json |
| `exit_monitor.py` | All exit and trim logic — mechanical + math. GAP 1/3/4 applied. | `position_ledger.json`, `trim_log.json`, `logs/exits_DATE.log` | hold_health.json, hold_decisions.json, macro_context.json |
| `hold_monitor.py` | 8-layer position health scoring | `hold_health.json`, `hold_history.json`, `hold_decisions.json` | bars, signals |
| `agent_layer.py` | EntryAgent + HoldAgent Ollama wrappers | `entry_vetoes.json`, `hold_decisions.json`, `prompt_versions/` | macro_context.json |
| `market_agent.py` | Session gate — SCAN/REDUCE/STANDBY | `market_decision.json` | macro_context.json |
| `macro_context.py` | FRED macro regime classifier. 50/150/200MA breadth (Zweig). | `macro_context.json` | FRED API, yfinance (SPY + sector ETFs) |
| `margin_guard.py` | Capital utilization gate. Fail closed. On-margin → reduce. | — | Alpaca account |
| `ledger.py` | Position tracking open/closed by model version | `position_ledger.json` | — |
| `outcome_tracker.py` | Tags closed trades with agent decisions. Position ledger fallback for exit_path. | `outcome_log.json` | Alpaca orders, entry_vetoes.json, hold_decisions.json, position_ledger.json |
| `backfill_ledger.py` | One-time ledger population from Alpaca | `position_ledger.json` | Alpaca positions |
| `daily_recap.py` | Bloomberg-style HTML email. 10 new metrics added 2026-05-22. | `logs/recap_preview.html` | all JSON files, bars |
| `backtest.py` | Walk-forward backtester. Composite+health proxies for GAP 1 validation. | `backtest_results/` | bars |
| `calibrate_gap1.py` | GAP 1 trail modifier calibration from backtest trades.csv | `backtest_results/calibration_gap1.json` | backtest_results/trades.csv |
| `diagnose.py` | Signal diagnostics | — | bars, signals |
| `check_account.py` | Quick account viewer | — | Alpaca account |
| `options_engine.py` | Viper v2.0 — 3 options strategies | `logs/viper_*.csv` | bars, Alpaca |

### 3.2 — JSON State Files

| File | Written By | Read By | Purpose | Growth |
|------|-----------|---------|---------|--------|
| `macro_context.json` | macro_context.py (9:00 AM) | market_agent.py, agent_layer.py, exit_monitor.py | Daily macro regime + breadth + agent summary | Overwritten daily |
| `market_decision.json` | market_agent.py (9:15 AM) | main.py | SCAN/REDUCE/STANDBY + risk_scalar | Overwritten daily |
| `hold_health.json` | hold_monitor.py | exit_monitor.py, daily_recap.py | Today's 8-layer scores + stop_dist_atr | Overwritten each run |
| `hold_history.json` | hold_monitor.py | hold_monitor.py (trajectory), main.py (velocity) | Full daily composite snapshots per symbol | Append-only, grows forever |
| `hold_decisions.json` | hold_monitor.py (via agent_layer) | exit_monitor.py (advisory log only) | HoldAgent HOLD/TRIM/EXIT decisions | Append-only, grows forever |
| `entry_vetoes.json` | main.py (via agent_layer) | outcome_tracker.py | EntryAgent PASS/VETO decisions | Append-only, grows forever |
| `position_ledger.json` | main.py (entries), exit_monitor.py (exits) | exit_monitor.py, hold_monitor.py, daily_recap.py, outcome_tracker.py | Open positions + closed trade history | Grows with trades |
| `outcome_log.json` | outcome_tracker.py | prompt_calibrator.py (planned), main.py (cooldown check) | Closed trades tagged with agent decisions + exit_path | Append-only, grows forever |
| `trim_log.json` | exit_monitor.py | prompt_calibrator.py (planned), daily_recap.py | Partial trims + math reasoning + agent cross-ref | Append-only, grows forever |
| `adaptive_weights.json` | signals.py (AdaptiveWeights) | signals.py | Ridge regression + IC weights from closed trades | Updated on each closed trade |

### 3.3 — Log Files (logs/)

| File | Written By | Purpose |
|------|-----------|---------|
| `raptor_YYYYMMDD.log` | main.py | Entry scan: signals, orders, vetoes, velocity, cooldown blocks |
| `exits_YYYYMMDD.log` | exit_monitor.py | Exit decisions, trail prices, vol_pctile, regime threshold, trims |
| `recap_preview.html` | daily_recap.py (--preview) | Local HTML preview of email |
| `viper_*.csv` | options_engine.py | Options trade journal |

### 3.4 — Cache Files

| Location | Written By | Purpose | TTL |
|----------|-----------|---------|-----|
| `cache/fred/` | data_feeds.py (FREDDataFeed) | FRED series JSON per series_id | 6 hours |
| `cache/universe/universe_YYYY-MM-DD.json` | universe_builder.py | Daily screened symbol list | 1 day |
| `cache/backtest_bars/` | backtest.py | Parquet per symbol for backtest runs | Permanent |

---

## 4. DATA FLOW ONTOLOGY

### 4.1 — Signal Pipeline

```
Alpaca API
    │
    ▼
data_feeds.AlpacaDataFeed.get_daily_bars()
    │  DataFrame per symbol: open/high/low/close/volume/vwap
    │  150 calendar days lookback, cached 5 min
    ▼
signals.QuantSignalEngine._raw(sym, bars, spy_bars)
    │  16 raw factor values per symbol
    │  Shared intermediates: EMA8/21/50, TR, ADX computed once
    ▼
signals.generate_signals() — cross-sectional z-scoring
    │  MAD-robust z-score per factor across all symbols
    │  Inverse-vol weights × macro_mult × micro_mult
    │  AdaptiveWeights.blend() — ridge + IC adjustment
    │  composite = Σ(z[fn] × w[fn]) for active factors
    │
    ├─→ self._last_full_signals {sym: Signal} ← ALL symbols before top-N
    │
    └─→ returns top-N Signal objects (composite > 0, ranked)
```

### 4.2 — Entry Pipeline

```
Signal objects (top-N)
    │
    ├─ filter: remove already-held symbols
    │
    ├─ GAP 6 cooldown filter (main._get_cooldown_symbols)
    │   outcome_log + position_ledger → blocked symbols set
    │   hard_stop/trail_loss within 5d → block re-entry
    │
    ├─ EntryAgent.evaluate_batch()
    │   Input: composite_score, kelly_fraction, regime,
    │          atr_pct, days_since_earnings, vix_regime, macro_regime
    │   Rules: 6 numbered veto conditions
    │   Output: PASS or VETO + confidence + rule_number
    │
    ├─ GAP 5 composite velocity sizing (main._get_composite_velocity)
    │   hold_history.json snapshots → velocity = comp_today - comp_3d_ago
    │   kelly_modifier = max(0.80, min(1.20, 1.0 + velocity × 0.2))
    │   effective_kelly = kelly_fraction × kelly_modifier
    │
    ├─ Position sizing:
    │   shares = int(my_equity × effective_kelly / entry_price)
    │   my_equity = account.equity × EQUITY_ALLOCATION × risk_scalar
    │   kelly base capped 0.02-0.12 in signals.py
    │
    ├─ Guards: buying_power × 0.95, margin_guard max_new cap
    │
    ├─ submit_order(BUY, limit, client_order_id="v5.4_SYM_TIMESTAMP")
    │
    └─ ledger.record_entry(model, symbol, shares, price, date,
        {stop, regime, t_stat, composite_score, composite_velocity, kelly_fraction})
```

### 4.3 — Exit Pipeline

```
Alpaca positions (live, source of truth)
    │
    ├─ signals._last_full_signals → real composite per held symbol
    │   default 0.0 for unscored (not -1.0 — unknown != weak)
    │
    ├─ Mechanical exits (per position):
    │   EXIT 1: hard_stop        GAP 3: vol-regime ATR mult (2.5/3.0/3.5×)
    │   EXIT 2: trailing_stop    GAP 1: f(days, profit, composite, health)
    │                            trail thresholds: pending calibrate_gap1.py
    │   EXIT 3: thesis_invalid   GAP 4: regime-scaled (RISK_ON→-2.0, NEUTRAL→-1.5,
    │                                   RISK_OFF→-2.0, CRISIS→-2.5) AND pnl < -5%
    │   EXIT 4: portfolio_heat   portfolio_dd < -12%
    │   EXIT 4B: lev_cap         3x ETF > 3 days, 2x ETF > 10 days
    │   EXIT 5: time_decay       flat(5d/20d) AND losing AND comp<0 AND health<0
    │
    ├─ Math trim (from hold_health.json — runs AFTER mechanical):
    │   compute_trim() → TRIM_MINOR/MODERATE/MAJOR/EXIT
    │   trim_pct = severity × stop_mult × FAR_mult × pnl_mult + slope_adj
    │   EXIT → full exit order
    │   TRIM → partial sell (trim_shares, capped at qty-1)
    │
    ├─ Agent advisory (from hold_decisions.json):
    │   Logs HOLD/TRIM/EXIT for calibration — NEVER executes
    │
    ├─ submit_order(SELL, market, client_order_id=reason_sym_timestamp)
    ├─ ledger.record_exit(model, symbol, price, date, reason)
    ├─ trim_log.json ← partial trim + math reasoning + agent cross-reference
    └─ outcome_tracker.run_tracker() → outcome_log.json
        └─ exit_path from client_order_id, fallback: position_ledger exit_reason
```

### 4.4 — Learning Pipeline

```
Every exit:
    outcome_log.json  ← full exits tagged with agent decisions + exit_path (outcome_tracker)

Every trim:
    trim_log.json     ← partial trims + math reasoning + agent cross-ref (exit_monitor)

Every entry:
    entry_vetoes.json ← EntryAgent PASS/VETO (agent_layer)
    adaptive_weights.json ← updated on closed trade (signals.AdaptiveWeights)
    position_ledger.json ← composite_velocity recorded at entry

Every hold_monitor run:
    hold_decisions.json  ← HoldAgent decisions (advisory, calibration data)
    hold_health.json     ← today's 8-layer scores (execution input for math trim)
    hold_history.json    ← daily composite snapshots (velocity input for entries)

[PLANNED — Layer 3] Sunday prompt_calibrator.py:
    Reads:   outcome_log.json + trim_log.json + entry_vetoes.json + hold_decisions.json
    Computes: precision/recall per decision type (VETO, EXIT, TRIM, HOLD)
    Writes:  prompt_versions/entry_prompt_vN.txt, hold_prompt_vN.txt
    Updates: ENTRY_PROMPT, HOLD_PROMPT in agent_layer.py
    Trigger: 30+ agent-tagged trades in outcome_log.json
```

---

## 5. AGENT ONTOLOGY

### 5.1 — Agent Roles

| Agent | Executes | Advisory | Input | Output |
|-------|----------|----------|-------|--------|
| MarketAgent | Rule-based SCAN/REDUCE/STANDBY | LLM reasoning string | macro_context.json | market_decision.json |
| EntryAgent | VETO blocks order | — | signal + macro fields | entry_vetoes.json |
| HoldAgent | **Nothing** (advisory only since 2026-05-15) | All decisions logged for calibration | hold_health summary | hold_decisions.json |

### 5.2 — Agent Infrastructure

- **Model:** llama3.2 (3.2B) — switched from mistral 7.2B (timed out at 120s)
- **Timeout:** 45s per call, 5s health ping
- **Health check:** `_ollama_alive()` pings `/api/tags` before every batch
- **Fast-fail:** entire batch → passthrough defaults if Ollama unreachable
- **Sequential only:** no parallelism — parallel calls timeout
- **Prompt versioning:** hash-deduped snapshots to `prompt_versions/` on every import
- **Macro injection:** `_load_macro_summary()` injects regime into every prompt
- **Short-circuit:** days_history < 5 → HOLD direct write, skips Ollama (saves ~8min)

### 5.3 — What Math Knows vs What Agents Know

| Dimension | Math (signals + hold_monitor) | Agents (llama3.2) |
|-----------|------------------------------|-------------------|
| All 16 factor z-scores | ✅ exact | Summary only |
| Price momentum (ROC5, HH/HL) | ✅ | From summary |
| Volume (OBV, up/down ratio) | ✅ | From summary |
| Composite velocity | ✅ (hold_history) | ❌ not aware |
| Earnings date proximity | ❌ | ✅ days_since_earnings |
| VIX spike | Via macro_context | ✅ injected macro |
| Sector breadth (50/150/200MA) | Via macro_context | ✅ injected macro |
| Credit spreads | Via FRED | ✅ injected macro |
| Portfolio-level heat | ✅ portfolio_dd | ❌ not aware |
| Re-entry cooldown | ✅ outcome_log lookup | ❌ not aware |

---

## 6. PERFORMANCE & ACCOUNT

### 6.1 — Backtest Benchmarks (v5.4, DO NOT MODIFY)
- 120-symbol: 1008% return, 56% CAGR, 4.64 Sortino, 1.73 PF, 2.0% expectancy
- 47-symbol: 201% return, 22.6% CAGR, 2.17 Sortino, 1.43 PF
- v5.5 (20 factors), v6.0 (SR cluster), v7 (behavioral gates) all underperformed — do not revisit
- **GAP 1 backtest with composite proxy:** RUNNING AS OF 2026-05-22 — results pending
- **Baseline (pre-GAP1):** trail_loss=980, trail_profit=629, profit_target=124, momentum_break=21, Total Return=1466%, Sharpe=1.517

### 6.2 — Live Account (as of 2026-05-22)
- Equity: ~$105K | ~10 open positions
- Backfilled positions: entry_date=2026-05-15, regime=BACKFILL
- HoldAgent: operational, short-circuit for days_history < 5

### 6.3 — Factor Library (DO NOT MODIFY)
```
MR cluster:    rsi_mr, bollinger_z, crowd_panic, ma_distance, hurst
TREND cluster: ma_stack, macd_accel, adx_dir, price_cloud
VOL cluster:   vol_ratio, obv_r2, accum_dist
VOLAT cluster: atr_pctile, bb_squeeze, rel_strength
REV cluster:   rev_momentum
```

---

## 7. TASK SCHEDULER

| Time ET | Task Name | Script | What it does |
|---------|-----------|--------|--------------|
| 9:00 AM | Raptor_MacroContext | macro_context.py | FRED + SPY + sector breadth → macro_context.json |
| 9:15 AM | Raptor_MarketAgent | market_agent.py | SCAN/REDUCE/STANDBY → market_decision.json |
| 9:35 AM | Raptor Bot | Start_Entry.bat → main.py | Entry scan + cooldown filter + velocity sizing + BUY orders |
| 9:52 AM | Raptor Morning Monitor | Start_Morning_Monitor.bat | exit_monitor + hold_monitor |
| 3:50 PM | Raptor Afternoon Monitor | Start_Afternoon_Monitor.bat | exit_monitor + hold_monitor + recap email |
| 4:30 PM | Raptor_DailyRecap | Start_Recap.bat → daily_recap.py | Recap email at closing prices |

**Scheduler rules:**
- Always run Task Scheduler edits (`Set-ScheduledTask`) from elevated (admin) PowerShell
- Regular PowerShell returns `Access is denied`
- All Python/coding work stays in regular PowerShell
- Afternoon Monitor: ExecutionTimeLimit=PT0S (unlimited) — 15min limit was killing it

---

## 8. CRITICAL RULES

1. **v5.4 signals.py is the gold standard.** Do NOT add factors. Do NOT rebuild from scratch.
2. **str_replace for edits, create_file for new files only.**
3. **Backtest before any signal change.** No exceptions.
4. **Exit path labels:** hard_stop, trailing_stop, thesis_invalid, portfolio_heat, leveraged_3x_cap, leveraged_2x_cap, time_decay, math_exit, math_trim_X% — all labeled distinctly for diagnostics.
5. **Ledger must match Alpaca.** Resync: `python backfill_ledger.py --write`
6. **v5.5, v6.0, v7 all failed.** Do not revisit.
7. **Clear pycache before every test:** `Remove-Item -Recurse -Force __pycache__`
8. **Never use defaults in agent context.** Missing data → skip position entirely.
9. **Math trim governs execution.** HoldAgent is advisory only — calibration data collection.
10. **Token budgets:** 4,000 per task, 30,000 per session. Surface breach, don't overrun silently.
11. **Surface conflicts, don't average them.** Pick more recent/tested, flag the other.
12. **Read before writing.** Read file + callers + shared utilities before any edit.
13. **Checkpoint after every significant step.**
14. **Fail loud.** If unconfirmed, say so. Never hide uncertainty.
15. **Steve is not a coder.** str_replace must show exact find/replace, no ambiguity.
16. **Alpaca position fields:** symbol, qty, avg_entry, current_price, unrealized_pnl, unrealized_pnl_pct, side. No market_value — compute as qty × current_price.
17. **exit_monitor never calls Ollama.** Reads hold_decisions.json advisory only.
18. **signals.py must set self._last_full_signals** before top-N filter.
19. **Use account["buying_power"] not account["cash"]** for capital checks.
20. **market_agent reads macro.get("regime")** not "macro_regime" — data_feeds returns "regime".
21. **Do NOT start Layer 3** until 30+ agent-tagged trades in outcome_log.json.
22. **margin_guard fails CLOSED** — API error blocks entries, does not allow them.
23. **On-margin = REDUCE** — cash < 0 caps new entries at 1, same as util >85%.
24. **composite_velocity recorded in ledger** at every entry — required for future IC calibration.
25. **Cooldown symbols from outcome_log first**, position_ledger second — outcome_log is authoritative.

---

## 9. WHAT NOT TO DO

- Don't add factors to signals.py — proven to degrade
- Don't propose HMM regime detection — overfits
- Don't propose online RandomForest retraining — can't incrementally learn
- Don't redesign architecture — iterate on what works
- Don't use CMD syntax — PowerShell only
- Don't parallelize Ollama calls — causes timeouts
- Don't use defaults in agent context — skip missing data
- Don't let HoldAgent execute orders — advisory only since 2026-05-15
- Don't set default composite to -1.0 for unscored symbols — use 0.0
- Don't remove _last_full_signals from signals.py
- Don't use account["cash"] for buying power — use buying_power
- Don't add Ollama calls to exit_monitor
- Don't use market_value from Alpaca positions — field doesn't exist
- Don't re-introduce parallel agent calls
- Don't fail open in margin_guard — always fail closed on API error
- Don't use sqrt(252) to annualize per-trade returns — use sqrt(252/avg_hold_days)

---

## 10. LAYER 3 READINESS CHECK

```powershell
python -c "
import json, os
d = json.load(open('outcome_log.json'))
real = [t for t in d if t.get('entry_agent') not in [None, 'no_record']]
print(f'{len(real)} agent-tagged trades in outcome_log.json (need 30+)')
trims = json.load(open('trim_log.json')) if os.path.exists('trim_log.json') else []
print(f'{len(trims)} trim records in trim_log.json')
holds = json.load(open('hold_decisions.json')) if os.path.exists('hold_decisions.json') else []
print(f'{len(holds)} hold agent decisions in hold_decisions.json')
"
```

---

## 11. STEVE'S PREFERENCES

- Math-first, PhD-level rigor. No emotion, pure math/TA/first principles.
- Creative and proactive design, not reactive.
- Explicit step-by-step with confirmation between steps.
- Direct critical feedback, no softening.
- PowerShell only (not CMD).
- Family financially depending on this — high stakes, no slop.
- Not a coder — str_replace edits must show exact find/replace, no ambiguity.

---

## 12. TOKEN CONSERVATION

- str_replace for edits, create_file for new files only
- Don't view files already discussed in the conversation
- Don't print verbose test output — just pass/fail
- Batch multiple changes in one turn
- Summarize and start fresh session if approaching 30,000 token limit

---

## 13. INFRASTRUCTURE FIXES (HISTORICAL)

### 2026-05-15
- **Raptor_DailyRecap**: Missing `<WorkingDirectory>` — never ran since creation. Fixed via elevated PowerShell.
- **Raptor Afternoon Monitor**: `PT15M` execution limit was killing it. Fixed to `PT0S` (unlimited).
- **time_decay exit**: Rewritten — flat(5d OR 20d) AND losing AND composite<0 AND health<0.
- **HoldAgent short-circuit**: days_history < 5 → direct HOLD write, skips Ollama. Cuts 9min → 75sec.

### 2026-05-17
- **GAP 1 — Signal-aware trailing stop**: `_trail_mult()` now f(composite, health). signal_strength = (composite+health)/2. strength>+0.3 → ×1.3, strength<-0.3 → ×0.75, neutral → ×1.0.
- **Rolling trend scoring (hold_monitor.py)**: `_score_price_momentum()` and `_score_volume()` upgraded from single-day snapshot to 3-snapshot rolling trend. ROC trend + OBV trend components added.

---

## 14. CHANGES APPLIED 2026-05-22

### daily_recap.py — 5 Bug Fixes
| # | Bug | Fix |
|---|-----|-----|
| 1 | `actual_exit_path=unknown` on all outcome_log trades | `outcome_tracker.py`: added `position_ledger.json` fallback — reads `exit_reason` by symbol when Alpaca `client_order_id` absent |
| 2 | Sharpe/Sortino overstated — `sqrt(252)` on per-trade returns | Fixed to `sqrt(252/avg_hold_days)` — correct annualization for swing system |
| 3 | `regime_score` always 0 — wrong key | Dual-path: FRED `score` when present, vote-count from `macro_context.json` signals sub-dict as fallback |
| 4 | Stop dist hardcoded 2% ATR proxy | Reads `stop_dist_atr` from `hold_health.json` — real ATR-based distance |
| 5 | Universe hardcoded `~120 symbols` | Live count from `get_signals()` → `len(universe)` |

### daily_recap.py — 10 New Metrics
| Metric | Source |
|--------|--------|
| Exit reason breakdown (n, win%, avg P&L, avg hold per path) | closed_trades + ledger |
| Rolling 10-trade win rate | Last 10 closed trades |
| Consecutive loss streak (current + worst) | Closed trades reversed |
| Capital efficiency (realized PnL / max capital deployed) | Ledger pnl + entry_price×qty |
| Avg hold days tile | Closed trades |
| Trim efficiency (n, neg comp %, avg PnL, disagree rate) | trim_log.json |
| Agent vs math disagreement rate (full exits) | outcome_log.json |
| Composite score at entry vs current per position | Ledger + today's signals |
| Macro regime at entry vs current per position | Ledger + macro_context.json |
| Composite Δ arrow (▲▼→) per position | Computed from above two |

### backtest.py — GAP E: Composite + Health Proxy
- `_composite_proxy()`: 3-component blend — 20d ROC (50%), excess return vs SPY (30%), EMA 8/21 spread (20%). Scaled to [-2,+2]. ~0.65 correlation with live composite.
- `_health_proxy()`: 2-component blend — 5d return/ATR (60%), position vs entry/ATR (40%). Scaled to [-1,+1].
- Both wired into `_trail_mult()` — replaces hardcoded (0.0, 0.0) neutral that made GAP 1 unvalidatable.
- `comp_history` accumulated per position daily; `avg_comp_proxy` + `health_proxy` recorded on every Trade.
- GAP 1 validation section in report: splits trades into strong/neutral/weak terciles, prints win rate + avg PnL + verdict.
- `calibrate_gap1.py`: 125-config parameter sweep after backtest. Derives optimal threshold/wide_mult/tight_mult. Writes `calibration_gap1.json`.

### margin_guard.py — 4 Bug Fixes
| Bug | Fix |
|-----|-----|
| Fail-safe returned `(True, 99)` on API error — silent capital risk | Now `(False, 0)` — fail closed |
| On-margin only logged warning, entries unlimited | Now caps at 1 new position, same as REDUCE |
| Magic number `99` compared against config | Replaced with `_UNLIMITED = 10_000` sentinel |
| `portfolio_value` fetched but never used | Removed with explanatory comment |

### macro_context.py — Sector Breadth Upgrade (GAP G / Zweig 1986)
- `get_sector_breadth()` now fetches `period="1y"` and checks 50MA, 150MA, 200MA per sector ETF
- Returns `pct_above_50ma`, `pct_above_150ma`, `pct_above_200ma`, `composite_pct` (weighted 40/35/25%), `structural` (BULL/NEUTRAL/BEAR from 200MA)
- `classify_macro()` gains extra vote from `structural` — long-term trend confirmation
- `agent_summary` now includes all three MA breadth %s

### exit_monitor.py — GAP 3 + GAP 4
- **GAP 3 (vol-regime hard stop):** Rolling 60d ATR percentile per position. <25th pctile → 2.5×, 25-75th → 3.0×, >75th → 3.5×. Logger prints vol_pctile on every hard stop.
- **GAP 4 (regime-scaled thesis invalidation):** Reads `macro_context.json` live. RISK_ON→-2.0, NEUTRAL→-1.5, RISK_OFF→-2.0, CRISIS→-2.5 composite threshold. Prevents mass exits during regime-wide drawdowns.

### main.py — GAP 5 + GAP 6
- **GAP 5 (composite velocity sizing):** `_get_composite_velocity()` reads `hold_history.json` snapshots. velocity = comp_today - comp_3d_ago. kelly_modifier = max(0.80, min(1.20, 1.0 + velocity×0.2)). `composite_velocity` recorded in ledger metadata.
- **GAP 6 (re-entry cooldown):** `_get_cooldown_symbols()` checks `outcome_log.json` + `position_ledger.json`. hard_stop/trail_loss within 5 days → blocked. trail_profit NOT blocked.

---

## 15. OPEN GAPS — CURRENT STATUS

| Gap | Description | Status | File |
|-----|-------------|--------|------|
| GAP 1 | Signal-aware trailing stop | ✅ DONE 2026-05-17. Backtest running. | exit_monitor.py |
| GAP E | Backtest composite proxy (enables GAP 1 validation) | ✅ DONE 2026-05-22. Backtest running. | backtest.py |
| GAP D | Calibrate trail modifier thresholds (0.3/-0.3, 1.3/0.75) | ⏳ WAITING for backtest → run calibrate_gap1.py | calibrate_gap1.py |
| GAP B | Kelly caps 0.02/0.12 and t/3.0 unjustified — derive from backtest drawdown | ⏳ NEXT after backtest results | signals.py |
| GAP C | Hold target days conflates volatility with OU speed | ⏳ NEXT after backtest results | hold_monitor.py |
| GAP A | Macro regime classifier vote-count with arbitrary thresholds — HMM/Kalman | 🔴 DO NOT DO — HMM overfits (see §9) | macro_context.py |
| GAP F | Universe filters never sensitivity-tested — parameter sweep needed | 📋 QUEUED | universe_builder.py |
| GAP 2 | Entry sizing ignores conviction gradient — Kelly vs percentile rank | 📋 QUEUED | signals.py |
| GAP 3 | Hard stop fixed, not vol-regime aware | ✅ DONE 2026-05-22 | exit_monitor.py |
| GAP 4 | Thesis invalidation threshold static | ✅ DONE 2026-05-22 | exit_monitor.py |
| GAP 5 | No momentum acceleration detection on entry | ✅ DONE 2026-05-22 | main.py |
| GAP 6 | No re-entry cooldown after stop-out | ✅ DONE 2026-05-22 | main.py |
| GAP G | Sector breadth only 50MA | ✅ DONE 2026-05-22 | macro_context.py |
| GAP H | margin_guard.py never analyzed | ✅ DONE 2026-05-22 | margin_guard.py |
| GAP 7 | Portfolio heat exit blunt — exits 1 position, should trim all health<0 | 📋 QUEUED | exit_monitor.py |
| GAP 9 | Universe scoring once per day — afternoon re-score needed | 📋 LONG TERM | signals.py, main.py |

### Next Session Priority (after backtest finishes)
1. Run `python calibrate_gap1.py` → get optimal thresholds → str_replace into exit_monitor.py
2. GAP B — derive Kelly caps from backtest drawdown analysis (Thorp 2006)
3. GAP C — OU theta per stock for hold target days (Leung & Zhang 2019)
4. GAP F — universe filter parameter sweep
5. GAP 2 — conviction-scaled Kelly (composite percentile rank continuous scaling)
6. GAP 7 — portfolio heat: trim all health<0 positions instead of exiting weakest

---

## 16. MATH GAP ANALYSIS — OPEN RESEARCH

Steve's core tenant: **every buy, hold, and exit decision must be math-driven, adaptive, and exploit inefficiencies.**

### GAP 2 — Entry Sizing Conviction Gradient
**File:** `signals.py`, `main.py`
**Problem:** Kelly capped 0.02-0.12, but doesn't differentiate between composite=0.5 and composite=2.5. High-conviction signals should size larger within Kelly bounds.
**Fix:** Kelly scales continuously with composite percentile rank. Top decile → full Kelly; bottom of entry threshold → minimum Kelly.
**Math:** `kelly = kelly_min + (kelly_max - kelly_min) × composite_percentile_rank`

### GAP B — Kelly Caps Unjustified
**File:** `signals.py`
**Problem:** 0.02/0.12 caps and t/3.0 normalization arbitrary — no derivation from data.
**Fix:** Derive from backtest drawdown analysis. Kelly optimal fraction = (edge/odds). Cap from max acceptable drawdown via Thorp (2006): `f_max = 1 - sqrt(1 - 2×target_return/variance)`.
**Reference:** Thorp (2006) — "The Kelly Criterion in Blackjack Sports Betting and the Stock Market"

### GAP C — Hold Target Days Conflates Volatility with OU Speed
**File:** `hold_monitor.py`, `signals.py`
**Problem:** `hold = max(1, min(30, int(16 + 14 × atr_pctile)))` mixes volatility (ATR) with expected holding period. High-vol stocks get longer hold targets when they should get shorter ones.
**Fix:** OU theta per stock. `theta = -OLS_slope(log_price_demeaned, 30d)`. `half_life = log(2)/theta`. Hold target = 1.5 × half_life, capped 2-30 days.
**Reference:** Leung & Zhang (2019) — arXiv:1701.03960

### GAP 7 — Portfolio Heat Exit Blunt
**File:** `exit_monitor.py`
**Problem:** `portfolio_dd < -12%` exits weakest position entirely. Doesn't consider recovery probability vs continuation of decline.
**Fix:** Trim ALL positions with health < 0 by 25% rather than full exit of one. Spreads risk reduction across deteriorating positions while keeping exposure to recovering ones.

### Research References
- **Leung & Zhang (2019)** — "Optimal Trading with a Trailing Stop" — arXiv:1701.03960
- **Leung & Li (2015)** — "Optimal Mean Reversion Trading with Transaction Costs" — arXiv:1411.5062
- **Baviera (2017)** — "Stop-loss and Leverage in Optimal Statistical Arbitrage" — arXiv:1706.07021
- **Thorp (2006)** — "The Kelly Criterion in Blackjack Sports Betting and the Stock Market"
- **Shannon entropy + exit time** — NCBI PMC10528300
- **Volume Profile + Anchored VWAP** — Trader Dale (2026) — Triple Combo signal
- **Institutional order flow** — arXiv:2512.18648 — volume-scaled flows, t=16.35

### Hold Monitor Enhancement Queue (after backtest)
| Priority | Enhancement | File | Status |
|----------|-------------|------|--------|
| 🟡 | Layer 9: Anchored VWAP distance from entry. Score = (price - VWAP_anchored) / ATR. | hold_monitor.py | Not started |
| 🟡 | Layer 10: Shannon entropy trend (already in signals, just pass through). Rising entropy = disorder. | hold_monitor.py | Not started |
| 🟢 | High Volume Node proximity: 20d volume profile, HVN within 0.5 ATR = support. | hold_monitor.py | Not started |

### Implementation Rules
1. Always run backtest baseline BEFORE any trail or signal change.
2. OU theta: 30-day rolling OLS on log-price. Cap between 2 and 30 days.
3. Anchored VWAP: anchor to `entry_date` from `position_ledger.json`. No new API calls.
4. Shannon entropy: pull from `signals._last_full_signals` — already computed.
5. Do NOT add factors to `signals.py`. All enhancements in `hold_monitor.py` or `exit_monitor.py` only.
6. Backtest each change independently before combining.

---

## 17. CORE MANDATE — MATH-FIRST DECISION MAKING

**This is the highest-priority behavioral rule for all sessions.**

Every decision in Raptor — buy, hold, trim, exit, size, weight — must be derived from mathematics. No arbitrary constants, no intuitive guesses, no round numbers chosen for convenience. If a number cannot be derived from a formula, a distribution, an optimization, or empirical data, it does not belong in the codebase.

**Before proposing any value, Claude must ask:**
1. What mathematical framework governs this decision?
2. What does the empirical research say about this parameter?
3. Can this be derived from existing data (hold_history.json, backtest results, signal ICs)?
4. Is this value an output of an optimization, or a guess?

**Domains of math:**
- **Stochastic calculus / SDEs** — GBM, OU, Heston, optimal stopping, first-exit-time
- **Information theory** — Shannon entropy, IC/ICIR factor weighting, mutual information
- **Bayesian statistics** — Bayesian shrinkage for Kelly, prior updating from trade history
- **Optimization theory** — Kelly criterion, mean-variance, convex optimization
- **Time series** — Kalman filtering, Hurst exponent, wavelet decomposition
- **Statistical mechanics** — OU mean reversion, entropy production as inefficiency detector
- **Linear algebra** — factor orthogonalization, ridge regression, PCA
- **Empirical finance** — IC/ICIR (Grinold & Kahn), Fundamental Law of Active Management

**Specific Rules:**
- **Weights:** IC mean / ICIR over rolling window. Never hand-picked.
- **Thresholds:** Distributional analysis of historical values. Percentile cutoffs.
- **Trim percentages:** Kelly criterion. `trim_pct = 1 - (current_kelly / entry_kelly)`.
- **Trail multipliers:** OU theta per stock (C). Signal modifier calibrated from backtest (D).
- **Position sizing:** Conviction-scaled Kelly. velocity modifier ±20% now live.
- **Hard stop:** Vol-regime ATR percentile scalar. Now live (GAP 3).
- **Re-entry:** Math re-qualification. Cooldown now live (GAP 6).

**What Claude Must NEVER Do:**
- Pick a round number without mathematical justification
- Propose a threshold based on intuition
- Use a fixed constant where a rolling empirical value is available
- Suggest equal weighting when IC-weighted alternatives exist
- Choose a parameter because "it's a common default in the literature"

**The Standard:** If Steve asks "why that number?" and the answer is anything other than a mathematical derivation or empirical measurement, the number is wrong.

---

## 18. CHANGES APPLIED 2026-05-22 (SESSION 2)

### Calibrated GAP 1 Trail Modifier (exit_monitor.py)
From 1565-trade backtest parameter sweep (125 combinations):
- threshold: `0.3 → 0.2` (42% of positions now classified strong vs 38%)
- wide_mult: `1.3 → 1.6` (net trail width 1.019× → 1.176× baseline)
- tight_mult: `0.75 → 0.80` (less aggressive tightening on weak signals)
Expected: +9.1% Sharpe improvement from converting ~9 trail_loss → trail_profit per 1565 trades.

### GAP B — Kelly Caps Derived (Thorp 2006) (signals.py)
From 1565-trade backtest: E[R]=1.476%, σ=8.307%, f*=14.26% (Thorp), f_post=13.97%, f_half=6.98%, f_max_dd=5.17%.
- Floor: `0.02 → 0.0171` (f_base × 0.33)
- Ceiling: `0.12 → 0.0517` (drawdown-constrained, was 2.3× too high)
- `t/3.0` normalization validated — correct, kept
- Leverage cap: `0.20 → KELLY_MAX × 2.0 = 0.1034`

### GAP 2 — Conviction-Scaled Kelly (signals.py)
Two-component conviction scalar: 40% t-stat + 60% composite percentile rank.
`kelly = KELLY_MIN + (KELLY_MAX - KELLY_MIN) × (0.40 × t_component + 0.60 × pctile)`
Range: 3.08% (55th pctile, t=0.5) → 5.13% (98th pctile, t=3.0). 65% size differential.

### GAP C — OU Theta Hold Target (signals.py)
30-day rolling OLS: `theta = -cov(delta_log_price, lagged_deviation) / var(lagged_deviation)`.
`hold_target = ceil(log(2)/theta × (2 if TRENDING else 1))`, capped [3, 30] days.
Fallback: 8 days (backtest avg_hold=7.9d). Replaces `16 + 14×atr_pctile` (wrong — conflated vol with OU speed).

### GAP F — Universe Filter Sweep (universe_builder.py)
3125-combination sweep on live 141-symbol universe. Sensitivity ranking: dollar_vol (0.1487) >> price_min (0.1009) >> volume (0.0381) >> range (0.0219) >> price_max (0.0062).
- `volume_min: 500K → 750K` (sensitivity rank 2, cuts low-quality names, ~93 symbols)
- `dollar_vol: $20M → $30M` (highest sensitivity, raises median liquidity $52M→$68M)
- Price and range filters: inert — no change.

### GAP 7 — Portfolio Heat Proportional Trim-All (exit_monitor.py)
Replaced binary single-exit with proportional trim of all health<0 positions.
`heat_trim_pct = clip(excess_dd / threshold, 10%, 50%)`
Healthy positions (health≥0) untouched. Never full-exits via heat path.

### GAP 9 — Afternoon Composite Rescore (exit_monitor.py)
Signal engine already runs in exit_monitor. After execution, fresh composites written back to hold_health.json:
- `composite_morning`, `composite_delta`, `afternoon_flag` fields added per position
- `COMPOSITE_DECAY` flag (Δ < -0.5) logged as warning for next-morning priority
- `COMPOSITE_STRENGTH` flag (Δ > +0.3) logged for hold confidence
Zero new API calls — uses full_map already in memory.

### P2-12 — Prompt Snapshot Lazy (agent_layer.py)
Was calling `_snapshot_prompts()` at module level — filesystem glob on every import.
Now lazy: fires on first `evaluate_batch()` call, guarded by `_prompts_snapshotted` flag.

### P1-9 — Watchdog v1.1 (watchdog.py)
Three bugs fixed:
1. SPY circuit breaker: was comparing yesterday vs day-before. Now live price vs prev close.
2. High water mark: was `max(price, entry)` at single moment. Now reads hold_health.json accumulated value.
3. Momentum break removed: was 8-day EMA on daily bars — not intraday. Belongs in exit_monitor.
`client_order_id` added to watchdog sell orders for outcome_tracker tagging.

### P1-15 — Sentiment Pipeline Removed (data_feeds.py)
`get_sentiment_scores()` called on every scan, hit Alpaca news API per symbol.
`sentiment_score=0.0` hardcoded on every Signal — zero contribution. Now disabled, returns `{}`.

### P2-7 — OBV Rolling Normalization (hold_monitor.py)
Magic constant 1000 replaced with rolling max magnitude across last 10 snapshots.
Self-calibrating per stock — high-volume stocks no longer compressed to near-zero.

### P2-8 — Volatility Layer Continuous (hold_monitor.py)
Binary behavior in 0.80-1.20 ATR expansion range replaced with linear interpolation.
0.80 (contracting) → +0.15. 1.20 (expanding) → ±0.5/-0.8. Smooth gradient in between.

### P2-9 — Stop Distance Zero = Danger (hold_monitor.py)
`dist=None` → 0.0 (no data, neutral). `dist≤0` → -1.0 (maximum negative — price at/below stop).
Was returning 0.0 (neutral) for both cases.

### Hold Monitor Layers 9 and 10 (hold_monitor.py)
Layer 9 — Anchored VWAP (5% weight):
  Anchored to entry. Score = clip((price - VWAP_anchored) / ATR, -1, 1).
  Approximated from daily price × vol_ratio in snapshots. No new API calls.

Layer 10 — Shannon Entropy (4% weight):
  H = -sum(p × log(p)) over 5-bin discretization of last 10 daily returns.
  score = clip(1 - 2 × H/H_max, -1, 1). Rising entropy → extra penalty.
  H=0 (directional) → +1.0. H=H_max (random) → -1.0.

Layer weights redistributed (sum = 1.00):
  composite_slope: 0.25→0.23, factor_agreement: 0.20→0.18, price_momentum: 0.15→0.14,
  cluster_health: 0.15→0.13, volume: 0.10→0.09, volatility: 0.08→0.07,
  stop_distance: 0.05 (unchanged), hold_duration: 0.02 (unchanged),
  anchored_vwap: 0.05 (NEW), entropy: 0.04 (NEW).

### log(0) Warnings Fixed (signals.py)
Three locations where np.log could receive zero or negative input:
- vol_ratio: guard today's volume > 0 AND avg > 0 before log
- hurst: guard mean_rs > 0 before log(mean_rs)
- OU theta: guard all close prices > 0 before np.log(close_vals)

### Open Gaps Remaining (post 2026-05-22 session 2)
- GAP D: calibrate_gap1.py → run after backtest finishes
- Backtest: re-run needed after GAP B/C/D changes
- Layer weight IC calibration: needs 60+ agent-tagged trades
- prompt_calibrator.py (Layer 3): needs 30+ agent-tagged trades
- Watchdog intraday ATR: true intraday vol needs 15-min bars
- HVN proximity (hold monitor): 20-day volume profile
- GAP 9 afternoon rescore of candidates (not just held positions)
