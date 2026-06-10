@echo off
chcp 65001 >nul
title Raptor Daily Recap
cd /d "C:\Users\steve\OneDrive\Desktop\Raptor"
echo [%date% %time%] Recap email starting >> logs\raptor_auto_start.log
echo Sending daily recap email...
python daily_recap.py
echo [%date% %time%] Recap email complete >> logs\raptor_auto_start.log
