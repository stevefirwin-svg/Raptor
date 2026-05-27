# RAPTOR Bat File Audit
*Audited from source: 2026-05-27*

---

## 24-Hour Schedule (What Actually Runs and When)

```
TIME ET         BAT FILE                    SCRIPTS RUN (in order)
─────────────────────────────────────────────────────────────────────────────
9:00 AM         (manual / scheduled)        macro_context.py
9:15 AM         (manual / scheduled)        market_agent.py
9:28 AM         Start_PreMarket.bat         hold_monitor.py --pre
9:35 AM         Start_Entry.bat             main.py
9:35 AM         Start_Intraday_Monitor.bat  [LOOP starts — see below]
9:35–3:50 PM    Start_Intraday_Monitor.bat  exit_monitor.py → hold_monitor.py (every 30 min)
3:50 PM         Start_Afternoon_Monitor.bat exit_monitor.py → hold_monitor.py → daily_recap.py
4:30 PM         Start_Recap.bat             daily_recap.py
5:00 PM         Start_Analysis_Lab.bat      factor_lab.py → kelly_engine.py
After close     (manual / scheduled)        outcome_tracker.py
End of day      Daily_GitHub_Push.bat       git add/commit/push

Continuous:     Start_Watchdog.bat          watchdog.py (every 15 min, market hours only)
Continuous:     Start_Crypto.bat            crypto_engine.py (every 30 min, 24/7)
Occasional:     Start_Viper.bat             options_engine.py (every 30 min)
```

---

## Bat File Details

### `Start_PreMarket.bat` — 9:28 AM
```
python hold_monitor.py --pre
```
Runs hold_monitor in pre-entry mode. Scores all current positions before the entry scan. Writes `hold_health.json` and `hold_history.json`. This is the only health data available when `main.py` runs at 9:35.

**Status:** ✅ Correct. Single script, no ordering issue.

---

### `Start_Entry.bat` — 9:35 AM
```
python main.py
```
Runs the entry scanner. Generates signals, applies gates, submits BUY orders, writes `position_ledger.json` and `composite_cache.json`.

**Status:** ✅ Correct. Single script, no ordering issue.

---

### `Start_Intraday_Monitor.bat` — 9:35 AM to 3:50 PM (loop every 30 min)
```
python exit_monitor.py   ← WRONG ORDER
python hold_monitor.py   ← should be first
```
**BUG:** exit_monitor runs before hold_monitor. exit_monitor reads `hold_health.json` for math trim signals and health scores used in the trail modifier. If exit_monitor runs first, it uses the previous cycle's health data — potentially 30+ minutes stale.

**Fix required:** Swap order to hold_monitor first, then exit_monitor.

---

### `Start_Morning_Monitor.bat` — (appears to be a one-off morning run)
```
python exit_monitor.py   ← WRONG ORDER
python hold_monitor.py   ← should be first
```
**BUG:** Same ordering bug as intraday loop. exit_monitor runs before hold_monitor.

**Fix required:** Swap order.

---

### `Start_Afternoon_Monitor.bat` — 3:50 PM
```
python exit_monitor.py   ← WRONG ORDER (current file on disk)
python hold_monitor.py   ← should be first
python daily_recap.py
```
**BUG:** Same ordering bug. Already fixed in the corrected file delivered this session — but the on-disk version still has the wrong order until Steve downloads and replaces it.

**Fix required:** Already generated — download from session outputs.

---

### `Start_Raptor.bat` — (appears to be a combined single-run entry+exit)
```
python main.py
python exit_monitor.py
```
Runs entry scan then immediately runs exit_monitor. This is used for manual/ad-hoc runs.

**Issue:** exit_monitor runs without hold_monitor having run first. On a fresh start this means exit_monitor uses whatever `hold_health.json` exists from the last run (potentially from yesterday). Not dangerous for a manual run but worth noting.

**Status:** ⚠️ Minor — acceptable for manual use but add a note in the file.

---

### `Start_Watchdog.bat` — Continuous, 9:35–3:50 PM (every 15 min)
```
python watchdog.py (loop every 15 min)
```
Watchdog is an intraday-only hard stop and trail monitor. Runs every 15 minutes. Does NOT run hold_monitor or update `hold_health.json`.

**Critical bug found:** Watchdog submits sell orders to Alpaca but **never writes to the ledger**. When watchdog exits a position:
- Alpaca sells the shares ✅
- `position_ledger.json` is NOT updated ❌
- `outcome_log.json` is NOT updated ❌
- `cooldown_log.json` is NOT updated ❌
- `trim_log.json` is NOT updated ❌

The position stays in the ledger as OPEN even after watchdog closes it. On the next exit_monitor cycle, exit_monitor will try to manage a position that no longer exists in Alpaca — it will get a 0-share position or an error.

