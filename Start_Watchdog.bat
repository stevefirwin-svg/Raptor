@echo off
chcp 65001 >nul
title Raptor Watchdog v1.0
cd /d "C:\Users\steve\OneDrive\Desktop\Raptor"

echo.
echo ========================================
echo  RAPTOR WATCHDOG v1.0
echo  Monitoring every 15 minutes
echo  Press Ctrl+C to stop
echo ========================================
echo.

:loop
python watchdog.py
echo.
timeout /t 900 /nobreak
goto loop
