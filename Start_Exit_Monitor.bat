@echo off
REM Start_Exit_Monitor.bat — NOT USED. Kept only as a record of a mistake.
REM
REM Written 2026-07-01 alongside Register_Exit_Monitor.ps1 (see that file for
REM the full explanation) on the wrong assumption that the exit/trim loop had
REM no Task Scheduler entry. It does, under the task "Raptor Intraday Monitor",
REM which runs exit_monitor.py directly (no bat file at all) and works fine.
REM This file is not referenced by any scheduled task. Safe to ignore.
echo This file is retired and not used by any scheduled task. Do not register it.
