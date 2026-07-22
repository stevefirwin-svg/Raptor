@echo off
REM RETIRED 2026-07-22 — do not schedule this. market_agent.py already runs via
REM Start_PreMarket.bat -> premarket_scanner.py (Step 2/2, 9:00 AM and 9:28 AM).
REM That path was crashing silently on every run; fixed directly in
REM premarket_scanner.py. This file is a leftover from a wrong assumption made
REM earlier in the same session — see Register_MarketAgent.ps1's own retraction
REM note. Safe to delete: C:\Raptor\Start_MarketAgent.bat
echo Start_MarketAgent.bat is retired — market_agent.py already runs via Start_PreMarket.bat. Do not schedule this file.
exit /b 1
