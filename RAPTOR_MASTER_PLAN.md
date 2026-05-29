# RAPTOR — Master Plan
*Single source of truth. Replaces RAPTOR_AUDIT_AND_PLAN, RAPTOR_BAT_AUDIT,*
*RAPTOR_DATA_FLOW_AUDIT, and prior RAPTOR_MASTER_PLAN.*
*Last verified: 2026-05-29 by executing against uploaded code, not from docs.*
*Version: 5.7*

---

## The Standard

Every number must be derivable from a formula, empirical data, or an optimization.
If "why that number?" cannot be answered, the number is a bug.
Real data or skip. Never fabricate a fallback.

---

## Session Startup (every session, no exceptions)

```powershell
# Steve's machine
git -C "C:\Users\steve\OneDrive\Desktop\Raptor" pull origin main
git -C "C:\Users\steve\OneDrive\Desktop\Raptor" log --oneline -3
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
```

Paste the top commit hash to Claude. Claude uses it as LAST_STEVE_COMMIT.

Then paste output of:
```powershell
python outcome_tracker.py --summary
python kelly_engine.py
```

---

## Daily Schedule

| Time ET | Script | Writes |
|---------|--------|--------|
| 9:00 AM | macro_context.py | macro_context.json |
| 9:15 AM | market_agent.py | market_decision.json |
| 9:28 AM | hold_monitor.py --pre | hold_health.json, hold_history.json |
| 9:35 AM | main.py | position_ledger.json, composite_cache.json |
| Every 30 min | hold_monitor.py → exit_monitor.py | hold_health.json, position_ledger.json, trim_log.json |
| 3:50 PM | hold_monitor.py → exit_monitor.py → daily_recap.py | same + recap |
| 4:30 PM | daily_recap.py | recap |
| 5:00 PM | factor_lab.py → kelly_engine.py | factor_ic_report.json, kelly_estimates.json |
| After close | outcome_tracker.py | outcome_log.json |
| End of day | Daily_GitHub_Push.bat | git push |

**Order is mandatory:** hold_monitor ALWAYS before exit_monitor.
Verified ✅ in Start_Morning_Monitor, Start_Intraday_Monitor, Start_Afternoon_Monitor.

---

## Data Flow (who owns what)

| File | Written by | Read by |
|------|-----------|---------|
| position_ledger.json | main.py, exit_monitor.py | exit_monitor.py, hold_monitor.py, daily_recap.py |
| hold_health.json | hold_monitor.py | exit_monitor.py, daily_recap.py |
| hold_history.json | hold_monitor.py | hold_monitor.py |
| outcome_log.json | outcome_tracker.py | factor_lab.py, kelly_engine.py, daily_recap.py |
| trim_log.json | exit_monitor.py | daily_recap.py |
| composite_cache.json | main.py | main.py (velocity gate) |
| cooldown_log.json | exit_monitor.py | main.py (cooldown gate) |
| macro_context.json | macro_context.py | signals.py, exit_monitor.py, hold_monitor.py |
| market_decision.json | market_agent.py | main.py |
| entry_vetoes.json | agent_layer.py | outcome_tracker.py |
| hold_decisions.json | agent_layer.py | outcome_tracker.py |
| factor_ic_report.json | factor_lab.py | reference only |
| kelly_estimates.json | kelly_engine.py | reference only |

---

## What Is Actually Live (verified 2026-05-29 against code)

| Feature | Verified |
|---------|---------|
| Bat order: hold_monitor before exit_monitor | ✅ |
| Ledger _save() atomic (os.replace) | ✅ |
| record_trim keeps position open, reduces shares | ✅ |
| record_exit moves to closed, pnl_pct in % units | ✅ |
| Fabricated fallbacks eliminated (no price×0.02, no entry×0.92) | ✅ |
| comp=0.0 for unscored positions (not -1.0) | ✅ |
| Velocity gate (_velocity_filter) in main.py | ✅ |
| Cooldown gate (_cooldown_filter) in main.py | ✅ |
| Atomic writes: main, outcome_tracker, hold_monitor, exit_monitor, ledger | ✅ |
| Spearman rank IC (not binary sign-match) | ✅ |
| Ledoit-Wolf SNR entry ranking | ✅ |
| Soft z-score shrinkage (replaces hard \|z\|>0.10) | ✅ |
| accum_dist uses r² weight (not abs(r)) | ✅ |
| Regime Gaussian probability blend (continuous macro_score) | ✅ |
| Multi-MA breadth (50/150/200) | ✅ |
| Vol-regime hard stop (2.5/3.0/3.5x ATR) | ✅ |
| Signal-aware trail (composite + health modifier) | ✅ |
| Portfolio heat proportional 25% trim | ✅ |
| Double-trim guard (last_trim_ts, 30 min block) | ✅ |
| portfolio_heat written to trim_log | ✅ |
| Bootstrap Kelly SHADOW mode | ✅ |
| Per-book AdaptiveWeights (MOMENTUM + MR files) | ✅ |
| Sharpe/Sortino correct annualization sqrt(252/avg_hold) | ✅ |
| OBV normalized by rolling std (not magic /1000) | ✅ |
| Outcome tracker --backfill --relabel flags | ✅ |

