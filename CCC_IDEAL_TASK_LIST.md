# CCC (Credit Control Center) — Gap Analysis & Task List to Reach Ideal

**Document purpose:** Structured list of tasks showing how close the system is to "ideal" and what remains to be done.  
**Scope:** Full project analysis (structure, config, security, DB, main app, engines, frontend, import/export, testing, deployment, documentation, code quality).

---

## Выполнено (актуальный прогресс)

- **2.1** — Пути (DB_PATH, UPLOAD_DIR, EXPORT_DIR, DATA_RAW_DOP_BASE) читаются из окружения в `config/settings.py`.
- **2.2** — Загрузка `.env` при старте в `main.py` (python-dotenv).
- **2.3** — В production обязателен SECRET_KEY из env, иначе RuntimeError.
- **5.1** — Обработчики 404, 403, 500 (HTML/JSON), логирование 500.
- **3.1** — CSRF: токен в session, проверка в before_request, скрытое поле во всех формах; исключены /api/, /health, /system/.
- **3.2** — Импорт/экспорт по модулям `import` и `export` (не только admin).
- **9.1** — Каталог `tests/`, smoke-тесты (health, login, 404).
- **9.2** — Юнит-тесты скоринга и аналитики (`tests/test_scoring.py`, `tests/test_analytics.py`).
- **1.1** — Роли: `config.settings.ROLES` формируется из `core.rbac.get_extended_roles_for_settings()` (алиасы director, risk_manager, scoring_officer).
- **10.1** — В README добавлены разделы «Запуск локально и в production» и «Тесты».
- **8.1** — В README и на странице Импорт/Экспорт описано использование папки Доп.база; в `data_raw/Доп.база/README.txt` — пояснение про uploads/exports и про движки в `engines/`.
- **1.2** — Дублирующие движки в Доп.база перенесены в `data_raw/Доп.база/legacy_scripts/`; README обновлён.
- **2.4** — В `config.settings`: FLASK_ENV (development | production), ALLOWED_IMPORT_EXTENSIONS, MAX_UPLOAD_MB; в main — _FLASK_ENV для SECRET_KEY и cookie.
- **3.3** — Импорт: проверка расширения по allowlist, понятное сообщение при неподдерживаемом типе/размере.
- **4.1** — Скрипты `scripts/backup_database.py` и `scripts/health_monitor.py` при локальном запуске подхватывают DB_PATH из config.settings при отсутствии env.
- **1.3** — Шаблон логина вынесен в `templates/login.html`; используется `render_template()`. Остальные страницы пока остаются inline.
- **4.2** — Миграции: таблица `schema_versions`, каталог `migrations/`, `core/run_migrations.py` (применение `NNN_desc.sql` / `NNN_desc.py` по порядку); после `init_db()` вызывается `run_pending_migrations()`.
- **7.1** — При отправке форм кнопка блокируется и текст меняется на «Загрузка…» (скрипт в NAV).
- **10.2** — Логирование: _setup_logging() в main.py (LOG_LEVEL, LOG_PATH), init-сообщения через logger.
- **10.4** — Добавлен wsgi.py (application = app, load_dotenv).
- **11.1** — Документация API: docs/API.md (эндпоинты, методы, авторизация).
- **12.1** — pyproject.toml: [tool.ruff], [tool.pytest.ini_options], описание проекта.
- **3.4** — Аудит: logout, export (data_type), export portfolio; backup и train_ml уже были в api_routes.

---

## Current State (Summary)

The CCC (Credit Intelligence Platform, scoringretail.uz) is a **production-oriented Flask application** with a clear split: `config/`, `core/` (database, RBAC, audit), `engines/` (scoring, analytics, ML, backup, scheduler, import/export), and `routes/` (dashboards, API). Deployment is Docker-based (Gunicorn, nginx, backup and health-monitor containers) with health checks, daily backups, and optional SSL. Security foundations are in place: Argon2/SHA256 password hashing, role-based access (RBAC and `config.settings.ROLES`), audit logging, session hardening, and in-app plus nginx rate limiting. The business logic is rich: rule-based and behavioral scoring, hybrid scoring with ML, feature store, intelligence tables (risk by age/region/branch/product), and scheduler-driven snapshots and alerts.

