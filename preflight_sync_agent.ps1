param(
    [string]$ProjectDir = "C:\CCC_scoringretail",
    [string]$ApiUrl = "http://127.0.0.1:5000",
    [string]$SourcesJson = "",
    [string]$ApiKey = ""
)

$ErrorActionPreference = "Stop"

function Write-Ok($msg) { Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "[FAIL] $msg" -ForegroundColor Red }

try {
    Write-Host "=== CCC Sync Agent Preflight ===" -ForegroundColor Cyan

    if (-not (Test-Path $ProjectDir)) { throw "ProjectDir not found: $ProjectDir" }
    Write-Ok "Project dir exists: $ProjectDir"

    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { throw "Python not found in PATH" }
    Write-Ok "Python found: $($py.Source)"

    $pyVer = python --version 2>&1
    Write-Ok "Python version: $pyVer"

    $pyodbcCheck = python -c "import pyodbc; print('pyodbc_ok')" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "pyodbc import failed: $pyodbcCheck" }
    Write-Ok "pyodbc import works"

    $drivers = Get-OdbcDriver -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "*Access Driver*" }
    if ($drivers) {
        Write-Ok "Access ODBC driver installed: $($drivers[0].Name)"
    } else {
        Write-Warn "Access ODBC driver not detected via Get-OdbcDriver. Verify manually if needed."
    }

    if (-not $SourcesJson) {
        $SourcesJson = Join-Path $ProjectDir "deployment\access_sources.json"
    }
    if (-not (Test-Path $SourcesJson)) {
        throw "Sources JSON not found: $SourcesJson"
    }
    Write-Ok "Sources JSON exists: $SourcesJson"

    $json = Get-Content -Raw -Path $SourcesJson | ConvertFrom-Json
    $entries = @()
    if ($json -is [System.Collections.IDictionary]) {
        foreach ($p in $json.PSObject.Properties) {
            $entries += @{ branch = [string]$p.Name; db = [string]$p.Value }
        }
    } elseif ($json -is [System.Collections.IEnumerable]) {
        foreach ($x in $json) {
            $entries += @{ branch = [string]$x.branch; db = [string]$x.db }
        }
    }
    if ($entries.Count -eq 0) { throw "No branch sources in JSON" }
    Write-Ok "Sources loaded: $($entries.Count)"

    $missing = @()
    foreach ($e in $entries) {
        if (-not (Test-Path $e.db)) { $missing += "$($e.branch) => $($e.db)" }
    }
    if ($missing.Count -gt 0) {
        Write-Warn "Some Access files are missing:"
        $missing | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
    } else {
        Write-Ok "All Access DB paths exist"
    }

    try {
        $health = Invoke-RestMethod -Method Get -Uri "$($ApiUrl.TrimEnd('/'))/health" -TimeoutSec 10
        Write-Ok "API health reachable at $ApiUrl/health"
    } catch {
        Write-Warn "API /health is not reachable now: $($_.Exception.Message)"
    }

    if ($ApiKey) {
        try {
            $headers = @{ "X-API-Key" = $ApiKey; "Content-Type" = "application/json" }
            $resp = Invoke-RestMethod -Method Post -Uri "$($ApiUrl.TrimEnd('/'))/api/sync/flush" -Headers $headers -Body "{}" -TimeoutSec 20
            Write-Ok "Sync API key accepted (/api/sync/flush)"
        } catch {
            Write-Warn "Sync API key check failed: $($_.Exception.Message)"
        }
    } else {
        Write-Warn "ApiKey not provided to preflight. Skip authenticated API check."
    }

    Write-Host "=== Preflight completed ===" -ForegroundColor Cyan
    exit 0
}
catch {
    Write-Fail $_.Exception.Message
    exit 1
}
