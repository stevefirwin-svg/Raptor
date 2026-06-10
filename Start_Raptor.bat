@echo off
chcp 65001 >nul
title Raptor v5.4
cd /d "C:\Users\steve\OneDrive\Desktop\Raptor"

echo.
echo ========================================
echo  RAPTOR v5.4 - %date% %time%
echo ========================================
echo.

echo [%date% %time%] Raptor v5.4 starting >> logs\raptor_auto_start.log

echo [1/2] Entry Scan...
echo --------------------
python main.py
echo.

echo [2/2] Exit Monitor...
echo ----------------------
python exit_monitor.py
echo.

echo [%date% %time%] Raptor v5.4 complete >> logs\raptor_auto_start.log

echo.
echo ========================================
echo  RAPTOR v5.4 COMPLETE
echo ========================================
pause
