@echo off
chcp 65001 >nul
title Raptor Afternoon Monitor
cd /d "C:\Raptor"
echo [%date% %time%] Afternoon monitor starting >> logs\raptor_auto_start.log
echo Running hold monitor...
python hold_monitor.py
echo Running exit monitor...
python exit_monitor.py
echo [%date% %time%] Afternoon monitor complete >> logs\raptor_auto_start.log
:: NOTE: daily_recap.py is NOT called here — it runs separately via Start_Recap.bat at 4:30 PM
