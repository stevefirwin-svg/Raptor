@echo off
chcp 65001 >nul
title Raptor After-Close
cd /d "C:\Raptor"
echo [%date% %time%] After-close sequence starting >> logs\raptor_auto_start.log

echo.
echo [%time%] Step 1: Tagging closed trades (outcome_tracker)...
python outcome_tracker.py
echo [%time%] Outcome tracker complete.

echo.
echo [%time%] Step 2: Factor IC validation...
python factor_lab.py
echo [%time%] Factor lab complete.

echo.
echo [%time%] Step 3: Kelly engine update...
python kelly_engine.py
echo [%time%] Kelly engine complete.

echo.
echo [%time%] Step 4: Deflated Sharpe Ratio...
python dsr.py
echo [%time%] DSR complete.

echo.
echo [%time%] Step 4: GitHub push...
git add -A
git commit -m "Daily update %date:~10,4%-%date:~4,2%-%date:~7,2%"
git push
echo [%time%] GitHub push complete.

echo.
echo [%date% %time%] After-close sequence complete >> logs\raptor_auto_start.log
echo Reports: factor_ic_report.json + kelly_estimates.json + outcome_log.json
