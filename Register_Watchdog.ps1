# Register_Watchdog.ps1
# Registers the Raptor Watchdog in Windows Task Scheduler.
# Runs Start_Watchdog.bat at 9:30 AM Mon-Fri.
# Start_Watchdog.bat loops watchdog.py every 15 minutes and self-terminates at 4:00 PM.
#
# HOW TO RUN (one time, as Administrator):
#   cd "C:\Raptor"
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\Register_Watchdog.ps1

$TaskName   = "Raptor Watchdog"
$ProjectDir = "C:\Raptor"
$BatFile    = "$ProjectDir\Start_Watchdog.bat"
$LogFile    = "$ProjectDir\logs\watchdog_task.log"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$BatFile`" >> `"$LogFile`" 2>&1" `
    -WorkingDirectory $ProjectDir

$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At "09:30AM"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
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
    -Description "Raptor intraday watchdog — hard stop + trail every 15 min, 9:30 AM - 4:00 PM ET."

Write-Host ""
Write-Host "============================================================"
Write-Host " Task registered: $TaskName"
Write-Host " Trigger:         Mon-Fri at 9:30 AM"
Write-Host " Action:          $BatFile"
Write-Host " Log:             $LogFile"
Write-Host "============================================================"
Write-Host "To verify: Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "To test now: Start-ScheduledTask -TaskName '$TaskName'"
