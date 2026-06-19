@echo off
REM Start_Monitor.bat — Raptor End-of-Day Monitor
REM Runs after daily_recap.py (Task Scheduler: 4:30 PM ET)
REM Output: single summary email + logs\monitor_run_YYYYMMDD.log (Python) +
REM         logs\monitor_bat_YYYYMMDD.log (this wrapper's own stdout/stderr capture)
REM NOTE: this log filename is intentionally different from the one Python's
REM logging module writes internally — two writers on one file caused a
REM PermissionError [Errno 13] on Windows the first time this ran.

cd /D "C:\Raptor"

set LOGDATE=%date:~10,4%%date:~4,2%%date:~7,2%
set LOGFILE=logs\monitor_bat_%LOGDATE%.log

echo [%date% %time%] Starting Raptor Monitor >> %LOGFILE% 2>&1
python raptor_monitor.py >> %LOGFILE% 2>&1
echo [%date% %time%] Monitor complete (exit code %ERRORLEVEL%) >> %LOGFILE% 2>&1
