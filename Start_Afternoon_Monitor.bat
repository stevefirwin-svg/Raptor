@echo off
chcp 65001 >nul
cd /d "C:\Raptor"
echo [%date% %time%] Start_Afternoon_Monitor starting >> "C:\Raptor\logs\raptor_auto_start.log"
"C:\Users\steve\AppData\Local\Programs\Python\Python313\python.exe" exit_monitor.py >> "C:\Raptor\logs\raptor_run.log" 2>&1
echo [%date% %time%] Start_Afternoon_Monitor complete >> "C:\Raptor\logs\raptor_auto_start.log"
