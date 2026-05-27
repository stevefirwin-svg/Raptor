# RAPTOR Data Flow & Scenario Audit
*Audited from source: 2026-05-27 | Commit: 14d6ede + 4965dfa*

---

## Data Stores — What They Are and Who Owns Them

| File | Written by | Read by | Purpose |
|------|-----------|---------|---------|
| `position_ledger.json` | `main.py` (entry), `exit_monitor.py` (exit/trim) | `exit_monitor.py`, `hold_monitor.py`, `daily_recap.py` | Authoritative record of entries, open shares, metadata (stop, high_water), trim history, closed trades |
| `hold_health.json` | `hold_monitor.py` | `exit_monitor.py` (math trim), `daily_recap.py` | Per-position 8-layer health score, trim recommendation, snapshot |
| `hold_history.json` | `hold_monitor.py` | `hold_monitor.py` | Rolling snapshot history per position (used for slope scoring) |
| `outcome_log.json` | `outcome_tracker.py` | `factor_lab.py`, `kelly_engine.py`, `daily_recap.py` | Closed trade records with exit path, pnl, agent decisions — IC training data |
| `trim_log.json` | `exit_monitor.py` | `daily_recap.py` | Partial trim records with math detail and agent comparison |
| `composite_cache.json` | `signals.py` (via `main.py`) | `main.py` (velocity gate) | Yesterday's composite scores for velocity filtering |
| `cooldown_log.json` | `main.py` | `main.py` (cooldown gate) | Stop-out cooldown blocks per symbol |
| `macro_context.json` | `macro_context.py` | `signals.py`, `exit_monitor.py`, `hold_monitor.py` | Regime label + continuous macro_score |
| `market_decision.json` | `market_agent.py` | `main.py` | SCAN/REDUCE/STANDBY decision for the session |
| `hold_decisions.json` | `agent_layer.py` | `outcome_tracker.py`, `exit_monitor.py` (advisory log) | Hold agent decisions per position (advisory only — math governs) |
| `entry_vetoes.json` | `agent_layer.py` | `outcome_tracker.py` | Entry agent veto decisions |
| `factor_ic_report.json` | `factor_lab.py` | (reference) | IC validation results per factor |
| `kelly_estimates.json` | `kelly_engine.py` | (reference) | Bootstrap Kelly sizing output |

---

## Scenario Traces — What Happens, Where Data Goes

### SCENARIO 1: BUY (New Entry)

**Trigger:** `main.py` 9:35 AM scan

**Flow:**
1. Signal engine scores all symbols → `composite_cache.json` written (atomic)
2. Velocity gate reads `composite_cache.json` — drops decelerating signals
3. Cooldown gate reads `cooldown_log.json` — drops recently stopped-out symbols
4. Margin guard checks Alpaca buying power
5. Market agent checks `market_decision.json` — STANDBY aborts scan
6. Entry agent screens candidates → result written to `entry_vetoes.json`
7. Order submitted to Alpaca
8. On fill: `ledger.record_entry()` writes to `position_ledger.json`:
   - `symbol`, `shares`, `entry_price`, `entry_date`
   - `metadata`: `stop` (ATR-based), `t_stat`, `kelly_fraction`, `composite_score`, `regime`, `velocity`

**What is NOT written at entry:**
- `high_water` — not set at entry, only starts accumulating on first exit_monitor cycle ✅ acceptable (first cycle sets it to entry price)
- `hold_health.json` — not updated until hold_monitor runs at 9:28 pre-entry or intraday

**Gap:** `ledger._save()` is NOT atomic (uses `json.dump` directly, not `os.replace`). A crash mid-write corrupts the ledger. **This is a bug.**

---

### SCENARIO 2: HOLD (Position Survives Exit Check)

**Trigger:** `exit_monitor.py` every 30 min, 9:35–3:50 PM

**Flow:**
1. Reads Alpaca positions (source of truth)
2. Reads `position_ledger.json` for entry_date, metadata (stop, high_water)
3. Reads `hold_health.json` for health score and composite
4. Computes `high_water = max(price, entry, stored_hw)`
5. If `high_water` increased → writes back to `ledger.metadata["high_water"]` (atomic via `_save`)
6. Computes trail → if position survives → ratchets `ledger.metadata["stop"]` up to trail level
7. Position added to `holds` list — no file written for holds

**What is written on a HOLD:**
- `position_ledger.json` — `high_water` and `stop` updated if they moved up

**What is NOT written on a HOLD:**
- `hold_health.json` — this is written by `hold_monitor.py`, not `exit_monitor.py`. If hold_monitor hasn't run this cycle, health data is stale.
- No hold event recorded in any log — holds are invisible in the data

**Gap:** `ledger._save()` is not atomic. Also: exit_monitor reads `hold_health.json` but does NOT call hold_monitor inline — so the health score used for trail modifier may be from the previous hold_monitor run (up to 30 min stale or older). The trail modifier uses stale health. Low severity but worth noting.

---

### SCENARIO 3: TRIM (Partial Sell — math_trim or portfolio_heat)

