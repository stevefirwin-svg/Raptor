# Register_MarketAgent.ps1 — DO NOT RUN. Kept only as a record of a mistake,
# same pattern as the retired Register_Exit_Monitor.ps1.
#
# Written earlier in this 2026-07-22 session on the assumption that
# market_agent.py had never been wired into the schedule at all. Turned out
# to be wrong in the same way the exit_monitor.py assumption was wrong on
# 2026-07-01: premarket_scanner.py (already scheduled via Start_PreMarket.bat,
# 9:00 AM and 9:28 AM) already calls market_agent.py's evaluate_session() as
# its Step 2/2 — see premarket_scanner.py. The real bug was that
# premarket_scanner.py crashed at import time on EVERY run, before Step 1 or
# Step 2 ever executed (a PermissionError from opening the same log file the
# .bat wrapper's own `>>` redirect had already opened — confirmed in every
# logs/premarket_*.log back to 2026-06-29). That's fixed directly in
# premarket_scanner.py now (removed its internal logging.FileHandler).
#
# Registering this script would create a second, redundant path writing
# market_decision.json. No action needed here.

Write-Host "This script is retired — do not run it." -ForegroundColor Red
Write-Host "market_agent.py already runs via Start_PreMarket.bat -> premarket_scanner.py" -ForegroundColor Yellow
Write-Host "(9:00 AM and 9:28 AM). That was crashing silently; see premarket_scanner.py's" -ForegroundColor Yellow
Write-Host "2026-07-22 fix (removed the conflicting internal FileHandler)." -ForegroundColor Yellow
Write-Host "Registering this would create a duplicate write path for market_decision.json." -ForegroundColor Yellow
exit 1
