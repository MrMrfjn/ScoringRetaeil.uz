@echo off
setlocal

REM === CCC Sync Agent (multi-branch Access) ===
REM 1) Adjust variables below
set "PROJECT_DIR=C:\CCC_scoringretail"
set "API_URL=http://127.0.0.1:5000"
set "API_KEY=ccc_live_replace_with_real_key"
set "SOURCES_JSON=%PROJECT_DIR%\deployment\access_sources.json"
set "INTERVAL_SEC=300"
set "LOG_DIR=%PROJECT_DIR%\logs"
set "LOG_RETENTION_DAYS=30"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "TODAY=%%I"
set "LOG_FILE=%LOG_DIR%\sync_agent_%TODAY%.log"

REM Optional: activate venv if needed
REM call "%PROJECT_DIR%\.venv\Scripts\activate.bat"

cd /d "%PROJECT_DIR%"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
REM Cleanup old daily logs (older than LOG_RETENTION_DAYS)
forfiles /p "%LOG_DIR%" /m "sync_agent_*.log" /d -%LOG_RETENTION_DAYS% /c "cmd /c del /q @path" >nul 2>&1
echo [%date% %time%] Starting sync agent, log=%LOG_FILE% >> "%LOG_FILE%"
python sync_agent.py ^
  --api-url "%API_URL%" ^
  --api-key "%API_KEY%" ^
  --branch-access-file "%SOURCES_JSON%" ^
  --interval %INTERVAL_SEC% ^
  >> "%LOG_FILE%" 2>&1

endlocal
