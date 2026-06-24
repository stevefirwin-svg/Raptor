@echo off
chcp 65001 >nul
cd /d "C:\Raptor"
echo [%date% %time%] Start_Analysis_Lab starting >> "C:\Raptor\logs\raptor_auto_start.log"
"C:\Users\steve\AppData\Local\Programs\Python\Python313\python.exe" factor_lab.py >> "C:\Raptor\logs\raptor_run.log" 2>&1
echo [%date% %time%] Start_Analysis_Lab complete >> "C:\Raptor\logs\raptor_auto_start.log"
