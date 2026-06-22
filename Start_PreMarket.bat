@echo off
chcp 65001 >nul
title Raptor PreMarket
cd /d "C:\Raptor"
echo [%date% %time%] PreMarket scan starting >> logs\raptor_auto_start.log
echo Running pre-entry hold monitor...
python hold_monitor.py --pre
echo [%date% %time%] PreMarket complete >> logs\raptor_auto_start.log
