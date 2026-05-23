# Raptor Trading System — Master Development Skill & Architecture Ontology
*Last updated: 2026-05-15*

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
         │
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
         ├─ margin_guard.check_margin_safety(dm)
         │   util >90% → BLOCK (return)
         │   util >85% → cap new entries at 1
         │   util >75% → WARNING, proceed
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
         ├─ EntryAgent.evaluate_batch(candidates)
         │   ├─ _ollama_alive() ping — fast-fail if down
         │   ├─ numbered veto rules 1-6
         │   ├─ RISK_ON/NEUTRAL/BULLISH → explicit PASS
         │   └─ writes entry_vetoes.json
         │
         ├─ for each signal that passes:
         │   ├─ size = my_equity × kelly_fraction / entry_price
         │   ├─ buying_power check (95% of Alpaca buying_power)
         │   ├─ submit_order(BUY, limit, client_order_id)
         │   └─ ledger.record_entry(v5.4, symbol, shares, price, date, metadata)
         │
9:52 AM  exit_monitor.py (Morning Monitor)
3:50 PM  exit_monitor.py (Afternoon Monitor)
         │
         ├─ signals.generate_signals() → _last_full_signals
         │   scores = {sym: real_composite for all held symbols}
         │   default 0.0 for genuinely unscored (not -1.0)
         │
         ├─ for each position:
         │   ├─ EXIT 1: hard_stop — price ≤ entry - 3.0×ATR
         │   ├─ EXIT 2: trailing_stop — price ≤ high_water - trail×ATR
         │   │          trail multiplier: 2.5→2.0→1.5→1.0 ATR by days held
         │   │          profit tightener: tightens when up >1/2/4 ATR
         │   ├─ EXIT 3: thesis_invalid — comp < -1.5 AND pnl < -5%
         │   ├─ EXIT 4B: leveraged_3x_cap — 3x ETF held > 3 days
         │   │           leveraged_2x_cap — 2x ETF held > 10 days
         │   └─ EXIT 5: time_decay — flat 20d AND losing AND held ≥ 10 days
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
         │   ├─ price momentum, OBV, ATR, Bollinger, volume ratios
         │   ├─ factor_scores from _last_full_signals (real, not Dummy)
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
         ├─ saves hold_health.json (today's scores — read by exit_monitor for math trim)
         ├─ saves hold_history.json (full trajectory — ML training data)
         │
         └─ HoldAgent.evaluate_batch() [ADVISORY — calibration only]
             ├─ _ollama_alive() ping
             ├─ numbered decision rules 1-4
             ├─ days_history < 5 → HOLD always
             └─ writes hold_decisions.json (append-only)

3:50 PM  daily_recap.py (via afternoon monitor)
4:30 PM  daily_recap.py (standalone task)
         └─ email: account summary, holdings, health monitor,
                   top signals, SPY benchmark, portfolio analytics
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
| `main.py` | Entry scanner — sizes and submits BUY orders | `position_ledger.json`, `logs/raptor_DATE.log` | market_decision.json |
| `exit_monitor.py` | All exit and trim logic — mechanical + math | `position_ledger.json`, `trim_log.json`, `logs/exits_DATE.log` | hold_health.json, hold_decisions.json |
| `hold_monitor.py` | 8-layer position health scoring | `hold_health.json`, `hold_history.json`, `hold_decisions.json` | bars, signals |
| `agent_layer.py` | EntryAgent + HoldAgent Ollama wrappers | `entry_vetoes.json`, `hold_decisions.json`, `prompt_versions/` | macro_context.json |
| `market_agent.py` | Session gate — SCAN/REDUCE/STANDBY | `market_decision.json` | macro_context.json |
| `macro_context.py` | FRED macro regime classifier | `macro_context.json` | FRED API, Alpaca (SPY) |
| `margin_guard.py` | Capital utilization gate | — | Alpaca account |
| `ledger.py` | Position tracking open/closed by model version | `position_ledger.json` | — |
| `outcome_tracker.py` | Tags closed trades with agent decisions | `outcome_log.json` | Alpaca orders, entry_vetoes.json, hold_decisions.json |
| `backfill_ledger.py` | One-time ledger population from Alpaca | `position_ledger.json` | Alpaca positions |
| `daily_recap.py` | Bloomberg-style HTML email report | `logs/recap_preview.html` | all JSON files, bars |
| `backtest.py` | Walk-forward backtester | `backtest_results/` | bars |
| `diagnose.py` | Signal diagnostics | — | bars, signals |
| `check_account.py` | Quick account viewer | — | Alpaca account |
| `options_engine.py` | Viper v2.0 — 3 options strategies | `logs/viper_*.csv` | bars, Alpaca |

### 3.2 — JSON State Files

| File | Written By | Read By | Purpose | Growth |
|------|-----------|---------|---------|--------|
| `macro_context.json` | macro_context.py (9:00 AM) | market_agent.py, agent_layer.py | Daily macro regime + agent summary | Overwritten daily |
| `market_decision.json` | market_agent.py (9:15 AM) | main.py | SCAN/REDUCE/STANDBY + risk_scalar | Overwritten daily |
| `hold_health.json` | hold_monitor.py | exit_monitor.py, daily_recap.py | Today's 8-layer scores + trim recommendations | Overwritten each run |
| `hold_history.json` | hold_monitor.py | hold_monitor.py (trajectory) | Full daily factor snapshots per symbol | Append-only, grows forever |
| `hold_decisions.json` | hold_monitor.py (via agent_layer) | exit_monitor.py (advisory log only) | HoldAgent HOLD/TRIM/EXIT decisions | Append-only, grows forever |
| `entry_vetoes.json` | main.py (via agent_layer) | outcome_tracker.py | EntryAgent PASS/VETO decisions | Append-only, grows forever |
| `position_ledger.json` | main.py (entries), exit_monitor.py (exits) | exit_monitor.py, hold_monitor.py, daily_recap.py | Open positions + closed trade history | Grows with trades |
| `outcome_log.json` | outcome_tracker.py | prompt_calibrator.py (planned) | Closed trades tagged with agent decisions | Append-only, grows forever |
| `trim_log.json` | exit_monitor.py | prompt_calibrator.py (planned) | Partial trims + math reasoning + agent cross-ref | Append-only, grows forever |
| `adaptive_weights.json` | signals.py (AdaptiveWeights) | signals.py | Ridge regression + IC weights from closed trades | Updated on each closed trade |

### 3.3 — Log Files (logs/)

| File | Written By | Purpose |
|------|-----------|---------|
| `raptor_YYYYMMDD.log` | main.py | Entry scan: signals, orders placed, vetoes |
| `exits_YYYYMMDD.log` | exit_monitor.py | Exit decisions, trail prices, trims, order results |
| `auto_start.log` | bat files | Task scheduler start/complete timestamps |
| `recap_preview.html` | daily_recap.py (--preview) | Local HTML preview of email |
| `viper_*.csv` | options_engine.py | Options trade journal |

### 3.4 — Cache Files

| Location | Written By | Purpose | TTL |
|----------|-----------|---------|-----|
| `cache/fred/` | data_feeds.py (FREDDataFeed) | FRED series JSON per series_id | 6 hours |
| `cache/universe/universe_YYYY-MM-DD.json` | universe_builder.py | Daily screened symbol list | 1 day |
| Bar cache | data_feeds.py (in-memory) | OHLCV DataFrames | 5 min |

### 3.5 — Prompt Version Files

| Location | Written By | Purpose |
|----------|-----------|---------|
| `prompt_versions/entry_prompt_TIMESTAMP_HASH.txt` | agent_layer.py (on import) | Versioned EntryAgent prompt |
| `prompt_versions/hold_prompt_TIMESTAMP_HASH.txt` | agent_layer.py (on import) | Versioned HoldAgent prompt |

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
    ├─ EntryAgent.evaluate_batch()
    │   Input: composite_score, kelly_fraction, regime,
    │          atr_pct, days_since_earnings, vix_regime, macro_regime
    │   Rules: 6 numbered veto conditions
    │   Output: PASS or VETO + confidence + rule_number
    │
    ├─ Position sizing:
    │   shares = int(my_equity × kelly_fraction / entry_price)
    │   my_equity = account.equity × EQUITY_ALLOCATION × risk_scalar
    │   kelly capped 0.02-0.12 in signals.py
    │
    ├─ Guards: buying_power × 0.95, margin_guard max_new cap
    │
    ├─ submit_order(BUY, limit, client_order_id="v5.4_SYM_TIMESTAMP")
    │
    └─ ledger.record_entry(model, symbol, shares, price, date, {stop, regime, t_stat})
```

### 4.3 — Exit Pipeline

```
Alpaca positions (live, source of truth)
    │
    ├─ signals._last_full_signals → real composite per held symbol
    │   default 0.0 for unscored (not -1.0 — unknown != weak)
    │
    ├─ Mechanical exits (per position):
    │   EXIT 1: hard_stop        price ≤ entry - 3.0×ATR
    │   EXIT 2: trailing_stop    price ≤ high_water - trail×ATR
    │                            trail: 2.5/2.0/1.5/1.0 ATR by days_held
    │   EXIT 3: thesis_invalid   comp < -1.5 AND pnl < -5%
    │   EXIT 4: portfolio_heat   portfolio_dd < -12%
    │   EXIT 4B: lev_cap         3x ETF > 3 days, 2x ETF > 10 days
    │   EXIT 5: time_decay       flat 20d AND losing AND held ≥ 10 days
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
```

### 4.4 — Learning Pipeline

```
Every exit:
    outcome_log.json  ← full exits tagged with agent decisions (outcome_tracker)

Every trim:
    trim_log.json     ← partial trims + math reasoning + agent cross-ref (exit_monitor)

Every entry:
    entry_vetoes.json ← EntryAgent PASS/VETO (agent_layer)
    adaptive_weights.json ← updated on closed trade (signals.AdaptiveWeights)

Every hold_monitor run:
    hold_decisions.json  ← HoldAgent decisions (advisory, calibration data)
    hold_health.json     ← today's 8-layer scores (execution input for math trim)
    hold_history.json    ← daily snapshots appended (ML training data)

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

### 5.3 — What Math Knows vs What Agents Know

| Dimension | Math (signals + hold_monitor) | Agents (llama3.2) |
|-----------|------------------------------|-------------------|
| All 16 factor z-scores | ✅ exact | Summary only |
| Price momentum (ROC5, HH/HL) | ✅ | From summary |
| Volume (OBV, up/down ratio) | ✅ | From summary |
| Earnings date proximity | ❌ | ✅ days_since_earnings |
| VIX spike | Via macro_context | ✅ injected macro |
| Sector breadth | Via macro_context | ✅ injected macro |
| Credit spreads | Via FRED | ✅ injected macro |
| Portfolio-level heat | ✅ portfolio_dd | ❌ not aware |
| Cross-stock correlation | ✅ portfolio guard | ❌ not aware |

**Implication:** Math is authoritative for position-level decisions. Agents add value on macro/structural context (EntryAgent veto) and accumulate calibration data (HoldAgent).

---

## 6. PERFORMANCE & ACCOUNT

### 6.1 — Backtest Benchmarks (v5.4, DO NOT MODIFY)
- 120-symbol: 1008% return, 56% CAGR, 4.64 Sortino, 1.73 PF, 2.0% expectancy
- 47-symbol: 201% return, 22.6% CAGR, 2.17 Sortino, 1.43 PF
- v5.5 (20 factors), v6.0 (SR cluster), v7 (behavioral gates) all underperformed — do not revisit

### 6.2 — Live Account (as of 2026-05-15)
- Equity: ~$105K | Cash: -$68.5K (on margin) | Buying Power: $213K
- 15 positions, market value ~$173K, utilization 165%
- Margin guard BLOCKING new entries until utilization < 90%
- Positions backfilled: entry_date=2026-05-15, regime=BACKFILL, stops estimated at 2% ATR proxy
- HoldAgent history: 1 day — needs 5 days before meaningful reasoning
- Layer 3: NOT started — needs 30+ agent-tagged trades in outcome_log.json

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
| 9:00 AM | Raptor_MacroContext | macro_context.py | FRED + SPY → macro_context.json |
| 9:15 AM | Raptor_MarketAgent | market_agent.py | SCAN/REDUCE/STANDBY → market_decision.json |
| 9:35 AM | Raptor Bot | Start_Entry.bat → main.py | Entry scan + BUY orders |
| 9:52 AM | Raptor Morning Monitor | Start_Morning_Monitor.bat | exit_monitor + hold_monitor |
| 3:50 PM | Raptor Afternoon Monitor | Start_Afternoon_Monitor.bat | exit_monitor + hold_monitor + recap email |
| 4:30 PM | Raptor_DailyRecap | Start_Recap.bat → daily_recap.py | Recap email at closing prices |

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

## 13. INFRASTRUCTURE FIXES APPLIED (2026-05-15)

### Task Scheduler Bugs Fixed
- **Raptor_DailyRecap**: Was missing `<WorkingDirectory>` — task had never successfully run since creation (LastRunTime showed 1999). Fixed by adding WorkingDirectory via elevated PowerShell.
- **Raptor Afternoon Monitor**: Had `<ExecutionTimeLimit>PT15M</ExecutionTimeLimit>` — was being killed at 15 minutes before daily_recap.py could run (exit_monitor + hold_monitor + agent calls exceed 15 min with 15 positions). Fixed to `PT0S` (unlimited).
- **Rule**: Always run Task Scheduler edits (`Set-ScheduledTask`) from an elevated (admin) PowerShell. Regular PowerShell returns `Access is denied`. All Python/coding work stays in regular PowerShell.

---

## 14. NEXT SESSION — HOLD MONITOR & EXIT MONITOR ENHANCEMENTS

Research sourced 2026-05-15. Do not implement until explicitly instructed. Backtest required before any signal change.

### Changes Applied This Session (2026-05-15)

**time_decay exit rewritten (exit_monitor.py):**
- Old: exit if flat 20 days AND losing. Nearly never fired (1 exit in full backtest).
- New: flat (5d OR 20d) AND losing AND `composite < 0` AND `health < 0`. Flatness alone is NOT an exit — a stock basing at support with positive signals holds. Only exits when math confirms thesis is dead across both signal engine and health monitor.
- `_pre_health` dict loaded before the per-symbol loop from `hold_health.json` so composite and health are available to all exit logic.
- Logger prints "flat but thesis intact" or full exit reasoning with health + composite values either way.

**agent_layer.py short-circuit:**
- HoldAgent skips Ollama call entirely when `days_history < 5`. Writes HOLD directly to `hold_decisions.json`. Cuts runtime from 9 minutes to 75 seconds during history buildup period. Agents resume full reasoning at day 5+.

### Priority Queue (ordered) — NEXT SESSION

| Priority | File | Enhancement | Status | Complexity |
|----------|------|-------------|--------|------------|
| 🔴 1 | `exit_monitor.py` | **Signal-aware trailing stop**: trail multiplier must incorporate composite score + health score. Strong signal + green health = wider trail (let winners run). Weak signal + red health = tighter trail (protect profits). Currently blind to signal quality — treats deteriorating and strengthening positions identically. | **NOT STARTED — backtest baseline needed first** | Medium |
| 🔴 2 | `exit_monitor.py` | OU-optimal trailing stop: replace fixed ATR step table (2.5/2.0/1.5/1.0) with per-stock OU theta (mean-reversion speed). Fast-reverting stocks trail tighter sooner; trending stocks get more room. | Not started | Medium |
| 🟡 3 | `hold_monitor.py` | Add Layer 9: **Anchored VWAP distance from entry**. Score = (current_price - anchored_vwap) / ATR. Negative and widening = institutional support eroding. | Not started | Medium |
| 🟡 4 | `hold_monitor.py` | Add Layer 10: **Shannon entropy trend**. Already computed in signals.py — pass into hold_monitor. Rising entropy over 3-day window = increasing disorder = early DECAYING signal. Zero new data fetch needed. | Not started | Low |
| 🟢 5 | `exit_monitor.py` | Regime-conditional profit ceiling: RISK_OFF/NEUTRAL only, soft exit at 3.5× ATR above entry if composite also < 0.3. Improves capital turnover in defensive regimes. | Not started | Medium |
| 🟢 6 | `hold_monitor.py` | High Volume Node proximity: 20-day volume profile per symbol, HVN proximity within 0.5 ATR = structural support = positive health contribution. | Not started | Higher |

### Research References (do not lose these)
- **Leung & Zhang (2019)** — "Optimal Trading with a Trailing Stop" — OU model for optimal trailing stop. arXiv:1701.03960
- **Leung & Li (2015)** — "Optimal Mean Reversion Trading with Transaction Costs and Stop-Loss Exit" — OU optimal entry/exit intervals. arXiv:1411.5062
- **Baviera (2017)** — "Stop-loss and Leverage in Optimal Statistical Arbitrage" — First-Exit-Time analytical solution for OU. arXiv:1706.07021
- **Shannon entropy + exit time** — NCBI PMC10528300 — entropy-based exit timing improves asset selection vs pure CVaR
- **Volume Profile + Anchored VWAP** — Trader Dale (2026) — Triple Combo highest probability hold/exit signal
- **Institutional order flow** — arXiv:2512.18648 — volume-scaled flows carry strongest return predictability (t=16.35)

### Implementation Rules
1. Always run backtest baseline BEFORE any trail or signal change — need the exit distribution to measure impact.
2. OU theta: 30-day rolling OLS on log-price. Cap between 2 and 30 days.
3. Anchored VWAP: anchor to `entry_date` from `position_ledger.json`. Use existing bar data — no new API calls.
4. Shannon entropy: pull from `signals._last_full_signals` — already computed, just not passed through.
5. Do NOT add factors to `signals.py`. All enhancements in `hold_monitor.py` or `exit_monitor.py` only.
6. Backtest each change independently before combining.

---

## 15. MATH GAP ANALYSIS — AREAS WHERE SIGNAL LOGIC IS INCOMPLETE

Steve's core tenant: **every buy, hold, and exit decision must be math-driven, adaptive, and exploit inefficiencies. Identify winners early, cut losers before they crash.**

The following gaps exist in the current architecture where mechanical or arbitrary logic overrides or ignores math:

### GAP 1 — Trailing Stop Is Signal-Blind (CRITICAL)
**File:** `exit_monitor.py` `_trail_mult()`
**Problem:** Trail multiplier is purely time + profit. A position with composite=+2.5 (strong trend, all signals aligned) trails at the same 1.0 ATR after 20 days as a position with composite=-0.8 (thesis failing). The math screams hold; the mechanical clock says tighten.
**Fix:** Trail multiplier = f(time, profit, composite, health). Strong signal widens trail. Weak signal tightens it.

### GAP 2 — Entry Sizing Ignores Signal Conviction Gradient
**File:** `signals.py`, `main.py`
**Problem:** Kelly fraction is capped at 0.02–0.12 and blended with IC weights, but position sizing doesn't differentiate enough between a composite=0.5 entry and a composite=2.5 entry. High-conviction signals should size larger within Kelly bounds.
**Fix:** Kelly fraction scales continuously with composite z-score percentile rank across the universe. Top decile signal gets full Kelly; bottom of entry threshold gets minimum Kelly.

### GAP 3 — Hard Stop Is Fixed, Not Volatility-Regime Aware
**File:** `exit_monitor.py`
**Problem:** Hard stop = entry - 3.0×ATR regardless of VIX regime, stock beta, or current volatility percentile. In a low-vol regime a 3 ATR stop is too wide; in a high-vol regime it may be too tight for normal price action.
**Fix:** Hard stop multiplier adjusts with ATR percentile. Low vol (ATR < 25th pctile) → 2.5× ATR. Normal → 3.0×. High vol (ATR > 75th pctile) → 3.5×. Prevents whipsawing out of positions in choppy high-vol environments.

### GAP 4 — Exit 3 (Thesis Invalidation) Threshold Is Static
**File:** `exit_monitor.py`
**Problem:** `comp < -1.5 AND pnl < -5%` is a fixed threshold. In a BULLISH regime, -1.5 composite is genuinely weak. In a RISK_OFF regime, the entire universe compresses — a -1.5 composite might just be average weakness, not thesis failure.
**Fix:** Thesis invalidation threshold scales with regime. BULLISH: comp < -1.0. NEUTRAL: comp < -1.5. RISK_OFF: comp < -2.0. Prevents mass exits during regime-wide drawdowns.

### GAP 5 — No Momentum Acceleration Detection on Entry
**File:** `signals.py`, `main.py`
**Problem:** Entry is based on composite score rank at a single point in time. A stock that has been accelerating (composite rising day over day) is a far better entry than one that has been decelerating toward the threshold. Same score, very different trajectory.
**Fix:** Add composite_velocity = composite_today - composite_3d_ago as an entry gate multiplier. Accelerating signals get priority; decelerating signals near threshold are skipped or sized smaller.

### GAP 6 — No Re-Entry Logic After Stop-Out
**File:** `main.py`
**Problem:** Once a position is stopped out, that symbol can re-enter immediately the next day if it still ranks in the top-N. A stop-out on a momentum-driven decline often means the thesis genuinely failed — re-entering without a cooldown period or re-qualification criteria burns capital twice on the same failing trade.
**Fix:** After a stop-out, symbol enters a cooldown (5 trading days minimum). Re-entry requires composite > 0.5 AND composite_velocity > 0 AND hold_health > 0 from a fresh hold_monitor evaluation. Math must re-qualify the thesis, not just rank.

### GAP 7 — Portfolio Heat Exit Is Blunt (EXIT 4)
**File:** `exit_monitor.py`
**Problem:** `portfolio_dd < -12%` exits the weakest position (by composite score). But it doesn't consider which position is most likely to recover vs most likely to continue falling. It also doesn't partial-trim multiple positions — it fully exits one.
**Fix:** In portfolio heat, trim ALL positions with health < 0 by 25% rather than fully exiting the weakest one. Spreads the risk reduction across deteriorating positions while keeping exposure to recovering ones.

### GAP 8 — No Profit-Taking Gradient on Winning Positions
**File:** `exit_monitor.py`
**Problem:** Winning positions either trail out or run to hard exits. There is no mechanism to take partial profits on extreme winners (e.g. INTC +23%, STM +23%, AMD +39% currently held) while keeping a core position running. Full position stays on until trail fires — which may give back significant open profit.
**Fix:** When pnl > 15% AND composite still positive AND health > 0 → trim 25% at market to lock profit. Remaining 75% continues trailing. This is not a fixed TP — it's a math-gated partial harvest. Requires composite and health to still support the thesis for the remainder.

### GAP 9 — Universe Scoring Happens Once Per Day
**File:** `signals.py`, `main.py`
**Problem:** Signals are computed once at 9:28 AM. A stock that breaks out at 2 PM with surging volume and momentum shift is invisible until the next morning. Capital sits in weaker positions all afternoon while better opportunities exist.
**Fix:** Afternoon monitor (3:50 PM) runs a lightweight re-score of held positions + top watchlist candidates. Not a full universe scan — just 20–30 symbols. If a held position has been leapfrogged by a stronger signal and its own score has decayed, flag for next-morning exit priority.

### Summary Priority for Next Sessions
1. GAP 1 (signal-aware trail) — DONE 2026-05-17. Trail multiplier now f(composite, health). Backtest pending.
2. GAP 8 (partial profit harvest) — CANCELLED. Wrong philosophy. Hold monitor trim logic is the correct tool. No separate harvest mechanism needed.
3. GAP 5 (momentum acceleration on entry) — improves entry quality without changing universe
4. GAP 3 (volatility-regime hard stop) — reduces whipsaw exits in high-vol environments
5. GAP 6 (re-entry cooldown) — prevents double-loss on same failing thesis
6. GAP 2 (conviction-scaled sizing) — refines Kelly application
7. GAP 4 (regime-scaled thesis invalidation) — prevents mass exit during regime drawdowns
8. GAP 7 (portfolio heat partial trim) — more surgical than current blunt exit
9. GAP 9 (afternoon re-score) — longer term infrastructure addition

---

## 16. CHANGES APPLIED THIS SESSION (2026-05-17)

### GAP 1 — Signal-Aware Trailing Stop (exit_monitor.py) ✅
`_trail_mult()` now accepts `composite` and `health` as inputs. Signal-quality modifier applied after base `min(time, profit)` calculation:
- `signal_strength = (composite + health) / 2`
- strength > +0.3 → multiply trail by 1.3 (wider — let winners run)
- strength < -0.3 → multiply trail by 0.75 (tighter — protect profits)
- neutral → 1.0 (no change)
Both call sites updated to pass `_comp` and `_hlth`. Logger now prints comp and health on every trail exit.
**Backtest baseline:** trail_loss=980, trail_profit=629, profit_target=124, momentum_break=21, Total Return=1466%, Sharpe=1.517
**Backtest with GAP 1:** PENDING — was still running at session end.

### Rolling Trend Scoring — hold_monitor.py ✅
Two scoring functions upgraded from single-day snapshot to rolling trend:

**`_score_price_momentum()`:** Added ROC trend component — measures whether 5-day ROC is accelerating or decelerating over last 3 snapshots. Weight redistribution: roc_s=0.35, struct_s=0.30, cp_s=0.20, roc_trend_s=0.15. Logger now prints ROCtrend score.

**`_score_volume()`:** Added OBV trend component — linear fit over last 3 OBV slope snapshots detects institutional distribution direction. Weight redistribution: obv_score=0.35, ud_score=0.35, obv_trend_s=0.30. Logger now prints OBVtrend score.

**Observed impact:** INTC health dropped 0.199→0.113 (correctly penalizing declining momentum + volume). TSLL health improved -0.234→+0.227 (correctly detecting improving trajectory). TSLL exited TRIM_MODERATE, entered HOLD. System now detects deterioration AND recovery earlier.

### Hold Monitor Layer Weights — NOT CHANGED
Research finding: static manually-chosen weights are wrong. The correct solution is IC-weighted dynamic weighting derived from `hold_history.json` — measuring actual Spearman IC of each layer score against forward trade returns over rolling 60–90 day window. Same approach as adaptive ridge regression in signals.py. Build this before touching any weight values manually.

---

## 17. CORE MANDATE — MATH-FIRST DECISION MAKING

**This is the highest-priority behavioral rule for all future sessions.**

### The Principle
Every decision in Raptor — buy, hold, trim, exit, size, weight — must be derived from mathematics. No arbitrary constants, no intuitive guesses, no round numbers chosen for convenience. If a number cannot be derived from a formula, a distribution, an optimization, or empirical data, it does not belong in the codebase.

### What This Means in Practice

**Before proposing any value, Claude must ask:**
1. What mathematical framework governs this decision?
2. What does the empirical research say about this parameter?
3. Can this be derived from existing data (hold_history.json, backtest results, signal ICs)?
4. Is this value an output of an optimization, or a guess?

**Domains of math to consider for every decision:**
- **Stochastic calculus / SDEs** — price processes (GBM, OU, Heston), optimal stopping, first-exit-time problems (Bertsimas & Lo, Leung & Zhang)
- **Information theory** — Shannon entropy for disorder detection, IC/ICIR for factor weighting, mutual information for signal independence
- **Bayesian statistics** — Bayesian shrinkage for Kelly fraction, prior updating from trade history, posterior distributions over regime states
- **Optimization theory** — Kelly criterion for sizing, mean-variance for portfolio construction, convex optimization for weight allocation
- **Time series / signal processing** — Kalman filtering for latent state estimation, Hurst exponent for regime detection, wavelet decomposition for trend isolation
- **Statistical mechanics / physics** — Ornstein-Uhlenbeck mean reversion (same math as particle in potential well), entropy production in non-equilibrium systems as market inefficiency detector
- **Linear algebra** — orthogonalization of correlated factors, ridge regression for weight learning, PCA for dimension reduction
- **Empirical finance** — IC/ICIR weighting (Grinold & Kahn), Fundamental Law of Active Management, Barra-style risk models

### Specific Rules

**Weights:** Must be derived from IC mean / ICIR over rolling window from actual trade data. Never hand-picked. Use Spearman rank IC between layer score and forward return. Normalize to sum to 1. Update rolling 60–90 days.

**Thresholds:** Must be derived from distributional analysis of historical values. Use percentile cutoffs (e.g. bottom 20th percentile of composite score = thesis failure threshold) not fixed constants.

**Trim percentages:** Must be derived from Kelly criterion applied to current signal strength. `trim_pct = 1 - (current_kelly / entry_kelly)`. Never a flat 25%.

**Trail multipliers:** Must be derived from OU mean-reversion speed (theta) per stock, not a fixed ATR step table. Signal-quality modifier now applied (GAP 1 done). Next: OU theta per stock.

**Position sizing:** Must be derived from conviction-scaled Kelly. `size = base_kelly * composite_percentile_rank`. Full Kelly for top decile signal; minimum for bottom of entry threshold.

**Hard stop:** Must adjust with volatility regime. `stop_mult = base_mult * (atr_percentile_scalar)`. Not fixed at 3.0 ATR regardless of regime.

**Re-entry:** Must require math re-qualification. Composite > 0.5 AND composite_velocity > 0 AND fresh hold_health > 0. Not just rank re-entry.

### What Claude Must NEVER Do
- Pick a round number (25%, 3.0, 0.5) without mathematical justification
- Propose a threshold based on intuition ("seems reasonable")
- Use a fixed constant where a rolling empirical value is available
- Suggest equal weighting when IC-weighted alternatives exist
- Choose a parameter because "it's a common default in the literature" without verifying it fits Raptor's specific data distribution

### The Standard
If Steve asks "why that number?" and the answer is anything other than a mathematical derivation or empirical measurement, the number is wrong and must be redesigned before implementation.

---

## 18. SESSION PROTOCOLS — MANDATORY FOR EVERY CONVERSATION

### 18.1 — Conversation Start
At the start of every new conversation, Claude MUST:
1. Read `RAPTOR_SKILL.md` via `project_knowledge_search` before responding to any technical question
2. Load the recent_updates block from userMemories to understand current state
3. Never assume prior session context without searching

### 18.2 — After Every File Change
After modifying any project file, Claude MUST automatically (without being prompted):
1. Present the file via `present_files` for download
2. Remind the user to push to GitHub with the exact commands:

```bash
# Run from your Raptor project folder on the laptop
git pull origin main
git add -A
git commit -m "describe what changed"
git push origin main
```

3. Note which files were changed so the commit message is accurate

### 18.3 — GitHub ↔ Laptop Reconciliation Rule
- GitHub is the source of truth
- Every session ends with a push — no exceptions
- Before starting work on the laptop after a Claude session, always `git pull origin main` first
- If laptop has uncommitted local changes, commit and push BEFORE pulling Claude's changes

### 18.4 — Ontology Sync Rule
Any architectural change (new file, new data flow, new gap fixed, new layer added, behavior change) MUST be reflected in the Architecture Ontology section (Section 2) of this SKILL file in the same session it is implemented. Do not defer ontology updates.

### 18.5 — File Delivery Standard
Never require the user to ask for the file after making changes. The sequence is always:
1. Make the change
2. Verify syntax / correctness
3. `present_files` automatically
4. Post GitHub push commands

---

## 19. GOVERNING PRINCIPLES — PROACTIVE THINKING BEFORE BUILDING

### 19.1 — The Core Problem Claude Must Never Repeat
As of May 2026, the model's PnL has been moving backwards as more work is added to it. This is the definition of overfitting and architectural drift. More complexity has produced worse real-world results. Claude contributed to this by being reactive — analyzing results when asked, building features when asked, without proactively holding the full system in view and flagging conflicts before they became code.

This section exists to prevent that from happening again.

### 19.2 — Think Before Building: The Pre-Build Checklist
Before writing a single line of code for any new feature, modifier, parameter, or diagnostic, Claude MUST answer all of the following questions explicitly:

1. **Live consistency:** Does this feature exist in the live system? At what time does it run? Does the backtest simulate it at that exact same time and frequency — or is the backtest assuming something the live system cannot deliver?

2. **Causal direction:** Does this change make the model better at finding real market edge, or does it make the backtest number look better? These are not the same thing. If the answer is "it improves the backtest," stop and ask why the live system would behave the same way.

3. **Complexity cost:** Does this addition reduce or increase the number of free parameters? Every free parameter that isn't mathematically derived from data is an overfitting risk. Adding a new modifier with a hand-picked threshold (0.3, 1.3, 0.75) without deriving it from empirical distribution is making the model worse, not better, even if the backtest improves.

4. **Marginal value:** What specific real-world edge does this add? If the answer cannot be stated in one sentence grounded in market microstructure, behavioral finance, or statistical theory, do not build it.

5. **Regression risk:** What could this break? Trace the data flow from signal generation through sizing through exit logic and identify every touchpoint the new feature affects. State it before building.

### 19.3 — Simulation Fidelity Is Non-Negotiable
The backtest must simulate **exactly** what the live system does:

- **Exit check frequency:** If `exit_monitor.py` runs every 30 minutes from 9:35–3:50 ET, the backtest checks exits at those same 30-minute intervals only — not on every bar
- **Entry timing:** If `main.py` runs pre-market and fills at open, the backtest fills at next-day open only
- **Signal availability:** The backtest cannot use information that would not have been available at the time of the simulated decision
- **Trail modifier:** If GAP 1 uses composite_score, the backtest uses the same composite_score available at that moment — not a future value, not a proxy

Any divergence between live execution timing and backtest simulation timing is **architectural slippage**. It will produce backtest results that cannot be reproduced in live trading. This is more damaging than price slippage because it is invisible and systematic.

### 19.4 — The PnL Direction Rule
If the model's backtest PnL is declining as more features are added, stop adding features. The correct response is:
1. Strip back to the last known good baseline (v5.2)
2. Identify exactly which addition caused the regression
3. Understand *why* it caused the regression before adding anything new
4. Never add two things at once — test one change at a time against the baseline

### 19.5 — Research-First, Code-Second
For every proposed model change, Claude must first identify:
- What academic or practitioner literature supports this approach
- What the empirical evidence says about its effectiveness in equity markets
- Whether the implementation matches the theoretical specification (e.g. OU-optimal trailing stop requires estimating theta per stock — a fixed ATR table is not an OU stop)

Domains to draw from before proposing any change:
- Market microstructure (Kyle 1985, Glosten-Milgrom) — how information flows into prices
- Optimal stopping theory (Bertsimas & Lo 1998, Leung & Zhang) — when to exit
- Factor investing (Grinold & Kahn, Fama-French, AQR research) — what signals have empirical edge
- Behavioral finance (Kahneman, Thaler, Jegadeesh & Titman) — exploitable irrationality
- Risk management (Kelly 1956, Markowitz, Black-Litterman) — how to size and allocate
- Time series econometrics (Engle, Hamilton) — regime detection, volatility modeling

### 19.6 — Claude's Role
Claude is not a code generator. Claude is a quantitative research partner whose job is to:
- Hold the full system architecture in mind at all times
- Flag inconsistencies between backtest assumptions and live execution before they become code
- Propose changes grounded in math and empirical research, not intuition
- Push back when a proposed change adds complexity without mathematical justification
- Protect the model from overfitting by treating backtest improvement as a necessary but not sufficient condition for any change
- Never let the backtest number become the goal — the goal is live trading performance

If Claude finds itself building something that makes the backtest look better without a clear live-trading mechanism, Claude must stop and say so.
