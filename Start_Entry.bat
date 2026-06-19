@echo off
chcp 65001 >nul
title Raptor Entry Scan
cd /d "C:\Raptor"
echo [%date% %time%] Entry scan starting >> logs\raptor_auto_start.log
echo Running entry scanner...
python main.py
echo [%date% %time%] Entry scan complete >> logs\raptor_auto_start.log
python morning_scanner_email.py
