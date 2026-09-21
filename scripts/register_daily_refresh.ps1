<#
Registers a Windows scheduled task that runs scripts/daily_refresh.py on
weekday evenings, so the Nifty 50 is scanned and the changed names re-analysed
without anyone having to remember to do it.

    Install:   powershell -ExecutionPolicy Bypass -File scripts\register_daily_refresh.ps1
    Custom:    ... -File scripts\register_daily_refresh.ps1 -Time 20:30
    Run now:   Start-ScheduledTask -TaskName "TradingAgents-India daily refresh"
    Remove:    Unregister-ScheduledTask -TaskName "TradingAgents-India daily refresh" -Confirm:$false
    Output:    %USERPROFILE%\.tradingagents\logs\daily_refresh.log

Why 19:00 IST by default: NSE closes at 15:30 and publishes the day's FII/DII
flows in the evening, and Gemini's free quota resets at 12:30 IST, so an
evening run sees the final close and the day's filings with a full quota.

Settings chosen for a laptop: the task runs on battery, is not killed when the
charger is pulled, and if the machine was asleep at 19:00 it runs as soon as
it wakes. A missed evening loses nothing - triggers are measured from each
stock's last report, so the next run still sees the move or the filing.
#>
param([string]$Time = "19:00")
$ErrorActionPreference = "Stop"

$TaskName = "TradingAgents-India daily refresh"
$repo   = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python -ErrorAction Stop).Source
$log    = Join-Path $env:USERPROFILE ".tradingagents\logs\daily_refresh.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

# UTF-8 so the rupee sign and dashes in the summary survive redirection to a
# file (the Windows console default code page would crash print()).
$argLine = "/c set PYTHONIOENCODING=utf-8&& `"$python`" scripts\daily_refresh.py >> `"$log`" 2>&1"

$action   = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $argLine -WorkingDirectory $repo
$trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -RunOnlyIfNetworkAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Hours 3)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Scan the Nifty 50 and re-analyse only what changed (TradingAgents-India)." -Force | Out-Null

Write-Host "Registered '$TaskName' - weekdays at $Time"
Write-Host "  python : $python"
Write-Host "  repo   : $repo"
Write-Host "  log    : $log"
