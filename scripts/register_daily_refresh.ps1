<#
Registers a Windows scheduled task that runs scripts/daily_refresh.py on
weekday evenings, so the Nifty 50 is scanned and the changed names re-analysed
without anyone having to remember to do it.

    Install:   powershell -ExecutionPolicy Bypass -File scripts\register_daily_refresh.ps1
    Custom:    ... -File scripts\register_daily_refresh.ps1 -Start 19:00 -End 20:00
    Wake PC:   ... -File scripts\register_daily_refresh.ps1 -Wake
    Run now:   Start-ScheduledTask -TaskName "TradingAgents-India daily refresh"
    Status:    Get-ScheduledTaskInfo -TaskName "TradingAgents-India daily refresh"
    Remove:    Unregister-ScheduledTask -TaskName "TradingAgents-India daily refresh" -Confirm:$false
    Output:    %USERPROFILE%\.tradingagents\logs\daily_refresh.log  (+ refresh_<date>.md)

Why 19:00-21:00 IST by default: NSE closes at 15:30 and publishes the day's
FII/DII flows in the evening, and Gemini's free quota resets at 12:30 IST, so
an evening run sees the final close and the day's filings with a full quota.

The window was 19:00-20:00 until 2026-10-02. One hour fitted only about 25
tickers at a measured median of 136s each, and on 1 October 29 triggered - so
the window, not the quota, was the limit. Two hours lets the 500/day quota be
the limit instead (~33 tickers; --max-tickers defaults to 30). The cost is that
the laptop has to stay awake until 21:00.

The window is enforced by the script itself (--until), not only by Task
Scheduler: no analysis is started that would not finish by the end, the batch
is killed outright if it is still going a minute before, and a run that starts
too late (the laptop was asleep until 19:55, say) does nothing.

--catch-up handles the opposite case. If the machine was off at 19:00, Windows
starts the task whenever it next can, often the following morning while the
market is open. Analysing a half-finished session would be wrong, so the run
instead analyses the last session that actually closed - and skips even that
if it has already been analysed, so a catch-up never re-spends the quota. Task
Scheduler's own time limit is a backstop set just past the window.

Settings chosen for a laptop: runs on battery and is not killed when the
charger is pulled; if the machine was asleep at the start time it runs on
waking (still bounded by the window); a second instance is never started on
top of a running one. -Wake also wakes the machine from sleep for the run (it
needs wake timers allowed in the power plan). A missed evening loses nothing:
triggers are measured from each stock's last report, so the next run still
sees the move or the filing.

The task runs as you, only while you are logged on, like any per-user task.
#>
param(
    [string]$Start = "19:00",
    [string]$End   = "21:00",
    [switch]$Wake
)
$ErrorActionPreference = "Stop"

$TaskName = "TradingAgents-India daily refresh"
$repo = Split-Path -Parent $PSScriptRoot
$log  = Join-Path $env:USERPROFILE ".tradingagents\logs\daily_refresh.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

$startAt = [datetime]::ParseExact($Start, "HH:mm", $null)
$endAt   = [datetime]::ParseExact($End, "HH:mm", $null)
$window  = $endAt - $startAt
if ($window.TotalMinutes -lt 20) {
    throw "The window $Start-$End is too short; a scan plus one analysis needs about 20 minutes."
}

# The full path is stored in the task, so resolve a real interpreter now:
# the WindowsApps "python" is a Store installer stub, not Python, and the
# task would fail silently every evening.
$python = (Get-Command python -ErrorAction Stop).Source
if ($python -like "*\WindowsApps\*") {
    throw "'python' resolves to the Microsoft Store stub ($python). Install Python or put it first on PATH."
}
& $python -c "import tradingagents, yfinance" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "$python cannot import tradingagents. Run 'pip install -e .' in $repo with that Python."
}

# Python is run directly (no cmd.exe wrapper): the script writes and rotates
# its own log, flushed line by line, so output up to any crash is kept.
$argLine = "scripts\daily_refresh.py --until $End --catch-up --log `"$log`""
$action  = New-ScheduledTaskAction -Execute $python -Argument $argLine -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $Start
$settingsArgs = @{
    StartWhenAvailable         = $true
    AllowStartIfOnBatteries    = $true
    DontStopIfGoingOnBatteries = $true
    RunOnlyIfNetworkAvailable  = $true
    MultipleInstances          = "IgnoreNew"
    ExecutionTimeLimit         = $window + (New-TimeSpan -Minutes 5)
    WakeToRun                  = [bool]$Wake
}
$settings = New-ScheduledTaskSettingsSet @settingsArgs

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Scan the Nifty 50 and re-analyse only what changed, $Start-$End (TradingAgents-India)." `
    -Force | Out-Null

Write-Host "Registered '$TaskName' - weekdays $Start-$End$(if ($Wake) { ', wakes the PC' })"
Write-Host "  python : $python"
Write-Host "  repo   : $repo"
Write-Host "  log    : $log"
