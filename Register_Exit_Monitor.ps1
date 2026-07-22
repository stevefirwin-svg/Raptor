# Register_Exit_Monitor.ps1 — DO NOT RUN. Kept only as a record of a mistake.
#
# Written 2026-07-01 on the wrong assumption that the exit/trim loop had no
# Task Scheduler entry anywhere. It does: the task named "Raptor Intraday
# Monitor" already runs `exit_monitor.py` directly at 9:35 AM and works
# correctly (confirmed by reading the live task on 2026-07-01 — its Action is
# literally `python.exe exit_monitor.py`, an 8-hour execution time limit, and
# a healthy 30-min cycle log most days). Running this script would register a
# SECOND task doing the same job. exit_monitor.py's process lock
# (logs/exit_monitor.lock) would make the second copy abort rather than
# double-run trades, but there's no reason to have two tasks for one job.
#
# No action needed here. If you ever want to properly rename/document the
# real "Raptor Intraday Monitor" task, that's a separate, deliberate step —
# not this script.

Write-Host "This script is retired — do not run it." -ForegroundColor Red
Write-Host "Your exit/trim loop already runs correctly under the task named" -ForegroundColor Yellow
Write-Host "'Raptor Intraday Monitor' (python.exe exit_monitor.py, 9:35 AM)." -ForegroundColor Yellow
Write-Host "Registering this would create a duplicate task." -ForegroundColor Yellow
exit 1
