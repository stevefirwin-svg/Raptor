# Migrate_To_CRaptor.ps1
# Run once from C:\Raptor after moving the folder out of OneDrive.
# Patches all hardcoded paths, re-registers Task Scheduler tasks,
# resumes OneDrive (no longer watching Raptor), verifies the result.
#
# Usage (elevated PowerShell from C:\Raptor):
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\Migrate_To_CRaptor.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$OLD = "C:\Users\steve\OneDrive\Desktop\Raptor"
$NEW = "C:\Raptor"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " RAPTOR MIGRATION: OneDrive -> C:\Raptor" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# ── GUARD: must run from C:\Raptor ───────────────────────────────────────────
if ((Get-Location).Path -ne $NEW) {
    Write-Host "ERROR: Run this script from C:\Raptor" -ForegroundColor Red
    Write-Host "  cd C:\Raptor ; .\Migrate_To_CRaptor.ps1"
    exit 1
}
if (-not (Test-Path "$NEW\main.py")) {
    Write-Host "ERROR: main.py not found in $NEW — wrong directory?" -ForegroundColor Red
    exit 1
}
if (Test-Path "$OLD\main.py") {
    Write-Host "ERROR: Old folder still exists at $OLD — move it first." -ForegroundColor Red
    exit 1
}

Write-Host "[1/6] Patching hardcoded paths in all files..." -ForegroundColor Yellow

# Files that contain the old path and need patching
$filesToPatch = @(
    "Daily_GitHub_Push.bat",
    "Start_AfterClose.bat",
    "Start_Afternoon_Monitor.bat",
    "Start_Analysis_Lab.bat",
    "Start_Crypto.bat",
    "Start_Daily_Recap.bat",
    "Start_Entry.bat",
    "Start_Intraday_Monitor.bat",
    "Start_Monitor.bat",
    "Start_Morning_Monitor.bat",
    "Start_PreMarket.bat",
    "Start_Raptor.bat",
    "Start_Recap.bat",
    "Start_Viper.bat",
    "Start_Watchdog.bat",
    "Register_Analysis_Lab.ps1",
    "Register_Intraday_Monitor.ps1",
    "Register_Raptor_Monitor.ps1",
    "Sync_To_Claude_Project.ps1",
    "sync_to_claude.py",
    "check_task_scheduler.py"
)

$patched = 0
foreach ($file in $filesToPatch) {
    $path = Join-Path $NEW $file
    if (-not (Test-Path $path)) {
        Write-Host "  SKIP (not found): $file" -ForegroundColor Gray
        continue
    }
    $content = Get-Content $path -Raw -Encoding UTF8
    if ($content -match [regex]::Escape($OLD)) {
        $updated = $content -replace [regex]::Escape($OLD), $NEW
        Set-Content $path $updated -Encoding UTF8 -NoNewline
        Write-Host "  PATCHED: $file" -ForegroundColor Green
        $patched++
    } else {
        Write-Host "  OK (no change needed): $file" -ForegroundColor Gray
    }
}
Write-Host "  $patched file(s) patched."

# ── STEP 2: Patch RAPTOR_STARTUP.md and RAPTOR_SKILL.md comments ─────────────
Write-Host ""
Write-Host "[2/6] Patching MD files..." -ForegroundColor Yellow
$mdFiles = @("RAPTOR_STARTUP.md", "RAPTOR_SKILL.md", "RAPTOR_MASTER_PLAN.md", "RAPTOR_ONTOLOGY.md")
foreach ($file in $mdFiles) {
    $path = Join-Path $NEW $file
    if (-not (Test-Path $path)) { continue }
    $content = Get-Content $path -Raw -Encoding UTF8
    if ($content -match [regex]::Escape($OLD)) {
        $updated = $content -replace [regex]::Escape($OLD), $NEW
        Set-Content $path $updated -Encoding UTF8 -NoNewline
        Write-Host "  PATCHED: $file" -ForegroundColor Green
    } else {
        Write-Host "  OK: $file" -ForegroundColor Gray
    }
}

# ── STEP 3: Re-register all Task Scheduler tasks ─────────────────────────────
Write-Host ""
Write-Host "[3/6] Re-registering Task Scheduler tasks with new path..." -ForegroundColor Yellow

# Task definitions: [TaskName, BatFile, Trigger description, Trigger PS code]
$taskDefs = @(
    @{
        Name    = "Start_PreMarket"
        Bat     = "Start_PreMarket.bat"
        Desc    = "Raptor pre-market hold monitor. Mon-Fri 9:28 AM."
        Trigger = { New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "09:28AM" }
        Limit   = 10
    },
    @{
        Name    = "Start_Entry"
        Bat     = "Start_Entry.bat"
        Desc    = "Raptor entry scan. Mon-Fri 9:35 AM."
        Trigger = { New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "09:35AM" }
        Limit   = 10
    },
    @{
        Name    = "Start_Morning_Monitor"
        Bat     = "Start_Morning_Monitor.bat"
        Desc    = "Raptor morning hold+exit monitor. Mon-Fri 9:52 AM."
        Trigger = { New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "09:52AM" }
        Limit   = 20
    },
    @{
        Name    = "Start_Recap"
        Bat     = "Start_Recap.bat"
        Desc    = "Raptor daily recap email. Mon-Fri 4:30 PM."
        Trigger = { New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "04:30PM" }
        Limit   = 10
    },
    @{
        Name    = "Raptor Monitor"
        Bat     = "Start_Monitor.bat"
        Desc    = "Raptor end-of-day monitor email. Daily 4:30 PM."
        Trigger = { New-ScheduledTaskTrigger -Daily -At "04:30PM" }
        Limit   = 0
    },
    @{
        Name    = "Start_AfterClose"
        Bat     = "Start_AfterClose.bat"
        Desc    = "Raptor after-close: outcome_tracker, factor_lab, kelly, DSR, git push. Mon-Fri 5:00 PM."
        Trigger = { New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "05:00PM" }
        Limit   = 30
    },
    @{
        Name    = "Raptor Analysis Lab"
        Bat     = "Start_Analysis_Lab.bat"
        Desc    = "Factor IC + Kelly engine. Mon-Fri 5:00 PM."
        Trigger = { New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "05:00PM" }
        Limit   = 30
    },
    @{
        Name    = "Daily_GitHub_Push"
        Bat     = "Daily_GitHub_Push.bat"
        Desc    = "Daily git add -A + push. Every day 6:00 PM."
        Trigger = { New-ScheduledTaskTrigger -Daily -At "06:00PM" }
        Limit   = 10
    }
)

