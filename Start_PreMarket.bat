@echo off
chcp 65001 >nul
cd /d "C:\Raptor"
echo [%date% %time%] Start_PreMarket starting >> "C:\Raptor\logs\raptor_auto_start.log"
"C:\Users\steve\AppData\Local\Programs\Python\Python313\python.exe" premarket_scanner.py >> "C:\Raptor\logs\premarket_%date:~10,4%%date:~4,2%%date:~7,2%.log" 2>&1
echo [%date% %time%] Start_PreMarket complete >> "C:\Raptor\logs\raptor_auto_start.log"
