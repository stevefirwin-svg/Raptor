# ============================================================
# Register_Intraday_Monitor.ps1
# Registers the Raptor Intraday Monitor in Windows Task Scheduler.
# Runs Start_Intraday_Monitor.bat at 9:30 AM Monday-Friday.
# The bat handles its own timing loop and shuts down at 3:50 PM.
#
# HOW TO RUN (one time only):
#   1. Open PowerShell as Administrator
#   2. cd "C:\Users\steve\OneDrive\Desktop\Raptor"
#   3. Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   4. .\Register_Intraday_Monitor.ps1
# ============================================================

$TaskName    = "Raptor Intraday Monitor"
$ProjectDir  = "C:\Users\steve\OneDrive\Desktop\Raptor"
$BatFile     = "$ProjectDir\Start_Intraday_Monitor.bat"
$LogFile     = "$ProjectDir\logs\task_scheduler.log"

# ── Remove existing task if present ──────────────────────────────────────────
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

# ── Action: run the bat file ─────────────────────────────────────────────────
$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$BatFile`" >> `"$LogFile`" 2>&1" `
    -WorkingDirectory $ProjectDir

# ── Trigger: 9:30 AM Monday through Friday ───────────────────────────────────
$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At "09:30AM"

# ── Settings ──────────────────────────────────────────────────────────────────
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

# ── Principal: run as current user, only when logged in ──────────────────────
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Highest

# ── Register ──────────────────────────────────────────────────────────────────
Register-ScheduledTask `
    -TaskName  $TaskName `
    -Action    $Action `
    -Trigger   $Trigger `
    -Settings  $Settings `
    -Principal $Principal `
    -Description "Raptor intraday exit + hold monitor loop. Runs 9:35 AM - 3:50 PM ET every 30 min. Self-terminating."

Write-Host ""
Write-Host "============================================================"
Write-Host " Task registered: $TaskName"
Write-Host " Trigger:         Mon-Fri at 9:30 AM"
Write-Host " Action:          $BatFile"
Write-Host " Log output:      $LogFile"
Write-Host "============================================================"
Write-Host ""
Write-Host "To verify: Open Task Scheduler and look for '$TaskName'"
Write-Host "To remove: Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
