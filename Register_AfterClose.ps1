# Register_AfterClose.ps1
# Registers the Raptor After-Close pipeline in Windows Task Scheduler.
# Runs Start_AfterClose.bat at 5:00 PM Mon-Fri.
#
# Pipeline (in order):
#   1. outcome_tracker.py    — tag closed trades → outcome_log.json
#   2. factor_lab.py         — Spearman IC + ICIR validation → factor_ic_report.json
#   3. kelly_engine.py       — bootstrap Kelly update → kelly_estimates.json
#   4. dsr.py                — Deflated Sharpe Ratio → (printed, read from position_outcomes.json)
#   5. git add -A + push     — daily snapshot to GitHub
#
# HOW TO RUN (one time, as Administrator):
#   cd "C:\Raptor"
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\Register_AfterClose.ps1

$TaskName   = "Raptor AfterClose"
$ProjectDir = "C:\Raptor"
$BatFile    = "$ProjectDir\Start_AfterClose.bat"
$LogFile    = "$ProjectDir\logs\afterclose_task.log"

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
    -At "05:00PM"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
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
    -Description "Raptor after-close pipeline: outcome tagging, factor IC, Kelly, DSR, git push. Mon-Fri 5PM."

Write-Host ""
Write-Host "============================================================"
Write-Host " Task registered: $TaskName"
Write-Host " Trigger:         Mon-Fri at 5:00 PM"
Write-Host " Action:          $BatFile"
Write-Host " Log:             $LogFile"
Write-Host "============================================================"
Write-Host "To verify: Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "To test now: Start-ScheduledTask -TaskName '$TaskName'"
