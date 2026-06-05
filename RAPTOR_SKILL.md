# Raptor Trading System — Development Skill
*Last updated: 2026-06-05*

---

## System Overview

Raptor v5.4 is a quantitative swing trading system on Alpaca paper (~$107K equity).

---

## Architecture

```
data_feeds.py       AlpacaDataFeed — submit_order, get_positions, get_account, get_daily_bars
signals.py          Dual-book signal engine — FACTOR_NAMES list drives all scoring
config.py           All parameters
main.py             Entry scanner — 9:35 AM, velocity + cooldown gates
exit_monitor.py     Position manager — 5 exit paths + math trim block
hold_monitor.py     8-layer health scoring — LAYER_WEIGHTS dict
outcome_tracker.py  Trade labeling — --backfill, --relabel flags
kelly_engine.py     Bootstrap Kelly (shadow until 100 trades)
factor_lab.py       Spearman IC validation per factor
macro_context.py    Regime classifier → continuous macro_score [-1,1]
backtest.py         Walk-forward backtester
universe_builder.py Screens 6800 assets → dynamic count (~75–150 symbols)
```

Dead files removed 2026-05-29: raptor_state.json, diagnose.py, diagnose_regime.py, Start_Raptor_Recap.bat

---

## Critical Infrastructure — Verify Every Session

**AlpacaDataFeed.submit_order** is the single method responsible for all order execution.
It was missing its `def` line from ~2026-05-25 to 2026-06-05, causing every exit and trim
to fail silently for 11 days. No log output, no Alpaca orders, no ledger updates.

**Always verify before any live run:**
```bash
python3 -c "
from data_feeds import AlpacaDataFeed
assert 'submit_order' in dir(AlpacaDataFeed), 'CRITICAL: submit_order MISSING'
print('OK: submit_order present')
"
```

After any edit to data_feeds.py: re-run this check. Non-negotiable.

---

## Factor System — Extensible by Design

Factors are registered in two places in signals.py. To add a new factor:

1. **Implement** the static method in `class Factors` — takes a DataFrame, returns a float
2. **Register** in `FACTOR_NAMES` list
3. **Assign cluster** in `FACTOR_CLUSTERS` dict — one of: `mr, trend, vol, volat, rev`
4. **Gate for production:** IC > 0.03, |t| > 1.0, ICIR > 0.3 over 60-day rolling window before entering composite

To remove a factor: delete from FACTOR_NAMES and FACTOR_CLUSTERS. All downstream scoring auto-adjusts.

**Current factors (16):**

| Cluster | Factors |
|---------|---------|
| mr | rsi_mr, bollinger_z, crowd_panic, ma_distance, hurst |
| trend | ma_stack, macd_accel, adx_dir, price_cloud |
| vol | vol_ratio, obv_r2, accum_dist |
| volat | atr_pctile, bb_squeeze, rel_strength |
| rev | rev_momentum |

**vol_ratio status:** IC = -0.11 — WATCH. Remove if IC < 0.03 and t < 1.0 for 3+ consecutive weeks.

**hurst estimator:** DFA-1 (Kantelhardt et al. 2002) as of 2026-05-29 — replaces R/S. Requires ≥ 60 bars.

---

## Trim and Exit Execution Chain

Understanding this chain prevents the class of bugs that hid for 11 days:

```
hold_monitor.py
  compute_health_score() → 8 layers → tier + health score
  compute_trim()         → trim% formula → trim_shares, action
  save_health()          → hold_health.json

exit_monitor.py
  EXIT 1–5 checks        → hard stop, trail, thesis, lev cap, time decay
  EXIT 4 portfolio heat  → 25% trim if portfolio_dd < -12%
  Math trim block        → reads hold_health.json, adds to exits list
  EXECUTE loop           → dm.alpaca.submit_order() for each exit
    → outcome_pending.json (keyed by order ID)
    → ledger.record_trim() or ledger.record_exit()
    → trim_log.json
  outcome_tracker.run_tracker() → outcome_log.json
```

**Failure points that have occurred:**
- submit_order missing def line → AttributeError on first call → entire execute loop aborts silently
- hold_monitor crash → stale hold_health.json → exit_monitor uses yesterday's health scores
- Ledger stop above current price (stale backfill) → EXIT 1 fires every run → position never trims

---

## Immutable Rules

1. **Math-first.** Every constant needs a derivation or `# TODO:DERIVE`. Round numbers without derivation are bugs.
2. **No fabricated fallbacks.** Missing data → skip with warning logged. Never substitute invented values.
3. **Not scored today ≠ failing thesis.** comp=0.0 (neutral) for unscored held positions, never -1.0.
4. **Momentum clustering is intentional alpha.** Do not add correlation gates.
5. **Kelly is SHADOW until 100 trades.** Do not override sizing.
6. **Syntax check every file before committing.**
7. **After every edit to data_feeds.py: verify submit_order exists as a method.**
8. **Update ONTOLOGY same session as any architecture change.**
9. **Never change code in intraday window 9:35–3:50 PM ET without explicit intent.**
10. **Clear __pycache__ before every test:** `Remove-Item -Recurse -Force __pycache__`
11. **Real data or skip.** When a position cannot be evaluated → log warning, skip. Never substitute.
12. **Logs are tracked in git.** logs/ is not gitignored. Every session push includes today's logs.

---

## What Must Never Be Violated

- Do NOT add factors to signals.py without IC validation gate
- Do NOT propose HMM regime detection (overfits at current data scale — use ARCH-2 gate)
- Do NOT propose online RandomForest retraining (can't incrementally learn)
- Do NOT redesign architecture — iterate on what works
- Do NOT use CMD syntax — use PowerShell
- Do NOT assume order execution worked without checking outcome_pending.json for the order ID

---

## Audit Lessons Learned (2026-06-05)

**Why submit_order was missed across multiple audit sessions:**
- Code read as complete — docstring + body present, just missing `def` line
- Audits focused on math layer (trim %, health scoring, IC) not infrastructure layer
- Failure was silent — `AttributeError` crashed execute loop with no `FAILED:` log line
- trim_log.json had real entries from before the break, creating false confidence
- hold_health showed TRIM_MAJOR recommendations that appeared to be "not executing" — actually they were being preempted by EXIT 1, but EXIT 1 was also silently failing

**Prevention:**
- Infrastructure integrity check is now Step 3 of every session startup (before health check)
- submit_order check added as explicit Rule 7
- logs/ tracked in git so execution logs are available for analysis
- execute loop now has try/except so one order failure doesn't abort remaining positions

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
