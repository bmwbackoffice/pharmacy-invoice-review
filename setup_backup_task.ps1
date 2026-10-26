<#
  Creates (or replaces) the Windows scheduled task that backs up the database every night (backup_db.py,
  2026-10-26): a pg_dump to backups\, uploaded privately to Google Drive (Pharmacy Invoices\db_backups),
  14 days kept in both places. Log: logs\backups.log (the dashboard's System health shows the last backup).

  Run it once, in PowerShell or Command Prompt:

      powershell -ExecutionPolicy Bypass -File "C:\Users\joel scher\downloads\claude\setup_backup_task.ps1"

  Options:
      -At "01:30"     the nightly time (default 1:30 AM, between the evening run and the morning ones)
      -Remove         delete the task

  Like the scheduled classify run, the task runs as you, only while you're signed in (so no password is
  stored), with no window; if the computer was off or asleep at that time it runs as soon as it's back.
#>
param(
    [string]$At = "01:30",
    [switch]$Remove
)

$TaskName = "Pharmacy invoices - nightly database backup"
$Project  = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed the scheduled task '$TaskName' (if it existed)."
    return
}

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    Write-Host "Couldn't find pythonw.exe on the PATH. Install Python or add it to the PATH, then run this again."
    exit 1
}
if (-not (Test-Path (Join-Path $Project "backup_db.py"))) {
    Write-Host "backup_db.py isn't next to this script ($Project)."
    exit 1
}

$trigger   = New-ScheduledTaskTrigger -Daily -At $At
$action    = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$Project\backup_db.py`"" -WorkingDirectory $Project
$who       = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $who -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
                 -ExecutionTimeLimit (New-TimeSpan -Hours 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "Runs backup_db.py in $Project (database backup to backups\ and Google Drive). Log: logs\backups.log" `
    -Force | Out-Null

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Scheduled task '$TaskName' is set up, as $who, while you're signed in:"
Write-Host "  Every night at $At. Next run: $($info.NextRunTime)"
Write-Host "  Log: $Project\logs\backups.log"
Write-Host "To run it right now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove it:         powershell -ExecutionPolicy Bypass -File `"$Project\setup_backup_task.ps1`" -Remove"
