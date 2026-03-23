param(
    [string]$ProjectDir = "C:\CCC_scoringretail"
)

$ErrorActionPreference = "Stop"

Write-Host "=== CCC Sync Agent Bootstrap ===" -ForegroundColor Cyan

if (-not (Test-Path $ProjectDir)) {
    throw "ProjectDir not found: $ProjectDir"
}

$deploymentDir = Join-Path $ProjectDir "deployment"
$logsDir = Join-Path $ProjectDir "logs"
$exampleJson = Join-Path $deploymentDir "access_sources.example.json"
$targetJson = Join-Path $deploymentDir "access_sources.json"

if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir | Out-Null
    Write-Host "[OK] Created logs dir: $logsDir" -ForegroundColor Green
} else {
    Write-Host "[OK] Logs dir exists: $logsDir" -ForegroundColor Green
}

if ((Test-Path $exampleJson) -and (-not (Test-Path $targetJson))) {
    Copy-Item $exampleJson $targetJson
    Write-Host "[OK] Created $targetJson from example" -ForegroundColor Green
    Write-Host "[WARN] Fill real Access paths in access_sources.json" -ForegroundColor Yellow
} elseif (Test-Path $targetJson) {
    Write-Host "[OK] Sources file exists: $targetJson" -ForegroundColor Green
} else {
    Write-Host "[WARN] Example JSON not found: $exampleJson" -ForegroundColor Yellow
}

Write-Host "=== Bootstrap done ===" -ForegroundColor Cyan