**Trigger:** `exit_monitor.py` — math trim from `hold_health.json` or portfolio drawdown

**Flow:**
1. `hold_health.json` trim recommendation read (action = TRIM, trim_shares = N)
2. Order submitted to Alpaca for `safe_trim` shares (capped at qty-1)
3. On fill:
   - `ledger.record_trim()` called → reduces `shares` in `position_ledger.json`, appends to `trims[]`, keeps position OPEN ✅ (fixed this session)
   - `trim_log.json` appended with: timestamp, symbol, qty, price, pnl_pct, reason, composite, trim_detail, agent_decision
4. `outcome_tracker.run_tracker()` called — fetches Alpaca filled sell orders, tags them in `outcome_log.json` with `actual_exit_path = "math_trim"`

**What is written on a TRIM:**
- `position_ledger.json` — shares reduced, trim record appended ✅
- `trim_log.json` — partial exit record ✅
- `outcome_log.json` — tagged by outcome_tracker ✅

**Gap 1:** `trim_log.json` records `pnl_pct` as `float(pnl_pct) * 100` — but `pnl_pct` from Alpaca position is already a decimal (e.g. 0.62 = 62%). So trim_log stores `62.0` correctly. But `outcome_log.json` records from outcome_tracker use `actual_pnl_pct` computed as `(exit_price - entry_price) / entry_price * 100` from Alpaca order data directly — this is correct. **No unit bug in these two files.**

**Gap 2:** After a trim, `hold_health.json` is NOT updated — it still shows the old share count and old trim recommendation. If hold_monitor doesn't run before the next exit_monitor cycle, the same trim could be recommended again and another slice sold. This is a **double-trim risk**.

**Gap 3:** `portfolio_heat` trim goes through `record_trim` ✅ but `portfolio_heat` is NOT written to `trim_log.json` — only `math_trim` entries are logged. Portfolio heat trims are invisible in trim_log.

---

### SCENARIO 4: FULL EXIT (trailing_stop, hard_stop, thesis_invalid, time_decay, math_exit)

**Trigger:** `exit_monitor.py` — one of 5 exit paths fires

**Flow:**
1. Exit added to `exits` list with full qty
2. Order submitted to Alpaca for all shares
3. On fill:
   - `ledger.record_exit()` called → pops position from `positions`, appends to `closed[]` with exit_price, exit_date, exit_reason, exit_path, pnl (absolute $), pnl_pct (%) ✅
4. `outcome_tracker.run_tracker()` called → tags in `outcome_log.json`
5. If `hard_stop`: `_record_stopout_cooldown()` adds symbol to `cooldown_log.json`

**What is written on a FULL EXIT:**
- `position_ledger.json` — moved to closed[] ✅
- `outcome_log.json` — tagged ✅
- `cooldown_log.json` — if hard_stop ✅

**What is NOT cleaned up on exit:**
- `hold_health.json` — stale entry remains until hold_monitor next runs and overwrites. The 8 ghost positions discovered today were exactly this. hold_monitor only writes symbols currently in Alpaca, so on next run after exit the symbol is simply not written — but the old entry persists until the file is regenerated fresh. **Stale entries remain until next hold_monitor run.**
- `hold_history.json` — stale snapshot history remains. Not harmful (hold_monitor appends by symbol, will just stop getting new snapshots).

---

### SCENARIO 5: POSITION NOT IN LEDGER (Alpaca has it, ledger doesn't)

**Current handling:** `exit_monitor.py` uses Alpaca as source of truth for position list. It reads ledger for metadata (stop, high_water, entry_date) but falls back gracefully when missing:
- `days_held` → fallback = 1 (conservative)
- `high_water` → fallback = max(price, entry)
- `hard_stop` → fallback = entry - initial_atr_mult * atr (from Alpaca avg_entry)
- `stop` → fallback = atr-based calculation

**hold_monitor.py:** Also reads ledger for stop and entry_date. Falls back gracefully (warns + skips stop_dist_atr). Position is still scored.

**Verdict:** The system degrades gracefully for positions not in the ledger — it doesn't crash, it just loses trail history and accurate stop data. **This is acceptable behavior.** The backfill script restores accurate metadata.

---

### SCENARIO 6: ADD TO POSITION (Alpaca increases qty, ledger has wrong shares)

**Current handling:** There is no `record_add()` method in `ledger.py`. `main.py` only calls `record_entry()` for new positions (`signals = [s for s in signals if s.symbol not in all_held]`). If a position is already held, it will never be entered again — the gate explicitly prevents it. So adds to existing positions do not happen in the current architecture. This is intentional.

**Verdict:** Not a bug — adds are architecturally excluded.

---

## Cross-Component Data Flow Diagram