Also: watchdog reads `hold_health.json` for `high_water` but `hold_health.json` is NOT written by hold_health — the `snapshot` key in hold_health does not contain a `high_water` field. Watchdog reads `health_rec.get("high_water", 0)` — this will always be 0 unless hold_monitor explicitly writes it there. **Watchdog high_water is always 0** which means it falls back to `max(0, entry, price) = max(entry, price)` — same as the old broken behavior. The fix applied to exit_monitor (reading from ledger metadata) was NOT applied to watchdog.

---

### `Start_Daily_Recap.bat` — Debug wrapper for daily_recap.py
```
python daily_recap.py
```
Captures all output to `logs\recap_error.txt`. Used for debugging recap failures.

**Status:** ✅ Correct. No ordering issues.

---

### `Start_Recap.bat` — 4:30 PM
```
python daily_recap.py
```
Simple recap runner without the debug wrapper.

**Status:** ✅ Correct. Duplicate of Start_Daily_Recap.bat functionality — consider consolidating.

---

### `Start_Analysis_Lab.bat` — 5:00 PM
```
python factor_lab.py
python kelly_engine.py
```
Runs IC validation and Kelly sizing after close. Correct order — factor_lab first (generates IC report), then kelly_engine (uses trade outcomes).

**Status:** ✅ Correct order. No issues.

---

### `Start_Raptor_Recap.bat`
```
python raptor_recap.py
```
Runs `raptor_recap.py` — this file does not exist in the repo. The bat file will fail silently.

**Status:** ❌ Dead bat file — references a script that doesn't exist.

---

### `Daily_GitHub_Push.bat` — End of day
```
git add .
git commit -m "Daily update YYYY-MM-DD"
git push
```
**Issues:**
1. Uses `git add .` not `git add -A` — will miss deletions
2. Commit message is always the same generic string — provides no diagnostic value
3. No error checking — if push fails it logs nothing useful

**Status:** ⚠️ Works but poor hygiene.

---

### `Start_Crypto.bat` — Continuous 24/7
```
python crypto_engine.py (every 30 min)
```
Runs crypto engine continuously. No dependency on other Raptor components.

**Status:** ✅ Self-contained. No ordering issues.

---

### `Start_Viper.bat` — On demand
```
python options_engine.py (every 30 min)
```
Runs options engine on demand.

**Status:** ✅ Self-contained. No ordering issues.

---

### `Start_Watchdog.bat` — Continuous market hours
```
python watchdog.py (every 15 min)
```
See watchdog bugs above.

**Status:** ❌ Two bugs — no ledger writes on exit, high_water always 0.

---

## Bug Summary

| # | Bug | File | Severity | Status |
|---|-----|------|----------|--------|
| 1 | exit_monitor runs before hold_monitor | Start_Intraday_Monitor.bat | High | ❌ Open |
| 2 | exit_monitor runs before hold_monitor | Start_Morning_Monitor.bat | High | ❌ Open |
| 3 | exit_monitor runs before hold_monitor | Start_Afternoon_Monitor.bat | High | ✅ Fixed (file generated) |
| 4 | watchdog exits never written to ledger — position stays OPEN in ledger after watchdog sells | watchdog.py | Critical | ❌ Open |
| 5 | watchdog reads high_water from hold_health.json snapshot (field doesn't exist) — always 0 | watchdog.py | High | ❌ Open |
| 6 | Start_Raptor_Recap.bat references raptor_recap.py which doesn't exist | Start_Raptor_Recap.bat | Low | ❌ Open (dead file) |
| 7 | Daily_GitHub_Push.bat uses git add . (misses deletions), generic commit message | Daily_GitHub_Push.bat | Low | ❌ Open |

---

## Correct Run Order (Every Cycle)

```
hold_monitor.py     → writes hold_health.json (health scores, trim recommendations)
exit_monitor.py     → reads hold_health.json, executes exits/trims, updates ledger
```

This order must be enforced in every bat file that runs both. hold_monitor is always first.

---

## Recommended Fixes

**Fix 1 — Swap order in Start_Intraday_Monitor.bat and Start_Morning_Monitor.bat:**
Change every occurrence of `exit_monitor.py` before `hold_monitor.py` to the correct order.

**Fix 2 — watchdog.py: write to ledger on exit:**
After a successful Alpaca sell, call `Ledger().record_exit()` (full exit) or `record_trim()` (partial). Also call `outcome_tracker.run_tracker()` and `_record_stopout_cooldown()` on hard_stop — same as exit_monitor does.

**Fix 3 — watchdog.py: read high_water from ledger metadata, not hold_health:**
`hold_health.json` snapshot does not contain `high_water`. The correct source is `ledger.metadata["high_water"]` — same fix applied to exit_monitor this session.

**Fix 4 — Delete Start_Raptor_Recap.bat** or create raptor_recap.py if it's supposed to exist.

**Fix 5 — Daily_GitHub_Push.bat:** Change `git add .` to `git add -A`.
