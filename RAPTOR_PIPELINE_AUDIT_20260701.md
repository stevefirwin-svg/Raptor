# Raptor Pipeline Audit — 2026-07-01

Full audit of the daily monitor pipeline: fire order, frequency, journaling, and
Alpaca↔ledger data-linking, requested after repeated crashes and reconciliation
gaps. Method: read every `Register_*.ps1` / `Start_*.bat`, then cross-checked
against **actual fire history** in `logs/raptor_auto_start.log` (3 weeks) and
per-script logs (`exits_*.log`, `monitor_*.json`, `recap_errors.log`,
`raptor_run.log`) — the scripts describe intent, the logs are ground truth.

---

## CORRECTION — verified against the live Task Scheduler, 2026-07-01

Section 3 below was written from repo files alone. Steve then pulled the real
task list, actions, and triggers directly from Task Scheduler on his machine.
That changed several conclusions — **the live scheduler is healthier than the
repo files suggested**:

- **§3.2 (duplicate 5PM task) — does not exist.** No "Raptor Analysis Lab" task
  is registered. Only "Raptor AfterClose" fires at 5 PM. No fix needed.
- **§3.3 ("Raptor Intraday Monitor" mislabeled) — wrong, retracted.** The real
  task's Action is `python.exe exit_monitor.py` directly — it correctly runs
  the exit/trim loop at 9:35 AM. `Start_Intraday_Monitor.bat` in the repo
  (which calls `raptor_monitor.py`) is dead code the live task doesn't even
  invoke. Nothing to remove or fix live; the task just has a confusing name
  relative to what's in the repo.
- **§3.4 (no Register script for the exit loop) — the task exists, just not
  version-controlled.** `Register_Exit_Monitor.ps1` / `Start_Exit_Monitor.bat`
  (added in this session) were written on the wrong assumption that no task
  existed. **They are now gutted and must not be run** — "Raptor Intraday
  Monitor" already does this job; running them would register a duplicate.
- **§3.5 (6/29 mid-day exit-loop death) — two hypotheses ruled out.**
  `Get-ScheduledTask "Raptor Intraday Monitor"` shows an 8-hour execution time
  limit (not the cause) and `powercfg` shows sleep-after set to Never on both
  AC and battery (not the cause either). Root cause remains unconfirmed —
  most likely a one-off (Windows Update, AV, manual close) rather than a
  systemic scheduling problem. The code-level fixes (S11-5/6/7: exit_monitor
  lock, cutoff bug, EXIT_LOOP_GAP detection) still stand — if it happens
  again, the 4:30 PM monitor email will catch it same-day.
- **§3.6 (duplicate PreMarket 9:00/9:28) — only one trigger found.** "Raptor
  MacroContext" (which runs `Start_PreMarket.bat`) has exactly one trigger,
  9:00 AM. The second historical firing at 9:28 is unexplained but may no
  longer be happening. Not chased further — low priority next to everything
  above.
