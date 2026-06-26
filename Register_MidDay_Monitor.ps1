# Register_MidDay_Monitor.ps1
# Registers a midday hold_monitor.py run at 12:30 PM Mon-Fri.
#
# Problem solved:
#   hold_health.json is written at 9:28 AM and not updated until 3:50 PM.
#   exit_monitor reads it every 30 minutes for math trim decisions, and
#   watchdog reads vol_pctile from it for stop multiplier calibration.
#   By noon, health scores are 3+ hours stale. A midday refresh halves
#   the maximum staleness from ~6.5h to ~3h.
#
# HOW TO RUN (one time, as Administrator):
#   cd "C:\Raptor"
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\Register_MidDay_Monitor.ps1

$TaskName   = "Raptor MidDay Monitor"
$ProjectDir = "C:\Raptor"
$LogFile    = "$ProjectDir\logs\midday_monitor_task.log"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

$Action = New-ScheduledTaskAction `
    -Execute "C:\Users\steve\AppData\Local\Programs\Python\Python313\python.exe" `
    -Argument "hold_monitor.py" `
    -WorkingDirectory $ProjectDir

$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At "12:30PM"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName   $TaskName `
    -Action     $Action `
    -Trigger    $Trigger `
    -Settings   $Settings `
    -Principal  $Principal `
    -Description "Midday hold_health.json refresh. Reduces max health score staleness from 6.5h to 3h for exit_monitor math trims."

Write-Host ""
Write-Host "============================================================"
Write-Host " Task registered: $TaskName"
Write-Host " Trigger:         Mon-Fri at 12:30 PM"
Write-Host " Log:             hold_health.json + logs/hold_monitor (stdout)"
Write-Host "============================================================"
Write-Host "Logs go to: $LogFile"
Write-Host "To verify: Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "To test now: Start-ScheduledTask -TaskName '$TaskName'"
