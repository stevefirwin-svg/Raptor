@echo off
chcp 65001 >nul
title Viper Options Engine v1.0
cd /d "C:\Raptor"

echo.
echo ========================================
echo  VIPER OPTIONS ENGINE v1.0
echo  Scanning every 30 minutes during market
echo  Press Ctrl+C to stop
echo ========================================
echo.

:loop
echo.
echo [%date% %time%] Running scan...
echo -----------------------------------
python options_engine.py
echo -----------------------------------
echo [%date% %time%] Scan complete. Next in 30 min.
echo.

timeout /t 1800 /nobreak
goto loop