---

## What Is NOT Live (claimed but absent from code)

| Claim | Reality | Impact |
|-------|---------|--------|
| P0-8: macro["regime"] overridden from macro_context.json in main.py/exit_monitor | NOT in code. main.py line 273 reads macro.get("regime") from data_feeds taxonomy | EntryAgent RISK_OFF veto never fires. Fails safe (silent not misfiring) |
| P0-1: outcome_pending sidecar written by exit_monitor | NOT in code. Zero references to outcome_pending in any .py file | entry_decision=None on all outcome records |
| P1-1: Kalman macro classifier | Not built. Gaussian blend in signals.py is the live regime path | No impact — Gaussian blend is better for current data scale |
| P1-5: OU hold target | Not built. Still 15 days MOM hardcoded, dist_to_mean for MR | Minor. TODO:DERIVE comment present |
| Per-book adaptive_mom/adaptive_mr in signals.py | NOT present. grep finds 0 references | Per-book files exist but QuantSignalEngine may not be using them correctly — verify |

---

## Real Data State (verified 2026-05-29)

```
outcome_log.json:       121 total records
  IC-valid (terminal):    8  ← THE REAL GATE COUNT (not 42)
  math_trim:             54  (excluded from IC — partial exits)
  pre_label:             47  (historical — no factor scores)
  crypto:                12  (separate system)
  entry_decision=PASS:    0  (P0-1 sidecar not live — all None)

kelly_estimates.json:   43 trades, SHADOW mode, win_rate=27.9%
factor_ic_report.json:  n_outcome=0, n_history=139 (IC is proxy-based, not real)
position_ledger.json:   8 open positions
hold_health.json:       8 symbols (matches ledger ✅)
```

**The "42 IC-valid trades" in prior docs is wrong. Real count is 8.**
Gates for MATH-1, MATH-5, ARCH-1 require 60 terminal exits. Currently at 8.

**The IC report is computed on 0 real outcomes.**
All 139 observations are pre-entry snapshots with the same realized return
copied to every snapshot of a position. This is circular (selection bias) and
inflates t-stats via row duplication. Do not make factor keep/drop decisions
from this report until real forward returns are measured.

---

## Open Items

### 🔴 Fix Now (affecting live system)

**OPEN-1: P0-1 sidecar — entry_decision never tagged**
exit_monitor must write outcome_pending.json keyed by Alpaca order ID after
every successful sell. outcome_tracker reads it and populates entry_decision.
Without this, the learning loop has no entry labels. Zero IC-valid records
will have entry_decision populated.
Files: exit_monitor.py, outcome_tracker.py

**OPEN-2: P0-8 regime unification — EntryAgent RISK_OFF veto dead**
After dm.get_full_dataset(), both main.py and exit_monitor.py must load
macro_context.json and overwrite macro["regime"] with its canonical
{RISK_ON, NEUTRAL, RISK_OFF, CRISIS} label.
Files: main.py (~line 175), exit_monitor.py (~line 120)

**OPEN-3: per-book adaptive weights not wired in QuantSignalEngine**
Per-book .json files exist. Verify QuantSignalEngine.__init__ creates
adaptive_mom and adaptive_mr as separate AdaptiveWeights instances and
calls blend_weights() per book, not a single self.adaptive.
File: signals.py

**OPEN-4: IC report is statistically invalid**
factor_lab.py load_history_observations() assigns one trade return to all
snapshots of that position (documented in its own docstring as "approximation").
This is circular and inflates t-stats via row duplication in compute_ic().
Until fixed, mark all factor IC values as PROVISIONAL.
Do not drop or keep any factor based on current report.
File: factor_lab.py

**OPEN-5: Stop prices corrupted for some positions**
AMD stop=$489, INTC stop=$106 when both trade ~$20-120.
Stops appear cross-contaminated. If exit_monitor reads these for hard-stop
or trail, it acts on garbage.
Immediate action: run python repair_and_verify.py and paste output.
File: position_ledger.json, backfill_positions.py

### 🟡 Fix When Gated (data not yet available)

| ID | Item | Gate | Current |
|----|------|------|---------|
| MATH-1 | Regime-conditional IC | 10+ trades per regime bucket | 0 |
| MATH-3 | Full Hurst DFA (replace R/S with DFA exponent) | No gate | Do now |
| MATH-5 | Reduce n_prior 50→20 in kelly_engine | 60 IC-valid terminal exits | 8 |
| ARCH-1 | IC layer weights in hold_monitor | 60 IC-valid terminal exits | 8 |
| Kelly ACTIVE | Enable live Kelly sizing | 100 terminal exits | 8 |

### 🟢 Hygiene (won't break anything, clean up rolling)

