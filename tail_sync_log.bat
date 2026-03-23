@echo off
setlocal

REM === Tail sync agent log (last lines + follow) ===
set "PROJECT_DIR=C:\CCC_scoringretail"
set "LOG_DIR=%PROJECT_DIR%\logs"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "TODAY=%%I"
set "LOG_FILE=%LOG_DIR%\sync_agent_%TODAY%.log"

if not exist "%LOG_FILE%" (
  echo Log file not found: %LOG_FILE%
  echo Start the sync agent first.
  exit /b 1
)

powershell -NoProfile -Command "Get-Content -Path '%LOG_FILE%' -Tail 120 -Wait"

endlocal
