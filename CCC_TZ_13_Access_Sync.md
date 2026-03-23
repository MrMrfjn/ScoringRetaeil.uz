# CCC — ТЗ 13: Интеграция MS Access и автоматическая синхронизация филиалов

---

## Принцип

- **MS Access** — только **источник данных** (data source), не основная СУБД.
- **CCC (SQLite / далее PostgreSQL)** — система учёта, скоринг, аналитика.
- Цепочка: **Access (филиал) → Sync Agent → API CCC → Pipeline → движки (feature_store, behavioral, analytics, intelligence).**

---

## Реализовано

### 1. Слой источников данных

- **`data_sources/access_source.py`**
  - Подключение к Access по пути к `.mdb`/`.accdb` через **pyodbc**.
  - Чтение таблиц `clients`, `contracts`, `payments`.
  - **Маппинг полей** на схему CCC (ФИО → full_name, Region → region, Summa → product_amount и т.д.).
  - Опция `only_unsynced` по полю `is_synced` (или аналог).
  - Возврат нормализованных списков словарей для вставки в CCC.

### 2. Data Pipeline Engine

- **`engines/data_pipeline_engine.py`** — метод **`import_from_access(db_path, branch=..., only_unsynced=..., run_engines=...)`**:
  - Вызов `data_sources.access_source.extract_from_access`.
  - Вставка клиентов, договоров, платежей через существующие `import_clients_csv`, `import_contracts_csv`, `import_payments_csv`.
  - После импорта при `run_engines=True` — вызов **`run_recalculate`** (feature_store, behavioral, analytics, intelligence).
  - Логирование: строки импортированы, ошибки, дубликаты (в ответе и логах).

### 3. API синхронизации (для агента в филиалах)

- **Авторизация:** заголовок **X-API-Key** или **Authorization: Bearer &lt;key&gt;** (тот же ключ, что в Developer Portal).
- **Эндпоинты:**
  - **POST /api/sync/client** — тело: один объект или `{"items": [...]}`. Вставка/обновление клиентов.
  - **POST /api/sync/contract** — то же для договоров (обязательно `client_external_id` уже существующего клиента).
  - **POST /api/sync/payment** — то же для платежей (`contract_external_id`, `amount`).
  - **POST /api/sync/flush** — запуск `run_recalculate` после батча (движки).
- При неверном/отсутствующем ключе — 401/403.

### 4. Sync Agent (скрипт в филиале)

- **`sync_agent.py`** — запуск из корня проекта:
  - Подключение к локальной Access БД филиала.
  - Чтение записей с `is_synced = 0` (или аналог) из `clients`, `contracts`, `payments`.
  - Маппинг в формат CCC и отправка на **POST /api/sync/client**, **/api/sync/contract**, **/api/sync/payment**.
  - При успешном ответе — **UPDATE в Access SET is_synced = 1** для отправленной записи.
  - Цикл по умолчанию **каждые 5 минут** (настраивается `--interval`).
  - **Повтор при сбое:** до 3 попыток с задержкой 5 сек.
  - Логирование всех операций в stdout.
  - Режим **однократного запуска:** `--once`.

**Пример запуска:**

```bash
python sync_agent.py --db "C:\branch\data.accdb" --api-url "http://ccc-server:5000" --api-key "ccc_live_..." --branch "Ф1" --interval 300
python sync_agent.py --db "C:\branch\data.accdb" --api-url "http://ccc-server:5000" --api-key "ccc_live_..." --branch "Ф1" --once
```

### 4.1 Режим «у каждого филиала свой Access»

Поддержан мульти-источник в одном агенте:

```bash
# Вариант 1: несколько --branch-access
python sync_agent.py \
  --api-url "http://ccc-server:5000" \
  --api-key "ccc_live_..." \
  --branch-access "Ф1=C:\branches\f1.accdb" \
  --branch-access "Ф2=C:\branches\f2.accdb" \
  --branch-access "Ф3=C:\branches\f3.accdb" \
  --interval 300

# Вариант 2: JSON-файл с источниками
python sync_agent.py \
  --api-url "http://ccc-server:5000" \
  --api-key "ccc_live_..." \
  --branch-access-file "C:\branches\access_sources.json" \
  --interval 300
```

`access_sources.json` может быть:

```json
{
  "Ф1": "C:\\branches\\f1.accdb",
  "Ф2": "C:\\branches\\f2.accdb",
  "Ф3": "C:\\branches\\f3.accdb"
}
```

Готовые файлы для быстрого старта:

- `deployment/access_sources.example.json` — шаблон источников по филиалам.
- `deployment/run_sync_agent_multi_branch.bat` — запуск агента для всех филиалов (дневные логи `logs/sync_agent_YYYY-MM-DD.log`).
  - включает автоочистку логов старше 30 дней (настраивается `LOG_RETENTION_DAYS`).
- `deployment/run_sync_agent_multi_branch_once.bat` — одноразовый запуск (batch sync).
- `deployment/tail_sync_log.bat` — просмотр последних строк и live-tail дневного лога.
- `deployment/bootstrap_sync_agent.ps1` — авто-подготовка (logs + `access_sources.json` из шаблона).
- `deployment/preflight_sync_agent.ps1` — preflight проверки Python/pyodbc/Access/API.
- `deployment/register_task_sync_agent.ps1` — авто-регистрация Scheduled Task.
- `deployment/TASK_SCHEDULER_SYNC_AGENT.md` — пошаговая настройка Windows Task Scheduler.

### 5. Зависимости

- В **requirements.txt** добавлены: **pyodbc**, **requests** (для агента).

---

## Что нужно в Access (рекомендация)

- В таблицах **clients**, **contracts**, **payments** — поле **is_synced** (Integer, 0/1) или аналог.
- После успешной отправки записи агентом — ставить `is_synced = 1`.
- Если такого поля нет — при первом запуске можно выполнить полный выгрузку через **`import_from_access`** (без `only_unsynced`), затем добавить поле и перейти на инкрементальную синхронизацию.

---

## Ручной импорт из файла Access (без агента)

Из кода или админки можно вызывать:

```python
from engines.data_pipeline_engine import import_from_access

result = import_from_access(
    db_path=r"C:\path\to\branch.accdb",
    branch="Ф1",
    only_unsynced=False,
    run_engines=True,
)
# result: status, clients: {inserted, updated, skipped, errors}, contracts: {...}, payments: {...}, engines: {...}
```

---

## Безопасность

- Синк-API доступен только по **API-ключу** (тот же механизм, что для `/api/marketplace/score`).
- Ключи создаются в разделе **Developer Portal**; для каждого филиала можно выдать отдельный ключ и при необходимости отозвать.

---

## Дальнейшие шаги (опционально)

- **Real-time alerts:** при росте NPL по филиалу / менеджеру — уведомления (Telegram, email).
- **Миграция на PostgreSQL** и отказ от Access как источника после перевода филиалов на веб/API.