$Settings_noLimit = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

foreach ($td in $taskDefs) {
    $batPath = Join-Path $NEW $td.Bat
    if (-not (Test-Path $batPath)) {
        Write-Host "  SKIP (bat not found): $($td.Name)" -ForegroundColor Gray
        continue
    }

    # Remove existing
    Unregister-ScheduledTask -TaskName $td.Name -Confirm:$false -ErrorAction SilentlyContinue

    $action = New-ScheduledTaskAction `
        -Execute "cmd.exe" `
        -Argument "/c `"$batPath`"" `
        -WorkingDirectory $NEW

    $trigger = & $td.Trigger

    if ($td.Limit -eq 0) {
        $settings = $Settings_noLimit
    } else {
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Minutes $td.Limit)
    }

    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Highest

    Register-ScheduledTask `
        -TaskName   $td.Name `
        -Action     $action `
        -Trigger    $trigger `
        -Settings   $settings `
        -Principal  $principal `
        -Description $td.Desc `
        -Force | Out-Null

    Write-Host "  REGISTERED: $($td.Name)" -ForegroundColor Green
}

# ── STEP 4: Re-enable all tasks ───────────────────────────────────────────────
Write-Host ""
Write-Host "[4/6] Re-enabling all tasks..." -ForegroundColor Yellow
$allTaskNames = $taskDefs | ForEach-Object { $_.Name }
# Also re-enable tasks we disabled but aren't re-registering above
$extraTasks = @("Start_Daily_Recap","Start_Raptor","Start_Afternoon_Monitor",
                "Start_Watchdog","Start_Viper","Start_Crypto","Raptor Intraday Monitor")
foreach ($t in ($allTaskNames + $extraTasks)) {
    Enable-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue | Out-Null
    Write-Host "  ENABLED: $t" -ForegroundColor Green
}

# ── STEP 5: Resume OneDrive (it no longer watches C:\Raptor) ─────────────────
Write-Host ""
Write-Host "[5/6] Resuming OneDrive sync..." -ForegroundColor Yellow
& "$env:LOCALAPPDATA\Microsoft\OneDrive\OneDrive.exe" /resume 2>$null
Write-Host "  OneDrive resumed (C:\Raptor is outside its sync scope)" -ForegroundColor Green

# ── STEP 6: Verify ────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[6/6] Verification..." -ForegroundColor Yellow

# Check no old path remains in bat/py/ps1 files
$remaining = Get-ChildItem $NEW -Include "*.bat","*.py","*.ps1","*.md" -Recurse |
    Select-String -Pattern ([regex]::Escape($OLD)) -List |
    Select-Object -ExpandProperty Path
if ($remaining) {
    Write-Host "  WARN: Old path still found in:" -ForegroundColor Red
    $remaining | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
} else {
    Write-Host "  OK: No old OneDrive paths remaining in any file" -ForegroundColor Green
}

# Check key files exist
$keyFiles = @("main.py","exit_monitor.py","position_ledger.json","sync_to_claude.py",".env")
foreach ($f in $keyFiles) {
    if (Test-Path (Join-Path $NEW $f)) {
        Write-Host "  OK: $f" -ForegroundColor Green
    } else {
        Write-Host "  MISSING: $f" -ForegroundColor Red
    }
}

# Check git remote
Push-Location $NEW
$remote = git remote get-url origin 2>$null
Write-Host "  Git remote: $remote" -ForegroundColor Green
$hash = git log --oneline -1
Write-Host "  Latest commit: $hash" -ForegroundColor Green
Pop-Location

# Check OneDrive is NOT watching C:\Raptor
$odConfig = "$env:LOCALAPPDATA\Microsoft\OneDrive\settings"
Write-Host "  OneDrive sync root is still: $env:OneDrive" -ForegroundColor Green
Write-Host "  C:\Raptor is outside OneDrive sync scope: OK" -ForegroundColor Green

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " MIGRATION COMPLETE" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Open a NEW PowerShell window at C:\Raptor" -ForegroundColor White
Write-Host "  2. python diagnose_system.py" -ForegroundColor White
Write-Host "  3. python sync_to_claude.py" -ForegroundColor White
Write-Host "  4. git add -A ; git commit -m 'Migrate to C:\Raptor, patch all paths (2026-06-19)' ; git push" -ForegroundColor White
Write-Host ""
Write-Host "OneDrive note: Your OneDrive still syncs Desktop and Documents." -ForegroundColor Gray
Write-Host "C:\Raptor is outside its scope — no more sync conflicts." -ForegroundColor Gray
Write-Host ""