```
9:00 AM  macro_context.py  →  macro_context.json
9:15 AM  market_agent.py   →  market_decision.json
9:28 AM  hold_monitor.py   →  hold_health.json, hold_history.json
                               reads: position_ledger.json (stop, entry_date)
                                      macro_context.json
                                      Alpaca positions

9:35 AM  main.py           →  position_ledger.json (record_entry)
                               composite_cache.json
                               cooldown_log.json (if stopout)
                               entry_vetoes.json (via agent_layer)
                               reads: market_decision.json
                                      composite_cache.json (velocity gate)
                                      cooldown_log.json
                                      Alpaca account + positions

9:35–3:50 exit_monitor.py →  position_ledger.json (high_water, stop ratchet, record_trim, record_exit)
(every 30 min)               trim_log.json
                               cooldown_log.json (if hard_stop)
                               outcome_log.json (via outcome_tracker)
                               hold_decisions.json (advisory log)
                               reads: hold_health.json (math trim signal, health for trail)
                                      position_ledger.json (entry_date, stop, high_water)
                                      Alpaca positions
                                      macro_context.json (via signals.py)

9:35–3:50 hold_monitor.py →  hold_health.json
(every 30 min)               hold_history.json
                               reads: position_ledger.json (stop, entry_date)
                                      Alpaca positions + bars
```

**Critical ordering dependency:** exit_monitor reads `hold_health.json` for the math trim signal and health score. hold_monitor writes `hold_health.json`. If exit_monitor runs before hold_monitor on a given cycle, it uses the previous cycle's health data. In the bat files, the order should be: hold_monitor first, then exit_monitor. **Verify this in the bat files.**

---

## Confirmed Bugs Found This Session

| # | Bug | File | Status |
|---|-----|------|--------|
| 1 | `math_trim` called `record_exit` → closed position in ledger while Alpaca still held shares | `exit_monitor.py`, `ledger.py` | ✅ Fixed |
| 2 | `pnl_pct` stored as raw decimal (0.536) not percentage (53.6%) | `ledger.py` | ✅ Fixed |
| 3 | `high_water` recomputed fresh each exit_monitor cycle → never accumulated → stop never trailed | `exit_monitor.py` | ✅ Fixed |
| 4 | Stop never written back after trail computation → stop stayed at entry level forever | `exit_monitor.py` | ✅ Fixed |
| 5 | `ledger._save()` not atomic — direct `json.dump`, no `os.replace` | `ledger.py` | ❌ Open |
| 6 | `hold_health.json` not cleaned up on exit → ghost positions persist until next hold_monitor run | `hold_monitor.py` | ❌ Open |
| 7 | `portfolio_heat` trims not written to `trim_log.json` | `exit_monitor.py` | ❌ Open |
| 8 | Double-trim risk: hold_health trim recommendation persists until hold_monitor re-runs; exit_monitor could fire same trim twice in one cycle | `exit_monitor.py` | ❌ Open |
| 9 | exit_monitor/hold_monitor run order not enforced — exit may use stale health | bat files | ❌ Open (verify) |

---

## Remaining Open Bugs — Fix Order

### Bug 5: `ledger._save()` not atomic (HIGHEST PRIORITY)

**File:** `ledger.py`, `_save()` method
**Risk:** Crash during write corrupts `position_ledger.json` — all position metadata lost.
**Fix:**
```python
def _save(self):
    tmp = self.path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(self.data, f, indent=2, default=str)
    os.replace(tmp, self.path)
```

---

### Bug 6: Ghost positions in hold_health.json after exit

**File:** `hold_monitor.py`, `run_monitor()`
**Risk:** Low — hold_monitor reads Alpaca positions, so ghosts don't affect scoring. But analytics reading `hold_health.json` directly see stale data.
**Fix:** At the start of `run_monitor()`, load current Alpaca symbols and remove any keys from `hold_health.json` that are no longer held before writing the new file.

---

### Bug 7: portfolio_heat trims not in trim_log

**File:** `exit_monitor.py`, trim log block
**Risk:** Low — portfolio heat trims are invisible in calibration data.
**Fix:** Change the trim log filter from `"trim" in e.get("reason", "")` to also include `"portfolio_heat"`.

---

### Bug 8: Double-trim risk

**File:** `exit_monitor.py`
**Risk:** Medium — if exit_monitor runs twice before hold_monitor updates, the same trim recommendation fires twice, selling more than intended.
**Fix:** After executing a trim, write a `last_trim_ts` to the position metadata in the ledger. In the math trim block, skip symbols where `last_trim_ts` is within the last 30 minutes.

---

## What Is Clean (Verified Working)

- Alpaca as source of truth for position list — exit_monitor never uses ledger for this ✅
- `record_trim` keeps position open, reduces shares, logs trim history ✅
- `record_exit` moves to closed, pnl_pct in correct % units, exit_path set ✅
- `high_water` now accumulates and persists across cycles ✅
- Stop ratchets up to trail level each surviving cycle ✅
- `outcome_tracker` correctly reads `client_order_id` to detect exit path ✅
- `math_trim` correctly tagged in `outcome_log.json` as `math_trim` ✅
- IC/Kelly correctly exclude `math_trim` partials from calibration ✅
- Atomic writes on `composite_cache.json`, `outcome_log.json`, `trim_log.json`, `cooldown_log.json` ✅
- Fallback behavior for missing ledger metadata is graceful (warns + skips, never fabricates) ✅
