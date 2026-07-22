@echo off
setlocal enabledelayedexpansion
cd /d C:\Raptor

set LOGFILE=logs\github_push.log
set DATESTAMP=%date:~10,4%-%date:~4,2%-%date:~7,2%

echo [%date% %time%] Starting daily GitHub push... >> %LOGFILE%

REM ── Guard: a leftover .git\index.lock silently breaks every command below.
REM This is exactly what happened 2026-07-01 through 2026-07-22 — the old
REM version of this script had no error checking, so 22 days of "Push
REM complete." got logged while nothing was actually committed. Fail loud
REM instead.
if exist ".git\index.lock" (
    echo [%date% %time%] FATAL: .git\index.lock exists — stale lock or a concurrent git process. Aborting. >> %LOGFILE%
    echo [%date% %time%] RESULT: FAILED — see %LOGFILE% >> %LOGFILE%
    echo GITHUB PUSH FAILED — stale .git\index.lock. Check %LOGFILE%.
    exit /b 1
)

git add -A >> %LOGFILE% 2>&1
if errorlevel 1 (
    echo [%date% %time%] FATAL: git add -A failed ^(errorlevel !errorlevel!^) >> %LOGFILE%
    echo [%date% %time%] RESULT: FAILED — see %LOGFILE% >> %LOGFILE%
    echo GITHUB PUSH FAILED — git add error. Check %LOGFILE%.
    exit /b 1
)

REM "nothing to commit" is not a failure — any OTHER commit error is.
git diff --cached --quiet
if errorlevel 1 (
    git commit -m "Daily update %DATESTAMP%" >> %LOGFILE% 2>&1
    if errorlevel 1 (
        echo [%date% %time%] FATAL: git commit failed ^(errorlevel !errorlevel!^) >> %LOGFILE%
        echo [%date% %time%] RESULT: FAILED — see %LOGFILE% >> %LOGFILE%
        echo GITHUB PUSH FAILED — git commit error. Check %LOGFILE%.
        exit /b 1
    )
) else (
    echo [%date% %time%] Nothing staged to commit — working tree clean. >> %LOGFILE%
)

git push >> %LOGFILE% 2>&1
if errorlevel 1 (
    echo [%date% %time%] FATAL: git push failed ^(errorlevel !errorlevel!^) >> %LOGFILE%
    echo [%date% %time%] RESULT: FAILED — see %LOGFILE% >> %LOGFILE%
    echo GITHUB PUSH FAILED — git push error. Check %LOGFILE%.
    exit /b 1
)

echo [%date% %time%] RESULT: SUCCESS — push verified. >> %LOGFILE%
echo GitHub push complete.
exit /b 0
