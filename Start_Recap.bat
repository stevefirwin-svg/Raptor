@echo off
chcp 65001 >nul
cd /d "C:\Raptor"
echo [%date% %time%] Start_Recap starting >> "C:\Raptor\logs\raptor_auto_start.log"
"C:\Users\steve\AppData\Local\Programs\Python\Python313\python.exe" daily_recap.py >> "C:\Raptor\logs\raptor_run.log" 2>&1
echo [%date% %time%] Start_Recap complete >> "C:\Raptor\logs\raptor_auto_start.log"
