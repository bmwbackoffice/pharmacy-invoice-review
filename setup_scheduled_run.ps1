<#
  Creates (or replaces) the Windows scheduled task that runs scheduled_run.py -- fetch new email and
  classify it -- every 30 minutes from 7:00 AM to 9:00 PM, every day. Run it once, in PowerShell:

      powershell -ExecutionPolicy Bypass -File setup_scheduled_run.ps1

  Options:
      -EveryMinutes 60       how often (default 30)
      -From "08:00"          first run of the day (default 07:00)
      -Hours 10              how long the window lasts (default 14, so 7:00 AM to 9:00 PM)
      -Remove                delete the task

  The task runs as you, only while you're signed in to this computer (so no password is stored), with
  no window. It never starts a second copy while one is still going, and a manual "Fetch New
  Invoices.bat" run at the same time is skipped safely by classify's run lock. Output goes to
  logs\scheduled_runs.log. See it, pause it or run it now in Task Scheduler under the name below.
#>
param(
    [int]$EveryMinutes = 30,
    [string]$From = "07:00",
    [int]$Hours = 14,
    [switch]$Remove
)

$TaskName = "Pharmacy invoices - fetch and classify"
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
if (-not (Test-Path (Join-Path $Project "scheduled_run.py"))) {
    Write-Host "scheduled_run.py isn't next to this script ($Project)."
    exit 1
}

$action  = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$Project\scheduled_run.py`"" -WorkingDirectory $Project
$trigger = New-ScheduledTaskTrigger -Daily -At $From
$repeat  = New-ScheduledTaskTrigger -Once -At $From -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
                                    -RepetitionDuration (New-TimeSpan -Hours $Hours)
$trigger.Repetition = $repeat.Repetition
$who       = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $who -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
                 -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "Runs scheduled_run.py in $Project (fetch Gmail + classify). Log: logs\scheduled_runs.log" `
    -Force | Out-Null

$info = Get-ScheduledTaskInfo -TaskName $TaskName
$until = ([datetime]::ParseExact($From, "HH:mm", $null)).AddHours($Hours).ToString("h:mm tt")
Write-Host "Scheduled task '$TaskName' is set up."
Write-Host "  Runs every $EveryMinutes minutes from $From to $until, as $who, while you're signed in."
Write-Host "  Next run: $($info.NextRunTime)"
Write-Host "  Log: $Project\logs\scheduled_runs.log"
Write-Host "To run it right now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove it:         powershell -ExecutionPolicy Bypass -File setup_scheduled_run.ps1 -Remove"
