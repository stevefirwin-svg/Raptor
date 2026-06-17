# Register_Raptor_Monitor.ps1
# Creates the "Raptor Monitor" scheduled task with all correct settings.
# Run once from an elevated PowerShell prompt (Run as Administrator).
#
# Usage:
#   1. Right-click PowerShell -> Run as Administrator
#   2. cd C:\Users\steve\OneDrive\Desktop\Raptor
#   3. powershell -ExecutionPolicy Bypass -File .\Register_Raptor_Monitor.ps1
#
# This is idempotent — running it again replaces the existing task cleanly.

$TaskName    = "Raptor Monitor"
$RaptorPath  = "C:\Users\steve\OneDrive\Desktop\Raptor"
$BatPath     = Join-Path $RaptorPath "Start_Monitor.bat"

if (-not (Test-Path $BatPath)) {
    Write-Host "ERROR: $BatPath not found. Make sure Start_Monitor.bat is in $RaptorPath" -ForegroundColor Red
    exit 1
}

# Remove existing task if present (idempotent re-run)
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Existing task found - removing before re-creating..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Action: run the bat file from the Raptor directory
$Action = New-ScheduledTaskAction -Execute $BatPath -WorkingDirectory $RaptorPath

# Trigger: daily at 4:30 PM
$Trigger = New-ScheduledTaskTrigger -Daily -At 4:30PM

# Settings
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

# Principal: run whether logged on or not, highest privileges
$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType S4U `
    -RunLevel Highest

# Register
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Raptor end-of-day monitor - runs raptor_monitor.py after daily_recap.py, sends single summary email" `
    -Force

Write-Host ""
Write-Host "Task registered successfully." -ForegroundColor Green
Write-Host "  Name:     $TaskName" -ForegroundColor Cyan
Write-Host "  Trigger:  Daily at 4:30 PM" -ForegroundColor Cyan
Write-Host "  Action:   $BatPath" -ForegroundColor Cyan
Write-Host "  Working dir: $RaptorPath" -ForegroundColor Cyan
Write-Host ""
Write-Host "NOTE: S4U logon type runs without you logged in but does not need a stored" -ForegroundColor Yellow
Write-Host "      password. If python or env vars do not resolve under S4U, switch to" -ForegroundColor Yellow
Write-Host "      password-based auth instead:" -ForegroundColor Yellow
Write-Host ""
Write-Host '      $cred = Get-Credential' -ForegroundColor White
Write-Host '      Set-ScheduledTask -TaskName "Raptor Monitor" -User $cred.UserName -Password $cred.GetNetworkCredential().Password' -ForegroundColor White
Write-Host ""
Write-Host "To test immediately without waiting for 4:30 PM:" -ForegroundColor Cyan
Write-Host '      Start-ScheduledTask -TaskName "Raptor Monitor"' -ForegroundColor White
Write-Host ""
Write-Host "To verify it ran:" -ForegroundColor Cyan
Write-Host '      Get-ScheduledTaskInfo -TaskName "Raptor Monitor"' -ForegroundColor White
