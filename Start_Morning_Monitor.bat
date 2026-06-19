@echo off
chcp 65001 >nul
title Raptor Morning Monitor
cd /d "C:\Raptor"
echo [%date% %time%] Morning monitor starting >> logs\raptor_auto_start.log
echo Running hold monitor...
python hold_monitor.py
echo Running exit monitor...
python exit_monitor.py
echo [%date% %time%] Morning monitor complete >> logs\raptor_auto_start.log
