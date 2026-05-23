@echo off
chcp 65001 >nul
title Raptor Analysis Lab
cd /d "C:\Users\steve\OneDrive\Desktop\Raptor"
echo [%date% %time%] Analysis lab starting >> logs\auto_start.log

echo.
echo ============================================================
echo  RAPTOR ANALYSIS LAB — %date% %time%
echo ============================================================
echo.

echo [%time%] Running Factor IC Validation Lab...
python factor_lab.py
echo [%time%] Factor lab complete.

echo.
echo [%time%] Running Kelly Engine (shadow mode)...
python kelly_engine.py
echo [%time%] Kelly engine complete.

echo.
echo [%date% %time%] Analysis lab complete >> logs\auto_start.log
echo Reports saved: factor_ic_report.json + kelly_estimates.json
pause
