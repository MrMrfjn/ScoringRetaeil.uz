param(
    [string]$TaskName = "CCC Sync Agent Multi Branch",
    [string]$ProjectDir = "C:\CCC_scoringretail",
    [string]$BatPath = "",
    [int]$RepeatMinutes = 5
)

$ErrorActionPreference = "Stop"

if (-not $BatPath) {
    $BatPath = Join-Path $ProjectDir "deployment\run_sync_agent_multi_branch.bat"
}

if (-not (Test-Path $BatPath)) {
    throw "Batch file not found: $BatPath"
}

Write-Host "Registering task: $TaskName" -ForegroundColor Cyan

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$BatPath`""
$triggerAtStartup = New-ScheduledTaskTrigger -AtStartup
$triggerDaily = New-ScheduledTaskTrigger -Daily -At 00:00
$triggerDaily.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes) -RepetitionDuration ([TimeSpan]::MaxValue)).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
} catch {}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger @($triggerAtStartup, $triggerDaily) `
    -Settings $settings `
    -Description "CCC multi-branch Access sync agent"

Write-Host "[OK] Task registered: $TaskName" -ForegroundColor Green
Write-Host "Use Task Scheduler to set 'Run whether user is logged on or not' if needed." -ForegroundColor Yellow