**Gaps:** Configuration is only partially environment-driven: `config/settings.py` does **not** read `DB_PATH`, `UPLOAD_DIR`, or `EXPORT_DIR` from the environment, so Docker-set env vars are ignored by the main app and paths stay under `BASE_DIR`. There are **no automated tests**. The main app lives in a single ~3.9k-line `main.py` with **inline HTML/CSS/JS templates** (`render_template_string`), no custom 404/500 handlers, and no CSRF protection for forms. Import is restricted by `@role_required('admin')` while settings grant "import" to other roles (e.g. director, branch_manager), and file upload validation is limited (extension/type checks could be stricter). A duplicate set of engines and scripts exists under `data_raw/Доп.база/`, which can cause confusion. Database access uses parameterized queries in most places; a few dynamic SQL usages (e.g. table/column names from fixed lists) are controlled but could be centralized for clarity. Documentation (README, status/audit docs) is good for run and deploy; API and runbooks could be more explicit.

---

## Task List to Reach Ideal

### 1. Project structure

| # | Title | Description | Priority |
|---|--------|-------------|----------|
| 1.1 | Unify role definitions | Single source of truth for roles: merge or clearly derive `config.settings.ROLES` (used for login/nav) and `core.rbac.OPERATIONAL_ROLES` (decorators, can_export, can_import) so adding a role or module does not require editing two places. | High |
| 1.2 | Resolve duplicate engines in `data_raw/Доп.база` | Remove or clearly separate legacy/duplicate engines (e.g. `analytics_engine.py`, `scoring_engine.py`, `db_adapter.py`) under `data_raw/Доп.база` from the main `engines/` used by the app; keep only data files (CSV) or a single adapter if needed for scripts. | Medium |
| 1.3 | Extract templates from `main.py` | Move inline template strings (LOGIN_T, DASHBOARD_T, SCORING_T, etc.) from `main.py` into `templates/*.html` and switch to `render_template()`; align with `templates/README.md` intended structure. | Medium |
| 1.4 | Split `main.py` by domain | Break `main.py` into smaller modules (e.g. `routes/dashboard.py`, `routes/scoring.py`, `routes/alerts.py`, `routes/portfolio.py`, `routes/import_export.py`) and keep `main.py` as a thin app factory that registers blueprints. | Medium |

---

### 2. Configuration and env

| # | Title | Description | Priority |
|---|--------|-------------|----------|
| 2.1 | Load paths from environment in `config/settings.py` | In `config/settings.py`, set `DB_PATH`, `UPLOAD_DIR`, `EXPORT_DIR`, and `DATA_RAW_DOP_BASE` from `os.environ` with fallbacks to current in-repo paths so Docker/env (e.g. `DB_PATH=/data/db/credit_control.db`) are actually used by the app. | High |
| 2.2 | Load `.env` at startup | Call `load_dotenv()` (e.g. in `main.py` or before importing `config.settings`) so local and deployment `.env` files are applied consistently. | High |
| 2.3 | Remove default SECRET_KEY in code | Ensure production never uses the fallback `SECRET_KEY` in `main.py`; require `SECRET_KEY` from env in production and fail fast if missing. | High |
| 2.4 | Separate dev/production config | Use `FLASK_ENV` or a dedicated `ENV` to branch settings (e.g. debug, cookie secure, strict SECRET_KEY) and document in README. | Medium |
| 2.5 | Document all env vars | Add a single place (e.g. README or `env.example` comments) listing every env var (SECRET_KEY, DB_PATH, UPLOAD_DIR, EXPORT_DIR, BACKUP_PATH, FLASK_ENV, HEALTH_URL, BACKUP_RETENTION_DAYS, etc.) with purpose and example. | Low |

