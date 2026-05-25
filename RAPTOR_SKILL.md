# Raptor Trading System — Development Skill

## System Overview
Raptor v5.4 is a quantitative swing trading system running on Alpaca paper trading.
Viper v1.0 is a separate options trading engine sharing the same account.

## File Architecture
```
C:\Users\steve\OneDrive\Desktop\Raptor\
├── signals.py          # v5.4 signal engine — 16 factors, 5 clusters, inverse-vol weighting
├── config.py           # All parameters — v5.4.0
├── main.py             # Entry scanner — runs daily at 9:35 AM
├── exit_monitor.py     # Position manager — all Alpaca positions, 5 exit paths
├── backtest.py         # Walk-forward backtester — dynamic 120-symbol universe
├── data_feeds.py       # Alpaca bars + FRED macro data
├── universe_builder.py # Screens 6800 assets down to ~120 tradeable symbols
├── ledger.py           # Position tracking by model version
├── diagnose.py         # Signal diagnostics
├── check_account.py    # Quick account viewer
├── options_engine.py   # Viper v1.0 — 3 strategy options engine
├── Start_Raptor.bat    # Daily launcher (entry scan + exit monitor)
├── Start_Viper.bat     # Options scan every 30 minutes
├── .env                # API keys (never modify or display)
├── logs/               # Daily logs + viper CSV journals
└── cache/              # Bar data cache (parquet files)
```

## Critical Rules
1. **v5.4 signals.py is the gold standard.** 16 factors, inverse-vol weighting, 201% backtest (1008% on 120-symbol universe). Do NOT add factors — v5.5 with 20 factors underperformed. Do NOT rebuild from scratch.
2. **Use str_replace for edits, not create_file.** Saves tokens. Only create_file for new files.
3. **Backtest before deploying any change.** No code goes to production without a backtest comparison.
4. **Exit system has 5 paths:** hard_stop, trail_profit, trail_loss, profit_target, momentum_break. All labeled distinctly for diagnostics.
5. **Ledger must match Alpaca.** If positions are sold outside the bot, clear the ledger: `python -c "import json; json.dump({'positions':{},'closed':[]}, open('position_ledger.json','w'), indent=2)"`
6. **v6.0 (20 factors with SR cluster) and v7 (behavioral gates) both failed.** Do not revisit.
7. **Clear __pycache__ before every test:** `Remove-Item -Recurse -Force __pycache__`

## Current Performance (best backtest)
- 120-symbol universe: 1008% return, 56% CAGR, 4.64 Sortino, 1.73 PF, 2.0% expectancy
- 47-symbol universe: 201% return, 22.6% CAGR, 2.17 Sortino, 1.43 PF
- Signal engine: 16 factors, 5 clusters, cross-sectional z-scoring, inverse-vol self-tuning weights
- Exit system: tight trailing (2.5/2.0/1.5/1.0 ATR) + profit target at 4 ATR + momentum break (2-day close below 8-EMA)

## Factor Library (DO NOT MODIFY)
MR cluster: rsi_mr, bollinger_z, crowd_panic, ma_distance, hurst
TREND cluster: ma_stack, macd_accel, adx_dir, price_cloud
VOL cluster: vol_ratio, obv_r2, accum_dist
VOLAT cluster: atr_pctile, bb_squeeze, rel_strength
REV cluster: rev_momentum

## Key Innovations
- Inverse-volatility factor weighting (from Meta Raptor) — factors that differentiate today get upweighted
- Score-rank entry (no hard t-stat cutoff) — top N by composite where composite > 0
- Kelly cap at 12% with market momentum scaling (0.5x-1.0x)
- Per-stock micro-regime detection (Hurst + ADX → TRENDING/REVERTING/MIXED)
- Adaptive ridge regression learning from closed trades

## Steve's Preferences
- Math-first, PhD-level rigor
- No emotion, pure math/TA/first principles
- Wants creative and proactive design, not reactive
- Explicit step-by-step instructions
- Direct critical feedback with no softening
- PowerShell commands (not CMD)
- Family financially depending on this

## Token Conservation
- Don't rewrite entire files — use surgical str_replace edits
- Don't view files already discussed in conversation
- Don't print verbose test output — just pass/fail
- Batch multiple changes in one turn
- Paste backtest report once, skip day-by-day progress lines

## What NOT To Do
- Don't add more factors to signals.py (proven to degrade)
- Don't propose HMM regime detection (overfits)
- Don't propose online RandomForest retraining (can't incrementally learn)
- Don't redesign architecture — iterate on what works
- Don't use CMD syntax (rmdir /s /q) — use PowerShell (Remove-Item)
