<#
Registers the agent with Windows Task Scheduler (cron does not exist on Windows).

Runs every 15 minutes on weekdays across a local window wide enough to cover the US
session in both CET and CEST (US and EU switch daylight saving on different dates).
The agent itself asks Alpaca's market clock whether the market is open and exits
without logging when it is not, so the wide window costs nothing.

Usage (from the project root):  powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
Remove:                         Unregister-ScheduledTask -TaskName AlpacaRiskAgent -Confirm:$false
#>
param(
    [string]$TaskName = "AlpacaRiskAgent",
    [int]$IntervalMinutes = 15,
    [string]$WindowStart = "14:30",
    [string]$WindowEnd = "22:30"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogDir = Join-Path $ProjectRoot "logs"

if (-not (Test-Path $Python)) { throw "venv not found at $Python - create it first (see README)" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Command = "`"$Python`" -m risk_agent.agent >> `"$LogDir\agent.log`" 2>&1"
$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $Command" -WorkingDirectory $ProjectRoot

$Duration = (New-TimeSpan -Start ([datetime]$WindowStart) -End ([datetime]$WindowEnd))
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $WindowStart
$Repeat = New-ScheduledTaskTrigger -Once -At $WindowStart `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration $Duration
$Trigger.Repetition = $Repeat.Repetition

# IgnoreNew: never start a second run while one is still going (no overlapping order checks).
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Write-Output "Registered '$TaskName': every $IntervalMinutes min, Mon-Fri $WindowStart-$WindowEnd local. Logs: $LogDir\agent.log"
