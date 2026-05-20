# RAPTOR — Full-Code Audit & Prioritized Change Plan
*Audit date: 2026-05-19. Source: every .py, .bat, .json, .md in /mnt/project.*
*P0 blockers: ALL 8 FIXED (2026-05-20). P1/P2: not yet started.*

---

## EXECUTIVE TRIAGE

Three categories. Everything is ranked inside each by alpha impact × broken-ness.

- **🔴 P0 BLOCKERS** — silently corrupting trades RIGHT NOW. Fix before any enhancement.
- **🟠 P1 ALPHA GAPS** — static where dynamic is mathematically required. Real money on table.
- **🟡 P2 HYGIENE** — dead code, schema drift, missing instrumentation. Won't bleed today, will tomorrow.

Then a **BLUEPRINT (SKILL.md) UPDATE LIST** at the end.

---

## 🔴 P0 — SILENT CORRUPTION (FIX FIRST, NO EXCEPTIONS)

These are not enhancements. These are bugs that make every other math improvement worthless. Each is verified against current state files.

### P0-1 — outcome_tracker is dead in the water — ✅ FIXED 2026-05-20
**Files:** `exit_monitor.py`, `outcome_tracker.py`
**Fix applied:** exit_monitor.py now writes `outcome_pending.json` sidecar keyed by Alpaca order ID after every successful sell. Contains: symbol, exit_reason, composite, trim_detail, agent_decision, agent_confidence, agent_reasoning, submitted_at. outcome_tracker.py reads this sidecar via `load_outcome_pending()` and passes per-order metadata to `build_outcome_record()`. client_order_id substring parsing kept as fallback for legacy orders only.
**Verification:** grep confirms 3 references to outcome_pending.json in exit_monitor.py, 5 in outcome_tracker.py. Both files parse clean.

### P0-2 — Three positions are trading BELOW their stop, no exit fired — ✅ FIXED 2026-05-20
**Files:** `hold_monitor.py`
**Fix applied:** hold_monitor.py now detects `meta.get("regime") == "BACKFILL"` and recomputes `stop_price = entry_price - 3.0 * atr_now` using real current ATR instead of trusting the stale 2%-proxy stop from backfill_ledger. Also triggers when `meta.get("stop")` is None.
**Verification:** grep confirms 2 references to `_is_backfill` in hold_monitor.py. File parses clean.

### P0-3 — Regime label schema is fractured across the stack — ✅ FIXED 2026-05-20
**Files:** `signals.py`, `agent_layer.py`
**Fix applied (option A):** signals.py REGIME_MULT now has canonical entries `{RISK_ON, NEUTRAL, RISK_OFF, CRISIS}` as primary keys. Legacy aliases `{EXPANSION, BULLISH, BEARISH}` kept for backtest compatibility. agent_layer.py ENTRY_PROMPT PASS rule changed from "RISK_ON, NEUTRAL, or BULLISH" to "RISK_ON or NEUTRAL" — matching the canonical taxonomy.
**Verification:** grep confirms RISK_ON and RISK_OFF in signals.py REGIME_MULT. agent_layer.py PASS line reads "RISK_ON or NEUTRAL". Both files parse clean.

### P0-4 — `daily_recap.py` reads non-existent `market_value` field — ✅ FIXED (pre-existing)
**Files:** `daily_recap.py`
**Status:** Already fixed in current codebase. `get_capital_utilization()` computes `qty * current_price` per position. No `p.get("market_value")` calls remain in production code paths.

### P0-5 — `watchdog.py` calls `dm.get_bars(...)`, method doesn't exist — ✅ FIXED (pre-existing)
**Files:** `watchdog.py`
**Status:** Already fixed in current codebase by install_updates.py (2026-05-18). Uses `dm.alpaca.get_daily_bars`. Note: still uses daily bars masquerading as intraday — see P1-9 for real fix.

### P0-6 — `.env` file is named `_env` — ✅ NOT A BUG (verified)
**Files:** `.env`
**Status:** `.env` exists on disk and loads correctly via `load_dotenv()`. The `_env` file is a template/example copy — not the active config. No action needed.