---

### 3. Security

| # | Title | Description | Priority |
|---|--------|-------------|----------|
| 3.1 | Add CSRF protection for forms | Protect state-changing forms (login, scoring, import, admin) with CSRF tokens (e.g. Flask-WTF or a minimal token in session + hidden field) and reject requests without valid token. | High |
| 3.2 | Align import route with role model | Change `/import` from `@role_required('admin')` to a check that uses the same "can_import" / module list as in settings/RBAC (e.g. director, head_analyst, branch_manager can import) so behavior matches documentation and role design. | High |
| 3.3 | Harden file upload (import) | Validate uploaded file: extension allowlist (e.g. .csv, .xlsx, .xls), optional MIME check, max size already set; store outside web root; avoid executing or double-opening uploads; return clear error messages without path disclosure. | Medium |
| 3.4 | Ensure audit coverage for sensitive actions | Verify audit log is written for: login/logout, user create/delete, password change, import, export, ML train, backup trigger, and any admin-only actions; add if missing. | Medium |
| 3.5 | Session timeout and invalidation | Configure session lifetime and absolute timeout; invalidate session on password change and optionally on role change. | Low |
| 3.6 | Security headers | Add or reinforce (e.g. in nginx or Flask) headers such as X-Content-Type-Options, X-Frame-Options, Content-Security-Policy (start with report-only if needed). | Low |

---

### 4. Database

| # | Title | Description | Priority |
|---|--------|-------------|----------|
| 4.1 | Use config paths for DB in all code | After 2.1, ensure no code assumes a hardcoded DB path; all DB access goes through `core.database.get_db()` using `config.settings.DB_PATH`. | High |
| 4.2 | Formalize migrations | Introduce a migration tool (e.g. Flask-Migrate/Alembic or a small versioned migration runner) and move schema changes (new tables, new columns) into versioned migration files instead of only `init_db()` and ad-hoc ALTER in code. | Medium |
| 4.3 | Connection handling and timeouts | Use a single pattern for DB: either short-lived connections (get_db/close) everywhere or a connection pool with explicit timeout; ensure no long-held connections in background tasks. | Medium |
| 4.4 | Indexes for heavy queries | Review slow or frequent queries (portfolio by branch/date, alerts, intelligence tables, search) and add indexes (e.g. composite on branch + contract_date, status_detail) where beneficial; document in migration/schema. | Low |
| 4.5 | Backup verification | Optionally add a post-backup integrity check (e.g. open backup DB and run PRAGMA integrity_check) in `scripts/backup_database.py` and log result. | Low |

---

### 5. Main app (main.py)

| # | Title | Description | Priority |
|---|--------|-------------|----------|
| 5.1 | Register 404 and 500 handlers | Add `@app.errorhandler(404)` and `@app.errorhandler(500)` (and optionally 403) returning user-friendly HTML or JSON depending on Accept header, and log server errors without exposing internals. | High |
| 5.2 | Reduce inline SQL in routes | Move complex SELECT/aggregations from route handlers into `core.database` or dedicated engine functions (e.g. dashboard KPIs, portfolio stats, alerts) so routes stay thin and logic is testable. | Medium |
| 5.3 | Consistent error responses for API | Ensure all `/api/*` endpoints return consistent JSON shape (e.g. `{ "error": "..." }` or `{ "ok": true, "data": ... }`) and appropriate HTTP status codes; avoid raw exception messages in production. | Medium |
| 5.4 | Remove duplicate route definitions | If both `main.py` and `routes/dashboards.py` (or api_routes) register the same path, keep a single registration (e.g. only in api_routes/dashboards) to avoid confusion and duplicate logic. | Low |

---

### 6. Engines and business logic

