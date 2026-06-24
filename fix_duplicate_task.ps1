# fix_duplicate_task.ps1
# Run as Administrator in PowerShell
# Finds and removes any stale Task Scheduler entries pointing to the old OneDrive path
# that caused the June 19 double-order incident.

Write-Host "`n=== TASK SCHEDULER AUDIT ===" -ForegroundColor Cyan

$tasks = Get-ScheduledTask | Where-Object {
    $_.TaskName -match 'raptor|start_entry|start_recap|start_monitor|start_pre|start_after|watchdog|analysis' -or
    ($_.Actions | Where-Object { $_.Execute -match 'raptor' -or $_.WorkingDirectory -match 'raptor' })
}

Write-Host "`nAll Raptor-related tasks found:`n"
foreach ($t in $tasks) {
    $action = $t.Actions[0]
    $path = $action.WorkingDirectory
    $isOld = $path -match 'OneDrive' -or $path -match 'Desktop\\Raptor'
    $isNew = $path -match 'C:\\Raptor' -and $path -notmatch 'OneDrive'
    $color = if ($isOld) { "Red" } elseif ($isNew) { "Green" } else { "Yellow" }
    Write-Host "  [$($t.State)] $($t.TaskName)" -ForegroundColor $color
    Write-Host "    Working Dir: $path"
    Write-Host "    Execute:     $($action.Execute)"
    if ($isOld) {
        Write-Host "    ^^^ OLD PATH - THIS IS THE DUPLICATE ^^^" -ForegroundColor Red
    }
}

Write-Host "`n=== IDENTIFYING DUPLICATES ===" -ForegroundColor Cyan

$oldTasks = $tasks | Where-Object {
    $_.Actions[0].WorkingDirectory -match 'OneDrive' -or
    $_.Actions[0].WorkingDirectory -match 'Desktop\\Raptor'
}

if ($oldTasks.Count -eq 0) {
    Write-Host "No old-path tasks found. All tasks point to C:\Raptor. You're clean." -ForegroundColor Green
} else {
    Write-Host "`nFound $($oldTasks.Count) task(s) pointing to old OneDrive path:" -ForegroundColor Red
    foreach ($t in $oldTasks) {
        Write-Host "  - $($t.TaskName)  [$($t.Actions[0].WorkingDirectory)]" -ForegroundColor Red
    }
    
    Write-Host "`nRemoving old-path tasks..." -ForegroundColor Yellow
    foreach ($t in $oldTasks) {
        try {
            Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false
            Write-Host "  REMOVED: $($t.TaskName)" -ForegroundColor Green
        } catch {
            Write-Host "  FAILED to remove $($t.TaskName): $_" -ForegroundColor Red
        }
    }
}

Write-Host "`n=== FINAL STATE ===" -ForegroundColor Cyan
$remaining = Get-ScheduledTask | Where-Object {
    $_.TaskName -match 'raptor|start_entry|start_recap|start_monitor|start_pre|start_after|watchdog|analysis'
}
foreach ($t in $remaining) {
    Write-Host "  [OK] $($t.TaskName) -> $($t.Actions[0].WorkingDirectory)" -ForegroundColor Green
}
Write-Host "`nDone. Verify all remaining tasks show C:\Raptor as working directory.`n"