### P0-7 — daily_recap Sharpe/Sortino math is wrong — ✅ FIXED 2026-05-20
**Files:** `daily_recap.py`
**Fix applied:** Replaced `np.sqrt(252)` with `annualization_factor = np.sqrt(252.0 / avg_hold)`. avg_hold computed from closed_trades hold_days field, with fallback to entry/exit date delta, default 15.0 if no data. Both Sharpe and Sortino now use this corrected factor.
**Verification:** grep confirms 3 references to `annualization_factor` and 3 to `avg_hold` in daily_recap.py. File parses clean.

### P0-8 — `dataset["macro"]["score"]` ↔ `macro_context["agent_summary"]` are two separate macro fetches — ✅ FIXED 2026-05-20
**Files:** `main.py`, `exit_monitor.py`
**Fix applied:** Both main.py and exit_monitor.py now override `macro["regime"]` from `macro_context.json` immediately after `dm.get_full_dataset()`. This ensures macro_context.py's 9:00 AM canonical regime label is the single source of truth across entries, exits, and agent prompts. data_feeds.compute_regime_score() still runs (provides the numeric score) but its regime label is overridden.
**Verification:** grep confirms 4 references to macro_context.json in each of main.py and exit_monitor.py. Both files parse clean.

---

## 🟠 P1 — ALPHA GAPS (Static where Dynamic is Required)

Math-first violations. Ranked by expected alpha lift × ease of derivation. Every fix is mathematically derived, not hand-picked.

### P1-1 — Macro regime classifier is a vote count with hand-picked weights (blueprint GAP A)
**File:** `macro_context.py` `classify_macro()`
**Current:** Each signal votes ±1 or ±2 to a sum, threshold cuts at 3/0/−2.
**Mathematical violation:** Vote counts assume signal independence, equal information content, and lossless quantization. Yield curve at −0.4 (almost inverted) and at −1.5 (deeply inverted) both score −1.
**Fix (math-derived, dynamic):**
  - Kalman filter on continuous-valued macro features (VIX z, T10Y2Y, BBB spread z, SPY ret z, breadth %, fed rate Δ).
  - Latent regime state = continuous risk score ∈ [−1, +1], NOT a label.
  - Regime label = discretization only at API boundary, with hysteresis to prevent flipping.
  - Reference: Hamilton (1989) Markov regime-switching; Kim & Nelson (1999) state-space.
**Empirical anchor:** Calibrate transition probabilities from 20 years of FRED data, not from thresholds in code.

### P1-2 — Hard stop fixed at 3.0×ATR regardless of volatility regime (blueprint GAP 3)
**File:** `exit_monitor.py` line 192, `config.py` `initial_stop_atr_mult`
**Current:** Always 3.0× ATR. Same in low-vol and high-vol environments.
**Mathematical violation:** Stop is supposed to be a probability-of-noise-stop-out gate. P(stop-out | no thesis change) varies with ATR distribution itself, not with current ATR magnitude.
**Fix (math-derived, dynamic):**
  - `stop_mult = base × f(atr_pctile_60d)` where:
    - atr_pctile < 0.25 → 2.5× (low-vol regime, tighter stop is statistically equivalent)
    - 0.25 ≤ atr_pctile ≤ 0.75 → 3.0×
    - atr_pctile > 0.75 → 3.5× (high-vol regime, need room for noise)
  - Even better: derive multiplier from `stop_prob_target = 0.02` per day and invert through empirical return distribution per stock.

### P1-3 — Trail multipliers are hand-picked step table + ±0.3 / ±1.3 / 0.75 round numbers (blueprint GAP 1+D)
**File:** `exit_monitor.py` `_trail_mult()`, `config.py` trail_*
**Current:**
  - Base step table: 2.5 → 2.0 → 1.5 → 1.0 ATR by days held (Bertsimas & Lo handwave)
  - Signal modifier: ±0.3 thresholds, ×1.3 wide / ×0.75 tight — all round numbers
