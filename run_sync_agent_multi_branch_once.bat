@echo off
setlocal

REM === CCC Sync Agent (multi-branch Access, one-shot) ===
set "PROJECT_DIR=C:\CCC_scoringretail"
set "API_URL=http://127.0.0.1:5000"
set "API_KEY=ccc_live_replace_with_real_key"
set "SOURCES_JSON=%PROJECT_DIR%\deployment\access_sources.json"

REM Optional: activate venv if needed
REM call "%PROJECT_DIR%\.venv\Scripts\activate.bat"

cd /d "%PROJECT_DIR%"
python sync_agent.py ^
  --api-url "%API_URL%" ^
  --api-key "%API_KEY%" ^
  --branch-access-file "%SOURCES_JSON%" ^
  --once

endlocal