| # | Title | Description | Priority |
|---|--------|-------------|----------|
| 6.1 | Single source for alert thresholds | Keep all alert thresholds in `config.settings` (already partially done); ensure `main.py` and `engines.analytics_engine.run_alerts` use the same constants and document their meaning. | Medium |
| 6.2 | Inject dependencies for testability | Allow engines (e.g. analytics_engine, scoring_engine) to accept an optional DB connection or config so unit tests can run against in-memory SQLite or mocks without touching global state. | Medium |
| 6.3 | Deduplicate alert logic | If dashboard alerts in `main.py` and `engines.analytics_engine.run_alerts` overlap, call the engine from the dashboard route instead of reimplementing queries. | Low |
| 6.4 | Document engine responsibilities | Add a short docstring or README under `engines/` describing each engine’s role (scoring, behavioral, hybrid, analytics, intelligence, ML, backup, integrity, scheduler, import, export) and how they interact. | Low |

---

### 7. Frontend / UX

| # | Title | Description | Priority |
|---|--------|-------------|----------|
| 7.1 | Loading and error states in UI | Add visible loading indicators for slow operations (scoring, import, analytics refresh, export) and clear error messages (e.g. toast or inline) when requests fail. | Medium |
| 7.2 | Mobile-friendly layout | Review key pages (dashboard, scoring form, portfolio, alerts) with responsive breakpoints; ensure tables/cards stack and actions are tappable on small screens. | Medium |
| 7.3 | Theme toggle (light/dark) | Implement the light theme already hinted in CSS variables (`[data-theme="light"]`) with a persistent toggle (e.g. cookie or localStorage) so users can switch. | Low |
| 7.4 | Accessibility basics | Add aria-labels where needed, ensure focus order and keyboard access for main actions, and sufficient contrast for critical text and buttons. | Low |
| 7.5 | i18n readiness | If multiple languages are planned, introduce a minimal i18n layer (e.g. gettext or a dict-based wrapper) for UI strings; keep Russian/Uzbek as default. | Low |

---

### 8. Import / Export

| # | Title | Description | Priority |
|---|--------|-------------|----------|
| 8.1 | Document Доп.база usage | Clearly document that uploads and exports use the "Доп.база" folder (DATA_RAW_DOP_BASE): uploads go to `uploads/`, exports to `exports/`, and that this is the single place for file I/O for import/export. | Medium |
| 8.2 | File type and size errors | Return clear, user-facing messages for unsupported file type, encoding failure, or file too large (e.g. "Поддерживаются только CSV и Excel (.xlsx). Максимальный размер 100 МБ."). | Medium |
| 8.3 | Import result feedback | After import, show summary (rows imported, skipped, errors) and optionally a link or list of first N errors; log full errors server-side for support. | Low |
| 8.4 | Export branch filter consistency | Ensure export respects the same branch filter as the rest of the app (e.g. branch_manager sees only own branch) and document in UI. | Low |

---

### 9. Testing

| # | Title | Description | Priority |
|---|--------|-------------|----------|
| 9.1 | Add test suite and runner | Create a `tests/` package and a test runner (pytest recommended); add a minimal smoke test (e.g. app starts, `/health` returns 200, login page loads). | High |
| 9.2 | Unit tests for scoring and analytics | Add unit tests for `scoring_engine` (rule score, risk class, PD) and key functions in `analytics_engine` (snapshot, alerts) using fixtures or in-memory SQLite. | High |
| 9.3 | Integration tests for auth and RBAC | Test login/logout, session persistence, and access to protected routes for different roles (e.g. analyst cannot access admin, branch_manager sees only own branch). | Medium |
| 9.4 | Fixtures and test DB | Provide scripts or fixtures to create a small test database (clients, contracts, users) so tests do not depend on production data. | Medium |
| 9.5 | Coverage and CI | Add coverage reporting (e.g. pytest-cov) and run tests in CI (e.g. GitHub Actions) on push/PR; aim for critical paths (auth, scoring, import) first. | Low |

---

### 10. Deployment and run

