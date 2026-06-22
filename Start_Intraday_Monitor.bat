@echo off
chcp 65001 >nul
title Raptor Intraday Monitor
cd /d "C:\Raptor"

echo [%date% %time%] Intraday monitor loop started >> logs\raptor_auto_start.log

:LOOP
    :: Get current hour and minute (24h)
    for /f "tokens=1-2 delims=:" %%A in ("%time%") do (
        set /a HOUR=%%A
        set /a MIN=%%B
    )

    :: Strip leading spaces from HOUR (time can have a leading space before single digits)
    set /a HOUR=%HOUR%
    set /a MIN=%MIN%

    :: Total minutes since midnight
    set /a TOTAL_MIN = HOUR * 60 + MIN

    :: Market open 9:35 = 575 min, market close 15:50 = 950 min
    set /a OPEN_MIN=575
    set /a CLOSE_MIN=950

    if %TOTAL_MIN% LSS %OPEN_MIN% (
        echo [%time%] Pre-market — waiting until 9:35 AM...
        timeout /t 300 /nobreak >nul
        goto LOOP
    )

    if %TOTAL_MIN% GTR %CLOSE_MIN% (
        echo [%date% %time%] Market closed. Intraday monitor shutting down. >> logs\raptor_auto_start.log
        echo Market closed (past 3:50 PM). Monitor complete.
        goto DONE
    )

    :: ── Run monitors ─────────────────────────────────────────────────────────
    echo.
    echo ============================================================
    echo  RAPTOR INTRADAY MONITOR — %date% %time%
    echo ============================================================
    echo.

    echo [%time%] Running hold monitor...
    python hold_monitor.py
    echo [%time%] Hold monitor complete.

    echo [%time%] Running exit monitor...
    python exit_monitor.py
    echo [%time%] Exit monitor complete.

    echo [%date% %time%] Cycle complete >> logs\raptor_auto_start.log

    :: ── Wait 30 minutes (1800 seconds) then loop ─────────────────────────────
    echo.
    echo Sleeping 30 minutes. Next run at approx %time%...
    timeout /t 1800 /nobreak >nul

    goto LOOP

:DONE
pause
