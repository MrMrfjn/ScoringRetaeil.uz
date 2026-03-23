# Windows Task Scheduler — Sync Agent (Multi-Branch Access)

## 1) Prepare files

- Copy `deployment/access_sources.example.json` to `deployment/access_sources.json`.
- Fill real paths for each branch Access file.
- Edit `deployment/run_sync_agent_multi_branch.bat`:
  - `PROJECT_DIR`
  - `API_URL`
  - `API_KEY`
  - `SOURCES_JSON` if needed

Optional automation helpers:

- `deployment/bootstrap_sync_agent.ps1` — creates `logs` and initializes `access_sources.json` from example.
- `deployment/preflight_sync_agent.ps1` — checks Python/pyodbc/Access paths/API reachability.
- `deployment/register_task_sync_agent.ps1` — registers Scheduled Task automatically.

## 2) Test manually

Run in CMD as administrator:

```bat
cd /d C:\CCC_scoringretail
deployment\run_sync_agent_multi_branch.bat
```

If needed, test one-shot mode:

```bat
python sync_agent.py --api-url "http://127.0.0.1:5000" --api-key "ccc_live_..." --branch-access-file "deployment\access_sources.json" --once
```

Preflight example:

```powershell
powershell -ExecutionPolicy Bypass -File deployment\preflight_sync_agent.ps1 -ProjectDir "C:\CCC_scoringretail" -ApiUrl "http://127.0.0.1:5000"
```

## 3) Create Scheduled Task

1. Open `Task Scheduler` -> `Create Task...`
2. **General**:
   - Name: `CCC Sync Agent Multi Branch`
   - Run whether user is logged on or not
   - Run with highest privileges
3. **Triggers**:
   - New -> Begin the task: `At startup`
   - Optional second trigger: `Daily`, repeat task every `5 minutes` indefinitely
4. **Actions**:
   - New -> Action: `Start a program`
   - Program/script: `cmd.exe`
   - Add arguments:
     - `/c "C:\CCC_scoringretail\deployment\run_sync_agent_multi_branch.bat"`
5. **Settings**:
   - Allow task to be run on demand
   - If task is already running: `Do not start a new instance`

### Auto-register option (PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -File deployment\register_task_sync_agent.ps1 -ProjectDir "C:\CCC_scoringretail"
```

## 4) Monitoring

- Check Task Scheduler history and last run result.
- Logs are written to daily file:
  - `logs/sync_agent_YYYY-MM-DD.log`
- Old logs cleanup is automatic in `.bat`:
  - `sync_agent_*.log` older than `LOG_RETENTION_DAYS` (default 30) are deleted.
- Quick live view:
  - run `deployment/tail_sync_log.bat`

## 5) Notes

- One API key can be used for all branches, but recommended: separate key per environment.
- If any branch file path changes, update `deployment/access_sources.json`.
- If a branch Access DB is unavailable, sync continues for remaining branches.

## 6) One-shot helper

For manual one-time sync use:

- `deployment/run_sync_agent_multi_branch_once.bat`

This is useful before business day start or after downtime.
