@echo off
chcp 65001 >nul
title Raptor Morning Monitor
cd /d "C:\Users\steve\OneDrive\Desktop\Raptor"
echo [%date% %time%] Morning monitor starting >> logs\auto_start.log
echo Running hold monitor...
python hold_monitor.py
echo Running exit monitor...
python exit_monitor.py
echo [%date% %time%] Morning monitor complete >> logs\auto_start.log
