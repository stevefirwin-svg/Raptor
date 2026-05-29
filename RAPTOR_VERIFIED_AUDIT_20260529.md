# RAPTOR — Verified Audit (2026-05-29)

*Method: every finding below was confirmed by executing against the uploaded
working files, not read from planning docs. Where this audit contradicts
RAPTOR_MASTER_PLAN.md or RAPTOR_AUDIT_AND_PLAN.md, the contradiction is
explicit and the planning doc is wrong.*

---

## 0. META-FINDING: the audit trail is unreliable

The planning docs claim "all 8 P0 blockers fixed (2026-05-20)" with
grep-verified evidence (specific reference counts per file). Re-running those
greps against the uploaded code shows **at least 3 of the 8 claimed fixes are
absent from live code**. This means the verification record itself cannot be
trusted — fixes were either made and lost before commit (patch-handoff workflow
dropping changes) or documented as verified without execution.

**Action before any other fix:** adopt a verification rule that a fix is only
"done" when a grep/test is run *in the same session against the committed file*
and the raw output is pasted into the commit. No exceptions. The current docs
should be treated as aspirational, not factual, until re-verified.

---

## 1. CRITICAL — the learning loop is built on invalid statistics

### 1.1 "42 IC-valid trades" is actually 8
`outcome_log.json` = 121 records, composed of:
- 47 `pre_label` (historical, no factor scores)
- 54 `math_trim` (excluded from IC by design — partial exits)
- 12 crypto (excluded — separate system)
- **8 terminal exits** (5 trailing_stop, 2 math_exit, 1 hard_stop) ← real IC-valid count

Every "Have 42" gate in MASTER_PLAN (MATH-1, MATH-5, ARCH-1, Kelly) is really
at 8. You are ~52 trades from the 60-gate, not 18.

**Action:** correct all gate counters to count only terminal exits with
non-null `entry_decision` AND non-empty `factor_scores`. Display that number
in `outcome_tracker.py --summary` as the single source of truth.

### 1.2 factor_ic_report is computed on ZERO trade outcomes
`factor_ic_report.json`: `n_outcome: 0, n_history: 139`. All observations come
from `load_history_observations()` in `factor_lab.py`, which assigns the
**single realized trade return to every pre-entry snapshot of that position**
(stated in its own docstring). A 15-day hold ending +3% produces 15 snapshots
all labeled +3%.

Consequences:
1. **Circular**: factors are correlated against outcomes of trades that were
   selected *because* they scored well on those factors. Selection bias inflates IC.
2. **Duplicated labels**: forward return is constant across a position's
   snapshots, so within-position variance is zero — IC measures cross-sectional
   selection, not forward prediction.

The strong IC values (ma_distance −0.60 t=−8.3, adx_dir +0.47 t=5.8) are
substantially artifacts. **Do not make factor keep/drop decisions from this
report until it is computed on real per-snapshot forward returns.**

**Action:** to compute real forward-return IC you need historical prices at each
snapshot date. Either (a) store forward_return_5d / forward_return_10d on each
snapshot at capture time by looking ahead once the window closes, or (b) pull
historical bars in factor_lab and compute snapshot→snapshot+N returns. Until
one exists, mark the IC report `PROVISIONAL — selection-biased` in its output.

### 1.3 IC t-statistics inflated by row replication
`compute_ic()` applies observation weights via `np.repeat` (duplicating rows),
then computes `t = IC*sqrt(n-2)/sqrt(1-IC^2)` on the inflated n. Duplication
adds no information but the t formula treats it as independent samples. Every
t-stat in the report is overstated.

**Action:** compute t-stat on the *unique* observation count, or use a proper
weighted Spearman. Never let replication drive significance.

---

## 2. CRITICAL — claimed-fixed P0 items absent from live code

### 2.1 P0-8 regime unification — NOT in code
Doc claims main.py/exit_monitor.py override `macro["regime"]` from
`macro_context.json`. Reality:
- `main.py:273` still reads `macro.get("regime", "NEUTRAL")` (data_feeds taxonomy)
- **zero** references to `macro_context.json` in main.py or exit_monitor.py

