<#
  Creates (or replaces) the Windows scheduled task that runs scheduled_run.py -- fetch new email and
  classify it. Default schedule (2026-10-13), matching when email actually arrives:

      Weekdays   7:30 AM and 9:00 AM, then every 30 minutes from 10:00 AM to 6:00 PM, then 8:00 PM
      Weekends   every 3 hours from 9:00 AM to 9:00 PM

  Run it once, in PowerShell or Command Prompt:

      powershell -ExecutionPolicy Bypass -File "C:\Users\joel scher\downloads\claude\setup_scheduled_run.ps1"

  Options (all optional):
      -MorningRuns "07:30","09:00"   weekday runs before working hours
      -WorkStart "10:00" -WorkEnd "18:00" -EveryMinutes 30   weekday working-hours window
      -EveningRuns "20:00"           weekday runs after working hours ("" for none)
      -WeekendFrom "09:00" -WeekendTo "21:00" -WeekendEveryHours 3
      -Remove                        delete the task

  The task runs as you, only while you're signed in to this computer (so no password is stored), with
  no window. It never starts a second copy while one is still going, and a manual "Fetch New
  Invoices.bat" run at the same time is skipped safely by classify's run lock. Output goes to
  logs\scheduled_runs.log. The schedule is also written to logs\schedule.json, which the dashboard's
  "Last scheduled run" tile reads to know when a run is overdue.
#>
param(
    [string[]]$MorningRuns = @("07:30", "09:00"),
    [string]$WorkStart = "10:00",
    [string]$WorkEnd = "18:00",
    [int]$EveryMinutes = 30,
    [string[]]$EveningRuns = @("20:00"),
    [string]$WeekendFrom = "09:00",
    [string]$WeekendTo = "21:00",
    [int]$WeekendEveryHours = 3,
    [switch]$Remove
)

$TaskName = "Pharmacy invoices - fetch and classify"
$Project  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Weekdays = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
$Weekend  = @("Saturday", "Sunday")

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

function Minutes([string]$hhmm) { $t = [datetime]::ParseExact($hhmm, "HH:mm", $null); return $t.Hour * 60 + $t.Minute }

# A weekly trigger on $days at $at, optionally repeating every $interval for $duration.
function WeeklyTrigger($days, [string]$at, $interval = $null, $duration = $null) {
    $t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $at
    if ($interval) {
        $r = New-ScheduledTaskTrigger -Once -At $at -RepetitionInterval $interval -RepetitionDuration $duration
        $t.Repetition = $r.Repetition
    }
    return $t
}

$triggers = @()
foreach ($m in $MorningRuns) { if ($m) { $triggers += WeeklyTrigger $Weekdays $m } }
# Working hours: the duration runs one minute past the last slot so the WorkEnd run itself happens.
$workMinutes = (Minutes $WorkEnd) - (Minutes $WorkStart)
$triggers += WeeklyTrigger $Weekdays $WorkStart (New-TimeSpan -Minutes $EveryMinutes) (New-TimeSpan -Minutes ($workMinutes + 1))
foreach ($e in $EveningRuns) { if ($e) { $triggers += WeeklyTrigger $Weekdays $e } }
$weekendMinutes = (Minutes $WeekendTo) - (Minutes $WeekendFrom)
$triggers += WeeklyTrigger $Weekend $WeekendFrom (New-TimeSpan -Hours $WeekendEveryHours) (New-TimeSpan -Minutes ($weekendMinutes + 1))

$action    = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$Project\scheduled_run.py`"" -WorkingDirectory $Project
$who       = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $who -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
                 -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Principal $principal `
    -Settings $settings -Description "Runs scheduled_run.py in $Project (fetch Gmail + classify). Log: logs\scheduled_runs.log" `
    -Force | Out-Null

# Tell the dashboard what to expect, so a quiet morning or weekend isn't reported as overdue.
New-Item -ItemType Directory -Force (Join-Path $Project "logs") | Out-Null
@{
    work_start = $WorkStart; work_end = $WorkEnd; every_minutes = $EveryMinutes
    weekend_from = $WeekendFrom; weekend_to = $WeekendTo; weekend_every_hours = $WeekendEveryHours
    morning_runs = @($MorningRuns); evening_runs = @($EveningRuns)
} | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $Project "logs\schedule.json")

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Scheduled task '$TaskName' is set up, as $who, while you're signed in:"
Write-Host "  Weekdays: $($MorningRuns -join ', ') then every $EveryMinutes min $WorkStart-$WorkEnd$(if ($EveningRuns -and $EveningRuns[0]) { ', then ' + ($EveningRuns -join ', ') })"
Write-Host "  Weekends: every $WeekendEveryHours hours $WeekendFrom-$WeekendTo"
Write-Host "  Next run: $($info.NextRunTime)"
Write-Host "  Log: $Project\logs\scheduled_runs.log"
Write-Host "To run it right now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove it:         powershell -ExecutionPolicy Bypass -File `"$Project\setup_scheduled_run.ps1`" -Remove"
