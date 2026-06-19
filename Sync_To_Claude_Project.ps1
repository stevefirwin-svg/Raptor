# Sync_To_Claude_Project.ps1
# PURPOSE: Keep your local Raptor repo, GitHub, and the Claude Project in sync.
# RUN THIS: Before every Claude session AND after every git push.
# LOCATION: Save this file in C:\Raptor\
#
# HARD RULE (from RAPTOR_SKILL.md):
#   The Claude Project Files tab is the fallback when GitHub is unreachable.
#   GitHub is the single source of truth. Claude Project must mirror GitHub exactly.
#   Run this script whenever you push. No exceptions.

param(
    [string]$RaptorPath = "C:\Raptor"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "`n=== RAPTOR SYNC SCRIPT ===" -ForegroundColor Cyan
Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Gray

# ------------------------------------------------------------------
# STEP 1 — Pull latest from GitHub
# ------------------------------------------------------------------
Write-Host "`n[1/4] Pulling from GitHub..." -ForegroundColor Yellow
Push-Location $RaptorPath
git pull origin main
$hash = git log --oneline -1
Write-Host "  Latest commit: $hash" -ForegroundColor Green
Pop-Location

# ------------------------------------------------------------------
# STEP 2 — Clear pycache
# ------------------------------------------------------------------
Write-Host "`n[2/4] Clearing __pycache__..." -ForegroundColor Yellow
Get-ChildItem -Path $RaptorPath -Recurse -Filter "__pycache__" -Directory |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "  Done." -ForegroundColor Green

# ------------------------------------------------------------------
# STEP 3 — List files that changed in last commit (for manual Claude upload)
# ------------------------------------------------------------------
Write-Host "`n[3/4] Files changed in last commit (need re-upload to Claude Project if changed):" -ForegroundColor Yellow
Push-Location $RaptorPath
$changed = git diff --name-only HEAD~1 HEAD 2>$null
if ($changed) {
    $changed | ForEach-Object { Write-Host "  CHANGED: $_" -ForegroundColor Magenta }
} else {
    Write-Host "  (no diff — first commit or single commit repo)" -ForegroundColor Gray
}
Pop-Location

# ------------------------------------------------------------------
# STEP 4 — Remind about Claude Project sync
# ------------------------------------------------------------------
Write-Host "`n[4/4] CLAUDE PROJECT SYNC REMINDER" -ForegroundColor Yellow
Write-Host @"

  The Claude Project Files tab does NOT auto-sync from GitHub.
  You must manually upload changed files.

  HOW TO SYNC CLAUDE PROJECT:
  1. Open claude.ai → your Raptor project
  2. Click 'Project Files' or the knowledge/files panel
  3. For each CHANGED file listed above:
     - Delete the old version in the Claude Project
     - Upload the new file from: $RaptorPath\<filename>
  4. The four MD files are most critical:
       RAPTOR_SKILL.md
       RAPTOR_STARTUP.md
       RAPTOR_MASTER_PLAN.md
       RAPTOR_ONTOLOGY.md
  5. Also upload any .py files that changed.

  MINIMUM REQUIRED UPLOADS (always keep current):
     - All *.md files (system rules)
     - data_feeds.py, signals.py, main.py, exit_monitor.py
     - agent_layer.py, config.py, outcome_tracker.py
     - *.json state files (position_ledger, outcome_log, etc.)

"@ -ForegroundColor White

Write-Host "=== SYNC COMPLETE ===" -ForegroundColor Cyan
Write-Host "Paste this commit hash to Claude at session start: $hash`n" -ForegroundColor Green
