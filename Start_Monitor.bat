@echo off
chcp 65001 >nul
cd /d "C:\Raptor"
echo [%date% %time%] Start_Monitor starting >> "C:\Raptor\logs\raptor_auto_start.log"
REM BUG FIX 2026-07-01: this was calling hold_monitor.py, silently replacing the
REM 6-layer EOD raptor_monitor.py run (L1-L6 findings + summary email) that the
REM "Raptor Monitor" scheduled task (4:30 PM, see Register_Raptor_Monitor.ps1) is
REM supposed to trigger. logs/monitor_20260624.json..monitor_20260630.json and
REM logs/raptor_auto_start.log confirm this bat correctly ran raptor_monitor.py
REM every day through 6/30 (findings + "Monitor email sent" each time); somewhere
REM between then and now the command was swapped to hold_monitor.py, which would
REM have silently stopped the 4:30 PM monitor email with no error of any kind.
REM Restored to match Register_Raptor_Monitor.ps1's description and raptor_monitor.py's
REM own docstring ("Scheduling: 4:30 PM ET -> python raptor_monitor.py").
"C:\Users\steve\AppData\Local\Programs\Python\Python313\python.exe" raptor_monitor.py >> "C:\Raptor\logs\raptor_run.log" 2>&1
echo [%date% %time%] Start_Monitor complete >> "C:\Raptor\logs\raptor_auto_start.log"
