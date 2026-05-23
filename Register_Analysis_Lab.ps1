# Register_Analysis_Lab.ps1
# Registers the Raptor Analysis Lab in Windows Task Scheduler.
# Runs factor_lab.py + kelly_engine.py at 5:00 PM Mon-Fri.
#
# HOW TO RUN (one time, as Administrator):
#   cd "C:\Users\steve\OneDrive\Desktop\Raptor"
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\Register_Analysis_Lab.ps1

$TaskName   = "Raptor Analysis Lab"
$ProjectDir = "C:\Users\steve\OneDrive\Desktop\Raptor"
$BatFile    = "$ProjectDir\Start_Analysis_Lab.bat"
$LogFile    = "$ProjectDir\logs\analysis_lab.log"

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
    -StartWhenAvailable

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
    -Description "Factor IC lab + Kelly engine. Runs after close Mon-Fri 5PM."

Write-Host ""
Write-Host "============================================================"
Write-Host " Task registered: $TaskName"
Write-Host " Trigger:         Mon-Fri at 5:00 PM"
Write-Host " Log:             $LogFile"
Write-Host "============================================================"
Write-Host "To verify: Open Task Scheduler and look for '$TaskName'"