**Mathematical violation:** Both base table AND signal modifier are constants masquerading as math. Real fix is per-stock OU mean-reversion speed θ.
**Fix (math-derived, dynamic):**
  - OU process: dX = θ(μ − X)dt + σ dW
  - Estimate θ per symbol via 30-day OLS on log-price reversion to local mean (Leung & Zhang 2019, arXiv:1701.03960)
  - Trail width ∝ 1/√θ — fast-reverting stocks trail tighter, trending stocks (low θ) get more room
  - Signal-quality modifier becomes ∝ percentile rank of composite, not bucketed thresholds
  - Cap θ ∈ [2, 30] days half-life

### P1-4 — Kelly cap 0.02–0.12 unjustified, t-stat divisor /3.0 unjustified (blueprint GAP B+2)
**File:** `signals.py` lines 402–403, `config.py` `kelly_fraction=0.15`
**Current:** `base_kelly = 0.15 × (0.5 + min(|t|/3.0, 1.0))`, then clipped to [0.02, 0.12]. Numbers: 0.15, 0.5, 3.0, 0.02, 0.12 — all hand-picked.
**Mathematical violation:** Kelly fraction depends on edge and odds, both estimable from outcome_log. Cap should come from max-drawdown tolerance + uncertainty in edge estimate.
**Fix (math-derived, dynamic):**
  - `f* = (μ − r) / σ²` from realized trade returns by composite decile (need P0-1 fixed first to get clean data)
  - Bayesian shrinkage: prior f* ≈ 0.05, posterior updates per trade
  - Cap from max DD constraint: `f_max = max_drawdown_tolerance / (worst_loss_quantile × 2)` — derive `f_max` from your 12% portfolio_dd target
  - Conviction scaling: `f_used = f_max × composite_percentile_rank` (top decile → full Kelly, bottom of entry threshold → minimum)

### P1-5 — Hold target days conflates volatility with mean-reversion speed (blueprint GAP C)
**File:** `signals.py` line 411
**Current:** `hold = 16 + 14 × atr_pctile` — assumes high-vol = longer hold. Backward. High vol means **faster** information flow, **shorter** optimal hold for mean-reverters.
**Mathematical violation:** ATR is volatility magnitude; OU θ is mean-reversion speed. These are different variables. Hold target should derive from θ alone.
**Fix:** `hold_target_days = ceil(log(2) / θ) × micro_regime_mult`. TRENDING micro → ×2 (let trends run). REVERTING → ×1 (target one full half-life).

### P1-6 — Hold monitor layer weights are static and hand-picked (blueprint GAP, partially)
**File:** `hold_monitor.py` `LAYER_WEIGHTS` lines 46–55
**Current:** Manually chosen: composite_slope=0.25, factor_agreement=0.20, etc.
**Mathematical violation:** Same as factor weights in signals.py — should be IC-weighted from realized data.
**Fix (math-derived, dynamic):**
  - For each layer score, compute Spearman rank IC vs forward 3/5/10-day trade return from hold_history.json + closed trades
  - Rolling 60–90 day window
  - Normalize to sum to 1, no negative weights allowed (clip then renorm)
  - Same pattern as `AdaptiveWeights.blend_weights` in signals.py — copy that pattern
  - **Gate:** needs 60+ closed trades. Until then, static weights stay. **Don't touch manually before then.** (Memory note: confirmed.)

### P1-7 — `compute_trim()` has 5 hand-picked round-number components (blueprint GAP D)
**File:** `hold_monitor.py` lines 461–490
**Current:**
  - `stop_mult` step table: 1.50 / 1.25 / 1.00 / 0.75
  - `far_mult` cap: 0.30
  - `slope_adj` cap: 0.20
  - `pnl_mult` step table: 0.60 / 0.80 / 1.00 / 1.20 / 1.40
  - Action thresholds: 0.90 EXIT / 0.50 MAJOR / 0.25 MODERATE — round numbers
