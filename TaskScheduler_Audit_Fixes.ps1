# ============================================================
# Raptor Task Scheduler Audit & Fixes — 2026-07-01
# Run in an elevated (Administrator) PowerShell prompt, from any directory.
# Go phase by phase — Phase 1 is read-only and tells you what Phase 2-4
# need to actually do on your machine (I can't see your live Task Scheduler
# from here, so the exact task names in Phase 2-4 may need small edits
# based on what Phase 1 shows you).
# ============================================================

# ── PHASE 1 — DIAGNOSE: show every Raptor task with its real action + trigger ──
# Run this first. Read the output before touching anything else in this file.

Write-Host "`n=== ALL RAPTOR TASKS — action + trigger ===" -ForegroundColor Cyan
Get-ScheduledTask | Where-Object { $_.TaskName -like "*Raptor*" } | ForEach-Object {
    $task = $_
    $info = Get-ScheduledTaskInfo -TaskName $task.TaskName
    Write-Host "`n----------------------------------------" -ForegroundColor DarkGray
    Write-Host "Task:      $($task.TaskName)" -ForegroundColor Yellow
    Write-Host "State:     $($task.State)"
    Write-Host "Last Run:  $($info.LastRunTime)   Result: $($info.LastTaskResult)"
    foreach ($a in $task.Actions) {
        Write-Host "Action:    $($a.Execute) $($a.Arguments)"
    }
    foreach ($t in $task.Triggers) {
        Write-Host "Trigger:   $($t.CimClass.CimClassName)  -  StartBoundary=$($t.StartBoundary)  DaysOfWeek=$($t.DaysOfWeek)"
    }
    if ($task.Settings.ExecutionTimeLimit) {
        Write-Host "Time limit: $($task.Settings.ExecutionTimeLimit)"
    }
}
Write-Host "`n=== END OF LIST ===" -ForegroundColor Cyan
Write-Host "`nLook for:"
Write-Host " 1) Two tasks whose Action calls premarket_scanner.py (should explain the 9:00/9:28 double-fire)"
Write-Host " 2) Whatever task's Action is 'python.exe exit_monitor.py' (no bat, no args) around 9:35 AM"
Write-Host "    — this is the real exit/trim loop and it currently has no matching Register_*.ps1 in the repo"
Write-Host " 3) Confirm both 'Raptor AfterClose' and 'Raptor Analysis Lab' exist and both point at Start_AfterClose.bat`n"

# Pause here. Read the output above before running anything below.
Read-Host "Press Enter once you've reviewed Phase 1 output, or Ctrl+C to stop here"


# ── PHASE 2 — FIX: remove the confirmed duplicate 5 PM task ────────────────────
# Register_AfterClose.ps1 and Register_Analysis_Lab.ps1 both register a task
# that fires Start_AfterClose.bat at 5:00 PM. raptor_auto_start.log shows both
# firing at the identical timestamp every day back through 6/24 — meaning
# outcome_tracker/factor_lab/kelly_engine/dsr/git push have likely been
# running twice, concurrently, every night. Keeping "Raptor AfterClose" (fuller
# description) and removing "Raptor Analysis Lab".

if (Get-ScheduledTask -TaskName "Raptor Analysis Lab" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "Raptor Analysis Lab" -Confirm:$false
    Write-Host "Removed: Raptor Analysis Lab" -ForegroundColor Green
} else {
    Write-Host "Raptor Analysis Lab not found — already removed or never existed under this name." -ForegroundColor Yellow
}


# ── PHASE 3 — FIX: retire the mislabeled 9:30 AM 'Raptor Intraday Monitor' ─────
# Its description promises a 30-min exit+hold loop, but Start_Intraday_Monitor.bat
# actually just runs raptor_monitor.py once (the EOD monitor, pointlessly re-run
# against yesterday's stale data at market open). The real intraday exit loop is
# handled separately (see Phase 4). Recommend retiring this task.

if (Get-ScheduledTask -TaskName "Raptor Intraday Monitor" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "Raptor Intraday Monitor" -Confirm:$false
    Write-Host "Removed: Raptor Intraday Monitor" -ForegroundColor Green
} else {
    Write-Host "Raptor Intraday Monitor not found." -ForegroundColor Yellow
}


# ── PHASE 4 — FIX: replace the undocumented exit-loop task with the version- ───
# controlled one (Register_Exit_Monitor.ps1, already written to C:\Raptor)
#
# IMPORTANT: from Phase 1's output, find whatever task is currently firing
# `python.exe exit_monitor.py` around 9:35 AM and note its exact TaskName below
# before running this block — replace <OLD_TASK_NAME_HERE> with that name.
# If Phase 1 showed no such task at all (i.e. you can't find what's actually
# running the exit loop), STOP and tell me what Phase 1 printed instead —
# don't register a second copy blind.

# Uncomment and fill in once you've identified the old task:
# Unregister-ScheduledTask -TaskName "<OLD_TASK_NAME_HERE>" -Confirm:$false
# Write-Host "Removed old exit-loop task: <OLD_TASK_NAME_HERE>" -ForegroundColor Green

cd "C:\Raptor"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\Register_Exit_Monitor.ps1


# ── PHASE 5 — CHECK: was the 6/29 mid-day death a sleep/power issue? ──────────
# The exit loop died silently between 10:35 AM and 3:50 PM on 6/29 with no
# Python traceback — consistent with the machine sleeping, or the old
# undocumented task having a short execution time limit. Register_Exit_Monitor.ps1
# sets an explicit 8-hour limit, which removes the second possibility going
# forward. Check power settings for the first:

Write-Host "`n=== POWER SETTINGS — check these allow the machine to stay awake during market hours ===" -ForegroundColor Cyan
powercfg /query SCHEME_CURRENT SUB_SLEEP
Write-Host "`nIf 'Sleep after' is anything other than Never (0) during market hours, either change your"
Write-Host "active power plan or set: powercfg /change standby-timeout-ac 0"


# ── PHASE 6 — VERIFY everything looks right ────────────────────────────────────

Write-Host "`n=== FINAL STATE — all Raptor tasks ===" -ForegroundColor Cyan
Get-ScheduledTask | Where-Object { $_.TaskName -like "*Raptor*" } |
    Select-Object TaskName, State |
    Format-Table -AutoSize

Write-Host "`nExpected list going forward:"
Write-Host "  Raptor Watchdog          (9:30 AM, loops watchdog.py every 15 min)"
Write-Host "  Raptor Exit Monitor      (9:35 AM, self-managing exit/trim loop -- NEW, replaces the old undocumented task)"
Write-Host "  Raptor MidDay Monitor    (12:30 PM, hold_monitor.py)"
Write-Host "  Raptor Monitor           (4:30 PM, raptor_monitor.py -- 6-layer check + email)"
Write-Host "  Raptor AfterClose        (5:00 PM, outcome_tracker -> factor_lab -> kelly_engine -> dsr -> git push)"
Write-Host "`n(Plus whatever fires premarket_scanner.py / main.py / daily_recap.py, Start_Afternoon_Monitor.bat --"
Write-Host " Phase 1 should have shown these too; none of the code in this session touched them.)"
