# Raptor Trading System — Development Skill
*Last updated: 2026-05-27*

---

## System Overview

Raptor v5.5 is a quantitative swing trading system on Alpaca paper (~$106K equity).  
Viper v1.0 is a separate options engine sharing the same account.

---

## Architecture

```
signals.py          Dual-book signal engine — FACTOR_NAMES list drives all scoring
config.py           All parameters
main.py             Entry scanner — 9:35 AM, velocity + cooldown gates
exit_monitor.py     Position manager — 5 exit paths
hold_monitor.py     10-layer health scoring — LAYER_WEIGHTS dict
outcome_tracker.py  Trade labeling — --backfill, --relabel flags
kelly_engine.py     Bootstrap Kelly (shadow until 100 trades)
factor_lab.py       Spearman IC validation per factor
macro_context.py    Regime classifier → continuous macro_score [-1,1]
backtest.py         Walk-forward backtester
universe_builder.py Screens 6800 assets → ~120–181 symbols
```

---

## Factor System — Extensible by Design

Factors are registered in two places in signals.py. To add a new factor:

1. **Implement** the static method in `class Factors` — takes a DataFrame, returns a float
2. **Register** in `FACTOR_NAMES` list (line ~242)
3. **Assign cluster** in `FACTOR_CLUSTERS` dict — one of: `mr, trend, vol, volat, rev`
4. **Gate for production:** new factor must show IC > 0.05 and ICIR > 0.5 over 60-day rolling window before entering composite — track via factor_lab.py

To remove a factor: delete from FACTOR_NAMES and FACTOR_CLUSTERS. AdaptiveWeights and all downstream scoring auto-adjusts. No other changes required.

**Current factors (16):**

| Cluster | Factors |
|---------|---------|
| mr | rsi_mr, bollinger_z, crowd_panic, ma_distance, hurst |
| trend | ma_stack, macd_accel, adx_dir, price_cloud |
| vol | vol_ratio, obv_r2, accum_dist |
| volat | atr_pctile, bb_squeeze, rel_strength |
| rev | rev_momentum |

**vol_ratio status:** IC = -0.11 — WATCH. Remove if IC < 0.03 and t < 1.0 for 3+ consecutive weeks.

---

## Immutable Rules

1. **Math-first.** Every constant needs a derivation or is explicitly flagged as `# TODO:DERIVE`. Round numbers without derivation are bugs.
2. **str_replace for edits, create_file for new files only.**
3. **No fabricated fallbacks.** Missing data → skip with warning logged. Never substitute invented values.
4. **Not scored today ≠ failing thesis.** comp=0.0 (neutral) for unscored held positions, never -1.0.
5. **Momentum clustering is intentional alpha.** Do not add correlation gates.
6. **Kelly is SHADOW until 100 trades.** Do not override sizing.
7. **Syntax check every file before committing.**
8. **Every session ends with git push.** Steve must pull before next trading day.
9. **Update ONTOLOGY same session as any architecture change.**
10. **Never change code in intraday window 9:35–3:50 PM ET without explicit intent.**
11. **Clear __pycache__ before every test:** `Remove-Item -Recurse -Force __pycache__`
12. **Real data or skip.** When a position cannot be evaluated → log warning, skip. Never substitute.

---

## What Must Never Be Violated

- Do NOT add factors to signals.py without IC validation gate (see above)
- Do NOT propose HMM regime detection (overfits at current data scale — use ARCH-2 gate)
- Do NOT propose online RandomForest retraining (can't incrementally learn)
- Do NOT redesign architecture — iterate on what works
- Do NOT use CMD syntax — use PowerShell

---

## Performance Reference

| Universe | Return | CAGR | Sortino | PF |
|----------|--------|------|---------|-----|
| 120-symbol | 1008% | 56% | 4.64 | 1.73 |
| 47-symbol | 201% | 22.6% | 2.17 | 1.43 |

Signal engine: 16 factors, 5 clusters, cross-sectional z-scoring, soft shrinkage, inverse-vol weighting, Spearman IC + WLS adaptive weights, Ledoit-Wolf SNR entry ranking.

---

## Steve's Preferences

- Math-first, PhD-level rigor
- Direct critical feedback, no softening
- PowerShell commands
- Explicit step-by-step instructions
- Family financially depending on this
