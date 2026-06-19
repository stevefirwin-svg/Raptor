@echo off
setlocal

set SCRIPT_DIR=C:\Raptor
set PYTHON=python
set ERRORFILE=%SCRIPT_DIR%\logs\recap_error.txt

if not exist "%SCRIPT_DIR%\logs" mkdir "%SCRIPT_DIR%\logs"

echo ===== Raptor Recap Debug ===== > "%ERRORFILE%"
echo Run time: %DATE% %TIME% >> "%ERRORFILE%"
echo. >> "%ERRORFILE%"

:: Log Python version
echo --- Python version --- >> "%ERRORFILE%"
%PYTHON% --version >> "%ERRORFILE%" 2>&1

:: Log working directory
echo. >> "%ERRORFILE%"
echo --- Working directory --- >> "%ERRORFILE%"
echo %SCRIPT_DIR% >> "%ERRORFILE%"

:: Check required files exist
echo. >> "%ERRORFILE%"
echo --- File checks --- >> "%ERRORFILE%"
for %%F in (daily_recap.py config.py data_feeds.py signals.py universe_builder.py) do (
    if exist "%SCRIPT_DIR%\%%F" (
        echo FOUND: %%F >> "%ERRORFILE%"
    ) else (
        echo MISSING: %%F >> "%ERRORFILE%"
    )
)

:: Run the script and capture all output
echo. >> "%ERRORFILE%"
echo --- Script output --- >> "%ERRORFILE%"
cd /d "%SCRIPT_DIR%"
%PYTHON% daily_recap.py >> "%ERRORFILE%" 2>&1

echo. >> "%ERRORFILE%"
echo --- Exit code: %ERRORLEVEL% --- >> "%ERRORFILE%"

echo Done. Check logs\recap_error.txt for details.
pause