| # | Title | Description | Priority |
|---|--------|-------------|----------|
| 10.1 | Document how to run locally and in production | In README, clearly separate: (1) local dev (e.g. `python main.py` or `flask run`, optional .env), (2) production (Docker Compose, gunicorn command, required env vars). | High |
| 10.2 | Centralize logging | Configure Python logging (level, format, optional file) in one place; ensure gunicorn and Flask log to the same config; avoid print for operational logs. | Medium |
| 10.3 | Health check contract | Document the `/health` response (status, database, optional disk_free_gb) and that 503 is used when degraded; ensure backup/monitor scripts rely on this contract. | Low |
| 10.4 | Optional wsgi.py | Add a small `wsgi.py` that imports `app` from `main` for servers that expect a `application` callable; keep gunicorn command as in Dockerfile. | Low |

---

### 11. Documentation

| # | Title | Description | Priority |
|---|--------|-------------|----------|
| 11.1 | API documentation | Document public API endpoints (e.g. `/api/score`, `/api/hybrid-score/<id>`, `/api/behavioral-score/<id>`, `/api/features/<id>`, `/api/clients/search`, `/health`) with method, parameters, response shape, and auth (session or API key). | Medium |
| 11.2 | Runbooks | Short runbooks for: deploy (`deploy.sh`), SSL renewal, restore from backup, and what to do when health check fails. | Low |
| 11.3 | Keep README and status docs in sync | When adding env vars or services, update README and FINAL_PLATFORM_STATUS / AUDIT_AND_SAFETY_REPORT so they reflect current behavior. | Low |

---

### 12. Code quality

| # | Title | Description | Priority |
|---|--------|-------------|----------|
| 12.1 | Lint and format | Add a linter (e.g. ruff or flake8) and formatter (e.g. black) with config; run in CI or pre-commit so style is consistent. | Medium |
| 12.2 | Type hints on public APIs | Add type hints to engine entry points (e.g. `score_client_by_id`, `compute_daily_snapshot`, `do_import`) and to core.database functions used across the app. | Low |
| 12.3 | Replace magic numbers | Move remaining magic numbers (e.g. limits, time windows, thresholds) into `config.settings` or named constants and document. | Low |
| 12.4 | Avoid wildcard imports | Replace `from config.settings import *` (and similar) in `main.py` with explicit imports to simplify refactors and static analysis. | Low |
| 12.5 | Dynamic SQL review | Keep table/column names used in dynamic SQL (e.g. feature_store_engine, intelligence_engine) validated against allowlists; document the pattern for future changes. | Low |

---

## Priority Order (First 5–10 Tasks for Maximum Impact)

Recommended order to get the most impact quickly:

1. **2.1 – Load paths from environment**  
   Fixes Docker deployment so DB and upload/export paths use mounted volumes; without this, production may write to wrong locations.

2. **2.2 – Load `.env` at startup**  
   Ensures local and production use the same configuration mechanism and env vars are actually applied.

3. **2.3 – Require SECRET_KEY in production**  
   Prevents accidental use of a default secret and improves session/cookie security.

4. **5.1 – 404/500 (and 403) handlers**  
   Better UX and security: no stack traces to users, consistent error pages, and logging of server errors.

5. **3.1 – CSRF protection for forms**  
   Protects login, scoring, import, and admin forms from cross-site request forgery.

6. **3.2 – Align import route with role model**  
   Makes import access consistent with documented roles and avoids confusion between "admin" and "import" module.

7. **9.1 – Add test suite and smoke test**  
   Prevents regressions and gives a base for future tests (scoring, auth, import).

8. **9.2 – Unit tests for scoring and analytics**  
   Protects core business logic (scores, risk class, alerts) when refactoring or adding features.

9. **1.1 – Unify role definitions**  
   Reduces bugs when changing roles or modules and clarifies who can do what.

10. **10.1 – Document run locally vs production**  
    Speeds up onboarding and reduces misconfiguration (e.g. wrong DB path, missing env).

After these, the next most valuable are: **1.2** (clean up Доп.база duplicates), **2.4** (dev vs prod config), **4.2** (migrations), **7.1** (loading/error states), and **11.1** (API docs).

---

*End of document.*