| ID | Item | File |
|----|------|------|
| H-1 | Delete: raptor_state.json, diagnose.py, diagnose_regime.py, Start_Raptor_Recap.bat | Various |
| H-2 | Ghost positions in hold_health after exit (stale until next hold_monitor run) | hold_monitor.py |
| H-3 | Universe size hardcoded "~120" in daily_recap | daily_recap.py |
| H-4 | Missing recap metrics (exit breakdown, rolling win rate, trim efficiency) | daily_recap.py |
| H-5 | compute_trim still parses stop_dist from string detail field | hold_monitor.py |
| H-6 | Prompt versioning runs on every import of agent_layer | agent_layer.py |
| H-7 | EQUITY_ALLOCATION=1.00 vestige in main.py | main.py |
| H-8 | config.py kelly_fraction=0.15 never reaches 0.15 (clipped to 0.12) | config.py |
| H-9 | Daily_GitHub_Push.bat uses git add . (misses deletions) | Daily_GitHub_Push.bat |
| H-10 | Start_Raptor_Recap.bat calls raptor_recap.py which does not exist | Start_Raptor_Recap.bat |

---

## Arbitrary Constants (must derive — flagged, not yet replaced)

| Location | Constant | How to derive |
|----------|---------|---------------|
| signals.py | Kelly SNR normalizer /3.0 | Bootstrap Kelly percentile distribution |
| signals.py | Kelly clip 0.02/0.12 | EVT tail on closed returns |
| signals.py | Regime blend sigma 0.25 | Historical regime transition frequency |
| signals.py | hold_target 16+14*atr_pctile | ln(2)/theta per-stock OU speed (Leung & Zhang 2019) |
| hold_monitor.py | LAYER_WEIGHTS (hand-picked) | Spearman IC per layer vs PnL — gate: ARCH-1 |
| hold_monitor.py | TIER_STRONG=0.20, TIER_STABLE=-0.15 | Health score vs forward return distribution |
| exit_monitor.py | Trail modifier ±0.3, 1.3/0.75 | Backtest trail width sensitivity (Bertsimas & Lo 1998) |
| config.py | initial_stop_atr_mult 3.0 | EVT-derived — gate: 50+ clean trades |
| macro_context.py | Vote thresholds 3/0/-2 | Regime IC vs forward return |

---

## Architecture (not changing)

```
signals.py          Dual-book engine — FACTOR_NAMES drives all scoring
config.py           All parameters
main.py             Entry scanner — velocity + cooldown gates
exit_monitor.py     All exit/trim logic — 5 exit paths
hold_monitor.py     10-layer health scoring
outcome_tracker.py  Trade labeling
kelly_engine.py     Bootstrap Kelly (shadow)
factor_lab.py       IC validation
macro_context.py    Regime classifier → macro_score [-1,1]
universe_builder.py 6800 → ~150 symbols
```

**Do not add:** HMM regime, online RandomForest, correlation gates.
**Do not redesign:** iterate on what works.
**Do not use:** CMD syntax (PowerShell only).

---

## Factor System

16 factors, 5 clusters. To add: implement in Factors class → register in
FACTOR_NAMES → assign cluster → gate on IC>0.05, ICIR>0.5 over 60-day rolling
window before composite inclusion.

| Cluster | Factors |
|---------|---------|
| mr | rsi_mr, bollinger_z, crowd_panic, ma_distance, hurst |
| trend | ma_stack, macd_accel, adx_dir, price_cloud |
| vol | vol_ratio, obv_r2, accum_dist |
| volat | atr_pctile, bb_squeeze, rel_strength |
| rev | rev_momentum |

vol_ratio: IC=-0.11, WATCH. Remove if IC<0.03 and t<1.0 for 3+ consecutive weeks.
NOTE: all IC values currently PROVISIONAL (see OPEN-4).

---

## Rules (immutable)

1. Math-first. Every constant needs derivation or TODO:DERIVE with method.
2. str_replace for edits, create_file for new files only.
3. No fabricated fallbacks. Missing data → warn + skip.
4. comp=0.0 for unscored held positions. Never -1.0.
5. Momentum clustering is intentional alpha. No correlation gates.
6. Kelly SHADOW until 100 terminal exits.
7. Syntax check every file before committing.
8. Every code session ends with a patch file handed to Steve.
9. Update ONTOLOGY same session as architecture changes.
10. Never change code intraday (9:35–3:50 ET) without explicit intent.
11. A fix is only DONE when grep/test output is pasted in the same session confirming it.

---

## End of Session (Claude)

```bash
for f in main.py signals.py exit_monitor.py hold_monitor.py outcome_tracker.py kelly_engine.py daily_recap.py; do
    python3 -c "import ast; ast.parse(open('$f').read()); print('OK: $f')" 2>/dev/null
done
git add -A
git commit -m "Description: what changed and why (2026-MM-DD)"
git diff LAST_STEVE_COMMIT HEAD > /tmp/session_fixes.patch
```

## End of Session (Steve)

```powershell
cd "C:\Users\steve\OneDrive\Desktop\Raptor"
git apply session_fixes.patch
git diff --stat
git add -A
git commit -m "same message"
git push origin main
Remove-Item session_fixes.patch
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
```
