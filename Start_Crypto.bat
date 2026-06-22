@echo off
chcp 65001 >nul
title Raptor Crypto v1.0 - BTC/ETH
cd /d "C:\Raptor"

echo.
echo ========================================
echo  RAPTOR CRYPTO v1.0 - BTC / ETH
echo  Scanning every 30 minutes (24/7)
echo  10%% capital allocation
echo  Press Ctrl+C to stop
echo ========================================
echo.

:loop
python crypto_engine.py
echo.
timeout /t 1800 /nobreak
goto loop