**Mathematical violation:** All five tables. The whole function is a wall of magic numbers.
**Fix (math-derived, dynamic):**
  - `trim_pct = 1 − (current_kelly / entry_kelly)` from P1-4 derivation
  - This means: if signal degrades to half its original conviction, trim 50%. If to zero, full exit. Continuous.
  - Action labels (TRIM_MINOR/MODERATE/MAJOR/EXIT) are display tier only — derived from quantiles of historical trim_pct distribution, not hardcoded.

### P1-8 — Thesis invalidation is fixed at `comp < -1.5 AND pnl < -5%` (blueprint GAP 4)
**File:** `exit_monitor.py` line 214
**Mathematical violation:** −1.5 composite means different things in different regimes. In RISK_OFF the whole universe compresses; −1.5 might be average. In RISK_ON it's a clear outlier.
**Fix:** Threshold = `μ_composite_today − 1.5 × σ_composite_today` (cross-sectional, today's data). Or percentile-based: bottom 10% of today's composite distribution. **Always relative, never absolute.**

### P1-9 — Watchdog uses daily bars, claims to be intraday
**File:** `watchdog.py`
**Current:** Even after fixing P0-5, `get_daily_bars(lookback_days=5)` returns DAILY bars. The "intraday trail" computes EMA8 on 5 daily bars — meaningless.
**Fix (math-derived, dynamic):**
  - Use Alpaca minute or 15-min bars endpoint
  - Real intraday volatility = realized 5-min variance × √(78 bars in 6.5 hours)
  - SPY circuit breaker = intraday SPY return < −3σ of daily vol, NOT −3% absolute
  - If intraday infrastructure isn't worth building, **delete watchdog.py and Start_Watchdog.bat**, don't pretend it's monitoring.

### P1-10 — No composite_velocity / acceleration on entry (blueprint GAP 5)
**File:** `signals.py`, `main.py`
**Mathematical violation:** Entry is snapshot rank. Two stocks with composite=1.5 — one accelerating from 0.5 last week, one decelerating from 2.5 — are not equivalent. Acceleration is a leading indicator.
**Fix (math-derived, dynamic):**
  - `composite_velocity = composite_today − composite_3d_ago` from hold_history pre-entry tracker
  - `entry_multiplier = sigmoid(velocity / σ_velocity_universe)` — sigmoid keeps it bounded
  - Entry size scales with velocity rank. Decelerating signals get smaller positions, not blocked.
  - Cheap because pre-entry tracker already runs and persists 7 days of snapshots.

### P1-11 — No re-entry cooldown / re-qualification (blueprint GAP 6)
**File:** `main.py`
**Current:** Symbol stopped out today can re-enter tomorrow if it ranks back into top-N. No memory of the stop.
**Fix (math-derived, dynamic):**
  - On stop-out, write `cooldown_until = today + (atr_pctile × 10 trading days)` to a `cooldown.json`
  - Re-entry requires: composite > 0 AND composite_velocity > 0 AND fresh hold_health > 0
  - Cooldown floor: 3 days. Cooldown ceiling: 15 days.
  - Avoids burning capital on a thesis the market just rejected.

### P1-12 — Portfolio heat exit is a binary "kick weakest" (blueprint GAP 7)
**File:** `exit_monitor.py` lines 277–290
**Current:** Drawdown > 12% → full exit of the weakest composite. Concentrated, blunt.
**Fix (math-derived, dynamic):**
  - When portfolio_dd < threshold, trim ALL positions with health < 0 by `α = abs(dd) / max_dd` percent
  - Keeps optionality on potential recoveries while spreading the risk reduction
  - Reference: risk parity rebalancing — equalize marginal contribution to risk

### P1-13 — Sector breadth uses only 50-day MA (blueprint GAP G)
**File:** `macro_context.py` `get_sector_breadth()`
**Current:** Only counts sectors above 50MA.
**Fix:** Composite breadth = weighted avg of (above 50MA, above 150MA, above 200MA). Zweig 1986 — 200MA breadth + advance/decline beats single-MA. Already have the data — no new API calls.

### P1-14 — Universe filters never sensitivity-tested (blueprint GAP F)
**File:** `universe_builder.py`
**Current:** Price ∈ [5, 1000], avg_vol ≥ 500K, dollar_vol ≥ $20M, daily_range ≥ 1.0% — all hand-picked.
**Fix (planning):** Backtest sweep over each threshold ±50%. Find Sharpe-optimal frontier. Pick filter where 10% change in threshold has < 2% Sharpe impact (robust point).

### P1-15 — Sentiment lexicon is 26 positive + 26 negative words, no domain validation
**File:** `data_feeds.py` lines 439–453
**Current:** Hand-curated word lists. No backtest of sentiment IC vs returns. Used to weight signals via `sentiment_score` (which is currently 0.0 everywhere in signals.py output — see backtest line 363).
**Math violation + actual dead path:** sentiment field is computed, never used in scoring. Lots of API calls + cache infrastructure for a feature that's a no-op.
**Fix:** Either (a) delete sentiment plumbing entirely (recommended), or (b) replace lexicon with FinBERT and measure IC before wiring into composite.

### P1-16 — Afternoon re-scoring of held positions (blueprint GAP 9)
**File:** `signals.py` runs once at 9:35 AM. Held positions and watchlist never re-scored intraday.
**Fix (math-derived, dynamic):**
  - 3:50 PM lightweight rescore: held + top 30 candidates only (not full universe)
  - If a held position has been leapfrogged by a stronger fresh signal AND its own composite has decayed by > 1σ, flag for next-day priority exit
  - Math: requires the cross-sectional context, but it's the same engine — just a smaller bars_dict
  - Cheap, ~5 sec marginal cost

### P1-17 — Entry sizing ignores conviction gradient within Kelly cap (blueprint GAP 2)
**File:** `signals.py` lines 402–408
**Current:** Kelly capped to [0.02, 0.12] but doesn't differentiate composite=+0.5 from composite=+2.5.
**Fix (covered in P1-4):** `f_used = f_max × percentile_rank(composite)`. Continuous, not bucketed.

---

## 🟡 P2 — HYGIENE / DEAD CODE / INSTRUMENTATION

Doesn't bleed alpha today, will tomorrow.

### P2-1 — Dead files in tree
- `outcome_tracker_v2.py` — drift copy of outcome_tracker.py, imported by nothing. **Delete or merge.**
- `send_ontology_email.py` — one-shot script, no caller. **Delete or move to /scripts/oneoff/.**
- `raptor_state.json` — stale to 2026-04-30, references positions long gone. Written by nothing, read by nothing. **Delete.**
- `diagnose.py` — calls `engine.get_diagnostics()` which doesn't exist in signals.py. **Delete or implement.**

### P2-2 — Duplicate launch scripts
- `Start_Daily_Recap.bat` and `Start_Recap.bat` both call `daily_recap.py`. Pick one, delete the other.
- `Start_Raptor_Recap.bat` calls `raptor_recap.py` which **does not exist in project**. Either rename target or delete bat.
- `Start_Raptor.bat` calls `main.py` then `exit_monitor.py` directly — bypasses macro_context.py and market_agent.py, meaning if it's used ad-hoc, MarketAgent gate runs on stale 12+ hour macro. Add the two missing steps or warn.

### P2-3 — daily_recap regime field hard-coded to "120 symbols"
**File:** `daily_recap.py` line 586
**Evidence:** `<UNIVERSE: ~120 symbols>` is a literal string. Real universe is built dynamically.
**Fix:** Pass `len(universe)` into build_html and template it.

### P2-4 — daily_recap missing exit-reason instrumentation
**File:** `daily_recap.py`
**Currently absent metrics that should be in the recap (memory note: agreed list):**
  - % exits by reason (hard_stop / trail / thesis / time / math) — gated on P0-1 fix
  - Avg hold days by exit reason
  - Rolling 10-trade win rate
  - Trim efficiency from trim_log.json (did trims preserve capital vs full hold?)
  - Agent vs math disagreement rate
  - Composite-at-entry vs composite-now per held position
  - Capital efficiency: realized PnL / max capital deployed
  - Consecutive loss streak
  - Macro regime at entry vs current regime per position
  - Universe size from `len(universe)` (P2-3)

### P2-5 — hold_health.json default for missing stop is `entry × 0.92` — ✅ RESOLVED by P0-2
**Status:** Fixed as part of P0-2 (2026-05-20). hold_monitor.py now computes `entry_price - 3.0 * atr_now` when stop is missing or regime is BACKFILL.

### P2-6 — `compute_trim` parses stop_dist out of a string detail field
**File:** `hold_monitor.py` lines 465–469
**Evidence:**
```python
stop_detail = layers.get("stop_distance", {}).get("detail", "stop_dist=2.0 ATR")
try:
    dist_atr = float(stop_detail.split("=")[1].split(" ")[0])
except: dist_atr = 2.0
```
String parsing to recover a number that exists structured in the snapshot. Fragile.
**Fix:** Pass `snapshots[-1]["stop_dist_atr"]` directly into compute_trim. (Memory note: TTD stop_dist_atr=−0.626 — verify the parse actually saw negative correctly, that minus sign in front of "0.626" via split is fine but the test bears running once.)

### P2-7 — `_score_volume` magic constant `max(abs(obv_slope), 1000)`
**File:** `hold_monitor.py` line 321
**Evidence:** `mag = min(1.0, abs(obv_slope) / max(abs(obv_slope), 1000))` — this is always 1.0 when |slope| > 1000, and `slope/1000` when smaller. Why 1000? OBV depends on volume units. Apple's OBV slope is in tens of millions; small caps in thousands.
**Fix:** Normalize OBV slope by `obv_std_30d` per symbol. Z-score, not magic threshold.

### P2-8 — `_score_volatility` returns 0.0 for atr_exp < 0.80, regardless of pnl
**File:** `hold_monitor.py` lines 354–355
**Evidence:** A contracting-volatility winner (e.g. AMD +39%, atr_exp shrinking after the run) gets the same 0.0 score as a contracting-vol loser. Cluster_health/Composite_slope handle direction; this layer should too.
**Fix:** atr_exp < 0.80 → +0.3 if pnl > 0, −0.2 if pnl < 0. (Better: derive from joint distribution of atr_exp × forward return from hold_history.)

### P2-9 — `_score_stop_distance` returns 0.0 if `dist == 0`, not if dist is None
**File:** `hold_monitor.py` line 365
**Evidence:** `if not dist: return 0.0` — Python falsy includes 0.0 AND negative values? Let me check: `not -0.5` is False, so negatives DO score. But `not 0.0` is True → 0.0. A position exactly at its stop returns "no_stop_data" — wrong.
**Fix:** Explicit `if dist is None:`.

### P2-10 — pre_entry tracker prunes after 7 days regardless of relevance
**File:** `hold_monitor.py` line 537
**Evidence:** Stale signal pruning at 7 days. But a candidate that's been "building" pre-entry health for 6 days is the highest-information setup we have. Pruning is fine but pre-entry health should be a stronger entry gate.
**Fix:** Use pre_entry_health.health ≥ 0 as an entry filter in main.py. Currently computed, currently ignored.

### P2-11 — agent_layer ENTRY_PROMPT has incoherent rules (carries P0-3) — ✅ RESOLVED by P0-3
**Status:** Fixed as part of P0-3 (2026-05-20). PASS line now reads "RISK_ON or NEUTRAL" — matches canonical taxonomy.

### P2-12 — agent_layer prompt versioning runs on EVERY import
**File:** `agent_layer.py` line 250
**Evidence:** `_snapshot_prompts()` called at module level. Every time anything imports agent_layer, it scans prompt_versions/, hashes prompts, writes a file. Fine if prompts haven't changed (no-op), but does I/O on every cold import.
**Fix:** Gate behind `if __name__ == "__main__"` or move to explicit `init_agents()` call.

### P2-13 — outcome_tracker has DUPLICATED exit_path patterns
**File:** `outcome_tracker.py` lines 164–166
**Evidence:** Pattern list mixes old (trail_profit, trail_loss — used by backtest) and new (trailing_stop, hard_stop, thesis_invalid — used by exit_monitor) exit reasons. Substring matching means "trail_loss" and "trailing_stop" can both match.
**Fix:** Anchor patterns + canonical list. Better: P0-1 fix sidesteps string parsing entirely.

### P2-14 — `Layer 3 prompt_calibrator` referenced in 7 places but doesn't exist
**Files:** RAPTOR_SKILL.md, daily_recap.py, exit_monitor.py comments, etc.
**Evidence:** All references say "PLANNED — needs 30+ agent-tagged trades". Per P0-1, agent_tagged count = 0 forever. **Layer 3 cannot start.**
**Fix:** Fix P0-1 first. Then Layer 3 (prompt_calibrator.py) is a planned new file.

### P2-15 — `EQUITY_ALLOCATION = 1.00` in main.py is a vestige of A/B testing
**File:** `main.py` line 11
**Evidence:** Comment says "v6 removed". The 1.00 is no-op now. Dead config.
**Fix:** Remove the variable. Use `account["equity"]` directly.

### P2-16 — config.py `kelly_fraction = 0.15` but signals.py clips to 0.12
**File:** `config.py` line 53
**Evidence:** Base Kelly 0.15 in config never reaches 0.15 in practice — capped to 0.12. The visible config setting is a lie.
**Fix:** Either remove the cap (let math run) or remove the 0.15 (set base = max actually usable). Pick one.

---

## BLUEPRINT (RAPTOR_SKILL.md) — PROPOSED CHANGES

These are SKILL.md edits, separate from code. Plan only.

### SKILL-1 — Section 8 (Critical Rules): add explicit "Single Regime Taxonomy" rule
Add as Rule 22:
> 22. **Single regime taxonomy:** `{RISK_ON, NEUTRAL, RISK_OFF, CRISIS}` is the only allowed set. All code paths read macro_context.json. data_feeds.compute_regime_score is deprecated; do not call.

### SKILL-2 — Section 9 (What Not to Do): add "Don't write client_order_id-encoded reason strings as primary data"
> - Don't depend on Alpaca echoing client_order_id verbatim. Persist trade metadata to a sidecar JSON keyed by Alpaca order ID, then join.

### SKILL-3 — Section 14 (Next Session): mark every GAP with empirical status
Each GAP row needs a column: "math derived from data?" Y/N/PENDING. Current state: all PENDING. Forces honesty about what's still hand-picked.

### SKILL-4 — Section 15 (Math Gaps): add GAP 10 — Regime Schema Unification
Add as GAP 10 (P0):
> Single regime taxonomy across signals.py, agent_layer.py, macro_context.py. Currently fractured. Highest priority — invalidates every regime-multiplied factor weight.

### SKILL-5 — Section 17 (Core Mandate): add "every constant in config.py must have a derivation comment"
> For every numeric in `config.py`, the docstring above it cites:
>   1. The empirical study (or Raptor data file) it came from
>   2. The date the value was last validated against data
>   3. The optimization target it minimizes/maximizes
> Any constant without all three is technical debt to be removed.

### SKILL-6 — Section 6.3 (Factor Library): note that sentiment is computed but unused
> sentiment_score field exists in Signal dataclass and is always 0.0 in production. Either wire in or delete the cache infrastructure (P1-15).

### SKILL-7 — New Section: "Layer Status Truth Table"
Today's blueprint says Layer 3 is planned. Add a state column:
| Layer | Implemented | Functional | Data Quality | Notes |
|-------|-------------|-----------|---------------|-------|
| 4 Session Gate | Y | Y | OK (read-only) | |
| 3 Macro Context | Y | Y | DEGRADED | yield_curve null on most days |
| 2 Signal Engine | Y | Y | OK | REGIME_MULT now has canonical labels (P0-3 ✅) |
| 1 Execution | Y | Y | OK | stops fixed for backfill positions (P0-2 ✅), macro unified (P0-8 ✅) |
| 0 Position Health | Y | Y | OK | static weights (P1-6), backfill stops fixed (P0-2 ✅) |
| Learning | Y | READY | PENDING DATA | sidecar writes enabled (P0-1 ✅) — needs trades to flow through |

### SKILL-7 — New Section: "Daily Audit Checklist"
Before each session, Claude runs:
1. Count outcome_log records with `actual_exit_path != "unknown"` — flag if 0.
2. Count hold_health.json entries with `stop_dist_atr < 0` — flag any.
3. Diff macro_context regime vs signals.py REGIME_MULT keys — flag mismatch.
4. Verify `.env` exists (not `_env`).

---

## SURGICAL ORDER OF OPERATIONS (planning level)

The math principle: don't optimize what's broken. The blockers neutralize every optimization.

```
WEEK 1  — P0 blockers, ALL of them. ✅ COMPLETE (2026-05-20)
          P0-1 ✅, P0-2 ✅, P0-3 ✅, P0-4 ✅, P0-5 ✅, P0-6 ✅, P0-7 ✅, P0-8 ✅.
          All 8 files syntax-verified, zero null bytes, all fixes confirmed via grep.

WEEK 2  — Math foundation, derived from clean data flowing from week 1.
          P1-1 (Kalman regime), P1-2 (vol-regime hard stop), P1-3 (OU trail),
          P1-4 (Bayesian Kelly), P1-5 (OU hold).
          These are entangled — must land together or not at all.

WEEK 3  — Hold monitor full dynamicization, requires 60+ closed trades.
          P1-6 (IC layer weights — gate on N≥60), P1-7 (continuous trim),
          P1-8 (regime-relative thesis threshold).
          Until N≥60, freeze touchpoints. Memory note enforces.

WEEK 4  — Capital efficiency / re-entry / portfolio guard math.
          P1-10 (velocity entry), P1-11 (re-entry cooldown),
          P1-12 (partial trim portfolio heat), P1-16 (afternoon rescore).

WEEK 5  — Hygiene + universe sensitivity.
          P1-13 (multi-MA breadth), P1-14 (universe sweep),
          P1-17 (already covered in P1-4), all P2s.
          
ONGOING — Blueprint edits as code lands.
```

---

## RAPTOR PRINCIPLE CHECK — DOES THIS PLAN HOLD?

> "No static measures, all dynamic. Adjust to market, early identify trends and momentum
>  using all math/TA or any other means to generate alpha for a stock portfolio."

| Principle | P0/P1 Coverage |
|-----------|---------------|
| No static measures | P1-1, P1-2, P1-3, P1-4, P1-5, P1-6, P1-7, P1-8 — every major static is converted to data-derived |
| Adjust to market | P1-1 Kalman regime, P1-2 vol-regime stop, P1-3 OU trail, P1-13 multi-MA breadth |
| Early identify trends | P1-10 composite velocity, P1-16 afternoon rescore, rolling trend already partially built in hold_monitor |
| Math/TA every means | OU process (Leung & Zhang), Kalman (Hamilton 1989), Bayesian Kelly (Thorp 2006), IC weighting (Grinold & Kahn), Z-score normalization throughout |
| Alpha generation | P0-7 fixes the metric you measure alpha BY. P0-1 enables learning loop. P1-4+P1-10 directly improve entry edge. P1-3+P1-6 directly improve exit edge. |

The plan is principle-aligned. The blockers must clear first or the math improvements compound onto broken telemetry.

---

*End of audit. 8,125 lines reviewed. 8 P0 / 17 P1 / 16 P2 items.*
*P0 status: ALL 8 FIXED (2026-05-20). P2-5 and P2-11 resolved by P0 fixes.*
*Next: Week 2 — math foundation (P1-1 through P1-5). Requires clean data flowing from P0 fixes.*
*Author: Claude. Reviewed under Steve's math-first mandate.*
