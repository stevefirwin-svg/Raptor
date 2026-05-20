@echo off
chcp 65001 >nul
title Raptor Afternoon Monitor
cd /d "C:\Users\steve\OneDrive\Desktop\Raptor"
echo [%date% %time%] Afternoon monitor starting >> logs\auto_start.log
echo Running exit monitor...
python exit_monitor.py
echo Running hold monitor...
python hold_monitor.py
echo Sending daily recap email...
python daily_recap.py
echo [%date% %time%] Afternoon monitor complete >> logs\auto_start.log