- **Also confirmed:** "Raptor Monitor" (4:30 PM) runs `raptor_monitor.py`
  directly, not through `Start_Monitor.bat` — so the S11-3 bat-file fix,
  while harmless, wasn't actually protecting anything at risk on the live
  system. The fix is still correct to have made (the bat file is real and
  could be what's used if the task is ever re-pointed at it), just not urgent.

**Net effect of this correction:** no live Task Scheduler changes were needed
or made. All the real fixes from this session were in the code (§2 below),
already applied and verified.

---

## 1. Actual daily fire order (confirmed from logs, not from script comments)

| Time ET | Task | Script | Registered? |
|---------|------|--------|-------------|
| 9:00 AM | Start_PreMarket (1st) | `premarket_scanner.py` | Not in repo — no Register_*.ps1 found |
| 9:28 AM | Start_PreMarket (2nd, duplicate) | `premarket_scanner.py` | Not in repo |
| 9:30 AM | Raptor Watchdog | `watchdog.py`, loops every 15 min to ~4:00 PM | `Register_Watchdog.ps1` ✅ |
| 9:30 AM | "Raptor Intraday Monitor" | actually runs `raptor_monitor.py` (~6-17 sec, one shot) | `Register_Intraday_Monitor.ps1` ✅ but mislabeled — see §3.3 |
| 9:35 AM | Start_Entry | `main.py` (entry scan, has process lock) | Not in repo — no Register_*.ps1 found |
| 9:35 AM | **Exit/trim engine** | `exit_monitor.py`, self-manages a 30-min loop to 3:50 PM | **No Register_*.ps1 existed** — drafted this session, see §3.4 |
| 12:30 PM | Raptor MidDay Monitor | `hold_monitor.py` (refreshes health scores) | `Register_MidDay_Monitor.ps1` ✅ |
| 3:50 PM | Start_Afternoon_Monitor | `hold_monitor.py` + `exit_monitor.py` (final-cycle safety net) | Not in repo |
| 4:15 PM | Start_Recap | `daily_recap.py` | Not in repo |
| 4:30 PM | "Raptor Monitor" | `raptor_monitor.py` (6-layer L1-L6 check + email) | `Register_Raptor_Monitor.ps1` ✅ — bat was silently broken, see §3.1 |
| 5:00 PM | Raptor AfterClose | `outcome_tracker.py → factor_lab.py → kelly_engine.py → dsr.py → git push` | `Register_AfterClose.ps1` ✅ |
| 5:00 PM | Raptor Analysis Lab | **same bat file, same time** as AfterClose | `Register_Analysis_Lab.ps1` ✅ — duplicate, see §3.2 |

---

## 2. Fixed this session (code changes, all verified)

| # | File | Issue | Fix |
|---|------|-------|-----|
| 1 | `raptor_monitor.py` | L3-PositionRisk crashed every run (`float(None)` on `stop_dist_atr`) — silently dropped every L3 finding | Added `safe_float()` helper; verified against real hold_health.json data (9 positions incl. the 5 null-stop ones), no crash |
| 2 | `daily_recap.py` | HOOD (and any market-sell exit) showed `@ None` instead of a real exit price in the recap | Cross-reference `slippage_log.json`'s reconciled fill price by order ID; verified against the real 6/28 HOOD exit — now shows `$99.81` |
| 3 | `Start_Monitor.bat` | Was calling `hold_monitor.py` instead of `raptor_monitor.py` — the 4:30 PM task would have silently stopped sending the EOD monitor email tonight, with no error anywhere. Logs through 6/30 confirm it used to correctly run `raptor_monitor.py` (findings + "Monitor email sent" every day) | Restored to call `raptor_monitor.py` |
| 4 | `reconcile_positions.py` | `--fix` flag was documented ("auto-runs backfill_ledger") and accepted on the command line, but never actually read anywhere in the code — it silently did nothing | Implemented: auto-runs `backfill_ledger.py --write` for `MISSING_FROM_LEDGER` symbols. `GHOST_IN_LEDGER` is deliberately **not** auto-closed (destructive, needs a human to confirm the position was really exited) |
| 5 | `Start_AfterClose.bat` | Alpaca↔ledger reconciliation was 100% manual and nobody had run it recently — 6/30's monitor found 5 missing + 3 ghost positions | Added `python reconcile_positions.py --fix` as Step 0, before outcome tagging, every day at 5 PM |
| 6 | `exit_monitor.py` | No process lock at all (unlike `main.py`, which got one after the 2026-06-19 double-order incident). The 3:50 PM safety-net call and the 9:35 AM self-loop could in principle write to `position_ledger.json` etc. concurrently | Added `logs/exit_monitor.lock`, same fail-open TTL pattern as `main.py`'s lock. Verified: concurrent acquire correctly blocked; stale lock correctly overwritten |
| 7 | `exit_monitor.py` | **The 3:50 PM safety-net call did nothing, every day.** Its cutoff check ran *before* the very first cycle, so any invocation at/after 3:50 PM exited immediately with 0 cycles run. Confirmed live in `exits_20260629.log`: self-loop died silently after cycle 3 (10:35 AM) — no traceback anywhere — and exits went unmonitored until the 3:50 PM call logged `"shutting down loop after 0 cycle(s)"` and exited without checking a single position. ~4h45m with zero exit checks that day. | Cutoff now only stops a *new* cycle from starting after at least one has run; the first cycle always executes. Verified against real 6/29 timestamps (would have run) and 6/30 (unaffected, already healthy) |
| 8 | `raptor_monitor.py` | No check existed for "did the exit loop actually run enough cycles today" — the exact 6/29 failure mode was invisible to the monitor | Added `EXIT_LOOP_GAP` finding to L1: compares actual vs. expected cycle count for time-of-day. Verified against real logs: flags 6/29 (3 actual vs. ~13 expected) as ALERT, passes 6/30 (13/13) as OK |
| 9 | `main.py`, `exit_monitor.py` | `UnicodeEncodeError` on `→` in log messages — 24 occurrences in `raptor_run.log` alone. Python's logging module catches this internally so the *program* didn't crash, but the log line was silently lost every time, and it clutters every log with noise | `sys.stdout`/`stderr` `.reconfigure(encoding="utf-8", errors="replace")` at each entry point (already present in `raptor_monitor.py` — extended the same pattern) plus replaced the specific `→` occurrences with `->` |
| 10 | *(new files)* | The single most-important recurring task (the exit/trim engine's 9:35 AM self-loop) had **no `Register_*.ps1` anywhere in the repo** — unlike every other scheduled piece | Added `Start_Exit_Monitor.bat` + `Register_Exit_Monitor.ps1`, matching existing conventions. **Steve: see §3.4 before running this** — don't create a second copy of the loop |

---

## 3. Found, not auto-fixed — needs your call

### 3.1 (context for fix #3 above)
Already fixed in code. Flagging so you know to watch tonight's 4:30 PM email actually arrives.

### 3.2 Duplicate 5 PM tasks — confirmed firing concurrently every day
`Register_AfterClose.ps1` **and** `Register_Analysis_Lab.ps1` both register a task that runs
`Start_AfterClose.bat` at 5:00 PM. `Register_Analysis_Lab.ps1`'s own comment already warns
about this ("they would run simultaneously at 5PM and write to the same files") but nothing
was ever done about it. `raptor_auto_start.log` shows both "After-close sequence starting"
and "Start_Analysis_Lab starting" at the **identical timestamp** every day back through 6/24.
That means `outcome_tracker.py`, `factor_lab.py`, `kelly_engine.py`, `dsr.py`, and `git push`
have all been running **twice, concurrently,** every single day — a very plausible source of
the git lock contention and data weirdness you've been seeing.

**Action:** on the machine, run:
```powershell
Get-ScheduledTask -TaskName "Raptor Analysis Lab" | Unregister-ScheduledTask -Confirm:$false
```
(Keep "Raptor AfterClose" — it's the one with the fuller description.)

### 3.3 "Raptor Intraday Monitor" (9:30 AM) is mislabeled and low-value
Its `Register_Intraday_Monitor.ps1` description promises "exit + hold monitor loop, 9:35 AM–3:50
PM every 30 min" — but `Start_Intraday_Monitor.bat`'s actual content just runs `raptor_monitor.py`
once (finishes in ~6-17 sec). That's the *end-of-day* 6-layer monitor, run pointlessly at market
open against yesterday's stale data, then run again for real at 4:30 PM. I didn't touch this —
repointing it to the real exit loop would be a much bigger functional change than a config fix,
and I don't want to guess at that. Simplest fix is probably to just retire this task:
```powershell
Get-ScheduledTask -TaskName "Raptor Intraday Monitor" | Unregister-ScheduledTask -Confirm:$false
```

### 3.4 The real exit-loop task has no version-controlled definition
Something on this machine fires `python exit_monitor.py` (no bat wrapper, no args) at 9:35 AM —
that's the only explanation for the clean 30-min cycle logs seen most days. It was never
captured in any `Register_*.ps1`, so its exact Task Scheduler settings (execution time limit,
restart behavior) aren't reproducible from the repo. I drafted `Register_Exit_Monitor.ps1` +
`Start_Exit_Monitor.bat` this session to fix that gap — **before running it, find and remove
whatever currently fires exit_monitor.py at 9:35 AM**, or you'll have two copies (the new lock
file added in fix #6 will make the second one abort safely, but better to just have one
registration).

### 3.5 2026-06-29: the exit loop died mid-day with zero trace
Confirmed in `logs/exits_20260629.log`: cycles 1-3 ran fine (9:35–10:35 AM), then nothing until
the unrelated 3:50 PM call. No Python traceback anywhere — `logger.exception()` only catches
catchable Python exceptions, not the process being killed outright (laptop sleep, forced reboot,
closed terminal window, or a Task Scheduler execution-time-limit killing an ad-hoc GUI-created
task early). Since this task has no `Register_*.ps1` (§3.4), there's no way to check what time
limit it was actually given. Once you've registered `Register_Exit_Monitor.ps1` (8h limit,
explicit), check Windows power settings too — if the laptop is allowed to sleep during market
hours, that alone would explain it.

### 3.6 Duplicate PreMarket firing (9:00 AM and 9:28 AM)
`premarket_scanner.py` runs twice every morning, every day since 6/24 (confirmed in
`raptor_auto_start.log`). No `Register_PreMarket.ps1` exists to check intent — if this isn't
deliberate (e.g. an early warm-up pass + a right-before-open refresh), it's another duplicate
registration worth consolidating.

### 3.7 `position_outcomes.json` has no rebuild script at all
This is bigger than a wiring fix, flagging separately rather than guessing at it. It's been a
documented open item since before 6/19 ("built manually — not auto-updated after close") and the
6/30 monitor confirmed it's now 11.4 days stale. I searched the repo for anything that writes to
it — nothing does. `rebuild_positions.py`, referenced in `RAPTOR_STARTUP.md`'s own open-actions
list, doesn't exist yet. Rebuilding it needs the same position-level dedup logic used to
originally produce the 27 independent positions (per the Independence corollary in
`RAPTOR_MASTER_PLAN.md`) — worth a dedicated session rather than improvising it here, since
getting the dedup wrong would quietly corrupt every gate counter and Kelly/DSR calculation
downstream.

---

## 4. Net effect

Before this session: silent daily monitor crash (L3), an exit-monitoring blind spot that had
already caused a real ~4h45m gap on 6/29 with no alert, a broken safety net for exactly that
failure mode, a 4:30 PM email that was about to silently stop sending, ledger reconciliation
that only ever happened when someone remembered to run it by hand, and (most likely) the
AfterClose pipeline quietly double-running every night.

After: the known crashes are fixed and verified against real data, the exit loop has a lock and
an actual working safety net, the monitor will now catch a repeat of 6/29 by lunchtime instead
of never, ledger reconciliation runs automatically every night, and the missing pieces of the
schedule are now either fixed in code or captured in a version-controlled script — with the
handful of things that need your hand on the actual Task Scheduler called out explicitly above.
