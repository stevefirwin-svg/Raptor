@echo off
setlocal enabledelayedexpansion

set SCRIPT_DIR=C:\Users\steve\OneDrive\Desktop\Raptor
set SCRIPT=raptor_recap.py
set LOG_DIR=%SCRIPT_DIR%\logs
set PYTHON=python

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "tokens=1-3 delims=/ " %%a in ("%DATE%") do set D=%%c-%%a-%%b
for /f "tokens=1-3 delims=:." %%a in ("%TIME: =0%") do set T=%%a%%b%%c
set TIMESTAMP=%D%_%T%
set LOGFILE=%LOG_DIR%\raptor_recap_%TIMESTAMP%.log

echo [%DATE% %TIME%] Starting raptor_recap.py... >> "%LOGFILE%"
echo [%DATE% %TIME%] Starting raptor_recap.py...

cd /d "%SCRIPT_DIR%"
%PYTHON% "%SCRIPT_DIR%\%SCRIPT%" >> "%LOGFILE%" 2>&1

echo [%DATE% %TIME%] raptor_recap.py finished. >> "%LOGFILE%"
echo [%DATE% %TIME%] raptor_recap.py finished.