The two-taxonomy schism (data_feeds BULLISH/BEARISH vs macro_context
RISK_ON/RISK_OFF) is fully intact. EntryAgent veto rules keyed on RISK_OFF
cannot fire. Fails safe (veto silent, not misfiring) but the gate is dead.

**Action:** after `dm.get_full_dataset()`, load macro_context.json and overwrite
`macro["regime"]` with its canonical label in both main.py and exit_monitor.py.
One source of truth for the {RISK_ON, NEUTRAL, RISK_OFF, CRISIS} taxonomy.

### 2.2 P0-1 outcome sidecar — NOT in code
Doc claims exit_monitor writes `outcome_pending.json` (3 refs) and
outcome_tracker reads it (5 refs). Reality: **no .py file references
outcome_pending.** The file exists with stale data (FXI). exit_monitor only
sets `client_order_id` and dumps trim_log.

This is why 114/121 records have `entry_decision=None`. The entry-tagging
mechanism the doc says exists, doesn't.

**Action:** restore the sidecar: exit_monitor writes
`{alpaca_order_id: {symbol, exit_reason, composite, trim_detail, agent_*}}`
after each successful sell; outcome_tracker joins on order id. Keep
client_order_id parse as legacy fallback only.

---

## 3. CRITICAL — live position stops are corrupted
Current `position_ledger.json` open positions:
- AMD  stop=$489.90 (AMD trades ~$120)
- TSLA stop=$418.01
- INTC stop=$106.25 (INTC trades ~$20)
- SMCI stop=$36.71  (SMCI trades ~$24)

Stops are nonsensical vs current price — cross-contaminated or computed against
wrong entry price. If exit_monitor reads these for hard-stop / trail, it acts on
garbage. Real-money risk today.

**Action:** audit how these stops were written (backfill_positions.py uses
closed-record entry_price — likely the wrong record matched). Recompute each
open stop as `entry_price - initial_stop_atr_mult * current_ATR` from real
Alpaca avg_entry, verify each stop < current price for longs, log any that
aren't.

---

## 4. VERIFIED-FIXED (no action — credit to May-24/27 work)
- Bat execution order: hold_monitor before exit_monitor (Morning + Afternoon) ✓
- Ledger entry dates real and varied (not all-backfill) ✓
- record_trim exists; partial trims keep position open ✓
- Ledger/hold_health in sync (8=8) ✓
- Sharpe/Sortino annualization sqrt(252/avg_hold_days) ✓
- OBV normalization by rolling std (not /1000) ✓
- Round-number params carry TODO:DERIVE with method+ref ✓
- Per-book adaptive weight files (MOMENTUM/MEAN_REVERSION) ✓
- Email app-password moved to env var ✓ (NOTE: revoke the old one in Google —
  it was committed to a public repo and is in git history)

---

## 5. STILL-OPEN modeling work (unchanged, correctly deferred)
- OU theta hold target (still 16+14*atr_pctile, TODO present) — gate: clean terminal data
- Layer-weight recalibration — gate: 60 real IC-valid trades (currently 8)
- Kelly active mode — gate: 100 trades (currently 43 by kelly's own count,
  8 of which are terminal). Note win_rate 27.9% on terminal MOMENTUM exits —
  the trim-inflated numbers were hiding a weak terminal book.

---

## 6. RECOMMENDED ORDER
1. **Fix the audit-trail rule** (§0) — stop generating false confirmations.
2. **Fix live stops** (§3) — real money, today.
3. **Restore sidecar + regime override** (§2.1, §2.2) — re-apply the two lost P0 fixes.
4. **Fix IC basis** (§1.2, §1.3) — until done, freeze all factor keep/drop
   decisions and mark the report provisional.
5. **Correct gate counters** (§1.1) — so you stop believing you're near 60.
6. Only then resume modeling (§5).

The foundation problem is not the individual bugs — it is that the system's
self-reported state diverges from its actual state. Fix that first; everything
else is downstream of trusting your own telemetry.
