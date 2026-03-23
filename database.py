"""
CCC FINAL — Database Layer
Architecture: clients → contracts → payments (правильная нормализация)
Analytics: portfolio_snapshots (precomputed, не live‑запросы)
Geo: regions → mfy (каскад)
Auth: Argon2 для новых паролей, SHA256 fallback для legacy.
"""

from __future__ import annotations

import os
import re
import time
import sqlite3
import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List

from config.settings import DB_PATH, DEFAULT_USERS, ROLES


# ═══════════════════════════════════════
#  PASSWORD HASHING
# ═══════════════════════════════════════

def _hash_password(password: str) -> str:
    """Хеширование пароля: Argon2, при отсутствии — SHA256."""
    try:
        from argon2 import PasswordHasher
        return PasswordHasher().hash(password)
    except Exception:
        return hashlib.sha256(password.encode()).hexdigest()


def _verify_password(stored: str, password: str) -> bool:
    """Проверка пароля: Argon2, при несовместимом формате — SHA256."""
    if not stored:
        return False
    if stored.startswith("$argon2"):
        try:
            from argon2 import PasswordHasher
            PasswordHasher().verify(stored, password)
            return True
        except Exception:
            return False
    # legacy SHA256
    return hashlib.sha256(password.encode()).hexdigest() == stored


# ═══════════════════════════════════════
#  DB CONNECTION
# ═══════════════════════════════════════

def get_db() -> sqlite3.Connection:
    _timeout = 60
    _max_attempts = 5
    _delay = 0.5
    last_err = None
    for attempt in range(1, _max_attempts + 1):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=_timeout)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA temp_store=MEMORY")
            except Exception:
                pass
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.OperationalError:
                pass
            return conn
        except sqlite3.OperationalError as e:
            last_err = e
            if attempt < _max_attempts:
                time.sleep(_delay)
    raise last_err


def init_db() -> None:
    """Инициализация БД и безопасная миграция схемы."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    db = get_db()
    c = db.cursor()

    # ═══ USERS ═══
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name        TEXT,
            role        TEXT,
            branch      TEXT,
            is_active   INTEGER DEFAULT 1,
            created_at  TEXT
        )
        """
    )

    # ═══ CLIENTS (master table — одна строка на клиента) ═══
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clients (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id     INTEGER,
            full_name       TEXT NOT NULL,
            gender          TEXT,
            age             INTEGER,
            income_source   TEXT,
            monthly_income  REAL DEFAULT 0,
            position        TEXT,
            workplace       TEXT,
            region          TEXT,
            mfy             TEXT,
            has_car         INTEGER DEFAULT 0,
            has_card        INTEGER DEFAULT 0,
            has_kafeel      INTEGER DEFAULT 0,
            has_mib_court   INTEGER DEFAULT 0,
            admin_fine      REAL DEFAULT 0,
            mib_debt        REAL DEFAULT 0,
            phone           TEXT,
            passport        TEXT,
            created_at      TEXT,
            updated_at      TEXT
        )
        """
    )

    # ═══ CONTRACTS (привязка к client_id) ═══
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS contracts (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id         INTEGER NOT NULL REFERENCES clients(id),
            external_id       INTEGER,
            branch            TEXT,
            product_type      TEXT,
            product_category  TEXT,
            supplier          TEXT,
            contract_date     TEXT,
            contract_end_date TEXT,
            contract_term     INTEGER,
            interest_rate     REAL,
            status            TEXT,
            status_detail     TEXT,
            product_amount    REAL DEFAULT 0,
            advance_payment   REAL DEFAULT 0,
            monthly_payment   REAL DEFAULT 0,
            paid_amount       REAL DEFAULT 0,
            debt_amount       REAL DEFAULT 0,
            overdue_amount    REAL DEFAULT 0,
            late_count        INTEGER DEFAULT 0,
            total_late_days   REAL DEFAULT 0,
            last_payment_date TEXT,
            responsible_person TEXT,
            imported_at       TEXT
        )
        """
    )

    # ═══ GEO ═══
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS geo_regions (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS geo_mfy (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            region_id INTEGER REFERENCES geo_regions(id),
            name      TEXT NOT NULL,
            UNIQUE(region_id, name)
        )
        """
    )

    # ═══ CLIENT BASE (справочник по паспорту/ПИНФЛ для ограничений/поощрений в скоринге) ═══
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS client_base (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            passport    TEXT,
            pinfl       TEXT,
            full_name   TEXT,
            region      TEXT,
            mfy         TEXT,
            limit_type  TEXT,
            notes       TEXT,
            imported_at TEXT
        )
        """
    )
    c.execute("CREATE INDEX IF NOT EXISTS ix_client_base_passport ON client_base(passport)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_client_base_pinfl ON client_base(pinfl)")

    # ═══ SCORING HISTORY ═══
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS scoring_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   INTEGER REFERENCES clients(id),
            total_score INTEGER,
            risk_class  TEXT,
            interest_rate REAL,
            decision    TEXT,
            pd          REAL,
            breakdown   TEXT,
            scored_by   TEXT,
            scored_at   TEXT,
            updated_at  TEXT
        )
        """
    )

    # ═══ ANALYTICS SNAPSHOTS ═══
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date   TEXT NOT NULL,
            branch          TEXT,
            total_contracts INTEGER,
            total_clients   INTEGER,
            portfolio_amount REAL,
            debt_amount     REAL,
            paid_amount     REAL,
            npl_count       INTEGER,
            npl_amount      REAL,
            npl_rate        REAL,
            collection_rate REAL,
            dpd30           INTEGER,
            dpd60           INTEGER,
            dpd90           INTEGER,
            dpd180          INTEGER,
            created_at      TEXT
        )
        """
    )

    # ═══ DASHBOARD SNAPSHOT (основные цифры дашборда с картинки: Портфел, Прри, Хатар) ═══
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_snapshot (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date   TEXT NOT NULL,
            branch          TEXT,
            portfolio       REAL NOT NULL DEFAULT 0,
            prri            REAL NOT NULL DEFAULT 0,
            npl_rate_pct    REAL NOT NULL DEFAULT 0,
            created_at      TEXT
        )
        """
    )
    c.execute("CREATE INDEX IF NOT EXISTS ix_dashboard_snapshot_date ON dashboard_snapshot(snapshot_date)")

    # ═══ MAX LIMITS ═══
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS max_limits (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            client_type       TEXT,
            income_source     TEXT,
            contracts_count   INTEGER,
            max_product_amount REAL,
            max_total_debt    REAL,
            max_overdue_days  INTEGER DEFAULT 60
        )
        """
    )

    # ═══ IMPORT LOG ═══
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS import_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            filename      TEXT,
            file_type     TEXT,
            branch        TEXT,
            rows_imported INTEGER,
            imported_at   TEXT,
            imported_by   TEXT
        )
        """
    )

    # ═══ AUDIT LOG ═══
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user      TEXT,
            action    TEXT,
            entity    TEXT,
            entity_id INTEGER,
            details   TEXT,
            created_at TEXT
        )
        """
    )

    # ═══ PAYMENTS ═══
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_id INTEGER NOT NULL REFERENCES contracts(id),
            client_id   INTEGER REFERENCES clients(id),
            amount      REAL NOT NULL DEFAULT 0,
            payment_date TEXT,
            payment_type TEXT,
            delay_days  INTEGER DEFAULT 0,
            created_at  TEXT,
            updated_at  TEXT
        )
        """
    )

    # ═══ ALERTS ═══
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type  TEXT NOT NULL,
            severity    TEXT DEFAULT 'info',
            message     TEXT,
            entity_type TEXT,
            entity_id   INTEGER,
            branch      TEXT,
            details     TEXT,
            created_at  TEXT,
            updated_at  TEXT
        )
        """
    )
    # Collection workflow fields (backward-compatible with legacy alerts table)
    for col in [
        "client_id INTEGER",
        "branch_id TEXT",
        "stage TEXT",
        "overdue_days INTEGER DEFAULT 0",
        "assigned_to TEXT",
        "status TEXT DEFAULT 'pending'",
        "deadline_date TEXT",
        "priority TEXT",
    ]:
        try:
            c.execute(f"ALTER TABLE alerts ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass

    # ═══ OPEX / FINANCE ═══
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS opex_entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            branch      TEXT,
            category    TEXT NOT NULL,
            subcategory TEXT,
            amount      REAL NOT NULL DEFAULT 0,
            period      TEXT NOT NULL,
            description TEXT,
            created_at  TEXT,
            updated_at  TEXT
        )
        """
    )

    # ═══ TENANTS ═══
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS tenants (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            slug       TEXT UNIQUE,
            is_active  INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    # default tenant
    c.execute(
        """
        INSERT OR IGNORE INTO tenants (id, name, slug, is_active, created_at, updated_at)
        VALUES (1, 'Default', 'default', 1, ?, ?)
        """,
        (datetime.now().isoformat(), datetime.now().isoformat()),
    )

    # ═══ INDEXES ═══
    for idx in [
        "CREATE INDEX IF NOT EXISTS ix_clients_name ON clients(full_name)",
        "CREATE INDEX IF NOT EXISTS ix_clients_ext ON clients(external_id)",
        "CREATE INDEX IF NOT EXISTS ix_contracts_client ON contracts(client_id)",
        "CREATE INDEX IF NOT EXISTS ix_contracts_branch ON contracts(branch)",
        "CREATE INDEX IF NOT EXISTS ix_contracts_status ON contracts(status_detail)",
        "CREATE INDEX IF NOT EXISTS ix_contracts_date ON contracts(contract_date)",
        "CREATE INDEX IF NOT EXISTS ix_scoring_client ON scoring_log(client_id)",
        "CREATE INDEX IF NOT EXISTS ix_snapshots_date ON portfolio_snapshots(snapshot_date)",
        "CREATE INDEX IF NOT EXISTS ix_payments_contract ON payments(contract_id)",
        "CREATE INDEX IF NOT EXISTS ix_payments_date ON payments(payment_date)",
        "CREATE INDEX IF NOT EXISTS ix_alerts_stage ON alerts(stage)",
        "CREATE INDEX IF NOT EXISTS ix_alerts_status ON alerts(status)",
        "CREATE INDEX IF NOT EXISTS ix_alerts_deadline ON alerts(deadline_date)",
    ]:
        c.execute(idx)

    # ═══ MULTI‑TENANT: добавление tenant_id при необходимости ═══
    for tbl, col in [
        ("users", "tenant_id INTEGER DEFAULT 1"),
        ("clients", "tenant_id INTEGER DEFAULT 1"),
        ("contracts", "tenant_id INTEGER DEFAULT 1"),
        ("portfolio_snapshots", "tenant_id INTEGER DEFAULT 1"),
        ("audit_log", "tenant_id INTEGER DEFAULT 1"),
    ]:
        try:
            c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass

    # ═══ updated_at на ключевых таблицах ═══
    for tbl in ["scoring_log", "contracts", "clients"]:
        try:
            c.execute(f"ALTER TABLE {tbl} ADD COLUMN updated_at TEXT")
        except sqlite3.OperationalError:
            pass

    # ═══ Retail analytics dimensions (TZ12) ═══
    for col in ["product_category TEXT", "supplier TEXT"]:
        try:
            c.execute(f"ALTER TABLE contracts ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass

    # ═══ schema_versions для версионированных миграций ═══
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_versions (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )

    # ═══ SEED USERS ═══
    if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        for un, u in DEFAULT_USERS.items():
            c.execute(
                "INSERT OR IGNORE INTO users (username,password_hash,name,role,branch,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (un, u["password_hash"], u.get("name"), u.get("role"), u.get("branch"), datetime.now().isoformat()),
            )

    # ═══ SEED DASHBOARD SNAPSHOT (данные с картинки 16.03.2026: Портфел, Прри, Хатар %) ═══
    if c.execute("SELECT COUNT(*) FROM dashboard_snapshot").fetchone()[0] == 0:
        _snapshot_date = "2026-03-16"
        _snapshot_rows = [
            ("Ф1", 20_571_736_989, 4_024_652_600, 19.564),
            ("Ф2", 18_116_422_064, 3_986_563_154, 22.005),
            ("Ф3", 9_103_221_242, 1_620_982_415, 17.807),
            ("Ф4", 13_972_693_523, 1_160_722_596, 8.307),
            ("Ф5", 7_743_916_789, 703_988_358, 9.091),
            ("Ф6", 2_423_327_479, 88_702_028, 3.660),
            ("Ф7", 2_389_479_157, 85_826_189, 3.592),
            (None, 74_320_797_243, 11_671_437_340, 15.704),
        ]
        _now = datetime.now().isoformat()
        for br, port, prri, npl in _snapshot_rows:
            c.execute(
                "INSERT INTO dashboard_snapshot (snapshot_date, branch, portfolio, prri, npl_rate_pct, created_at) VALUES (?,?,?,?,?,?)",
                (_snapshot_date, br, port, prri, npl, _now),
            )

    db.commit()
    db.close()

    # Применить версионированные миграции (migrations/)
    try:
        from core.run_migrations import run_pending_migrations
        run_pending_migrations()
    except Exception:
        pass


# ═══════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════

def authenticate(username: str, password: str) -> Dict[str, Any] | None:
    """
    Аутентификация пользователя с учётом legacy‑БД (is_active может отсутствовать).
    """
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    db.close()
    if not row:
        return None
    rd = dict(row)
    if int(rd.get("is_active", 1) or 1) != 1:
        return None
    if not _verify_password(rd.get("password_hash", ""), password):
        return None
    return rd


def get_all_users() -> List[Dict[str, Any]]:
    """
    Список активных пользователей для /admin.
    Фильтрация по is_active выполняется на уровне Python.
    """
    db = get_db()
    rows = db.execute("SELECT * FROM users ORDER BY role, username").fetchall()
    db.close()
    res: List[Dict[str, Any]] = []
    for r in rows:
        rd = dict(r)
        if int(rd.get("is_active", 1) or 1) != 1:
            continue
        res.append(rd)
    return res


def create_user(username: str, password: str, name: str, role: str, branch: str | None, created_by: str) -> tuple[bool, str]:
    db = get_db()
    ph = _hash_password(password)
    try:
        db.execute(
            "INSERT INTO users (username,password_hash,name,role,branch,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (username, ph, name, role, branch, datetime.now().isoformat()),
        )
        db.execute(
            "INSERT INTO audit_log (user,action,entity,entity_id,details,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (created_by, "create_user", "user", None, username, datetime.now().isoformat()),
        )
        db.commit()
        db.close()
        return True, "Создан"
    except sqlite3.IntegrityError:
        db.close()
        return False, "Логин занят"


def change_password(username: str, new_pw: str) -> None:
    db = get_db()
    db.execute(
        "UPDATE users SET password_hash=? WHERE username=?",
        (_hash_password(new_pw), username),
    )
    db.commit()
    db.close()


def suggest_clients(query: str, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Лёгкий suggest по ФИО для автодополнения:
    - начинает предлагать с 3 символов,
    - ищет по началу ФИО и по словам внутри.
    """
    q = (query or "").strip()
    if len(q) < 3:
        return []
    db = get_db()
    pattern_start = f"{q}%"
    pattern_word = f"% {q}%"
    rows = db.execute(
        """
        SELECT id, full_name, phone, region, mfy
        FROM clients
        WHERE full_name LIKE ? OR full_name LIKE ?
        ORDER BY full_name
        LIMIT ?
        """,
        (pattern_start, pattern_word, limit),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════
#  CLIENT SEARCH (client_id centric!)
# ═══════════════════════════════════════

def search_clients(query: str, branch: str | None = None, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Поиск клиентов:
    - по ID,
    - по телефону,
    - по паспорту (AB1234567),
    - по ФИО (LIKE).
    Результат всегда агрегирован: одна строка на client_id.
    """
    db = get_db()
    q = (query or "").strip()

    # Фильтр по филиалу — в JOIN, чтобы не отсекать клиентов без договоров (LEFT JOIN + WHERE ct.branch отсекал бы их)
    join_branch = " AND ct.branch = ?" if branch else ""
    base_sql = f"""
        SELECT
            c.id,
            c.full_name,
            c.phone,
            c.passport,
            c.age,
            c.income_source,
            c.region,
            c.gender,
            COUNT(ct.id)                           AS contracts_count,
            COALESCE(SUM(ct.debt_amount), 0)       AS total_debt,
            COALESCE(SUM(ct.paid_amount), 0)       AS total_paid,
            COALESCE(SUM(ct.product_amount), 0)    AS total_product,
            SUM(CASE WHEN ct.status_detail IN ('Ёмон','МИБ','Судда')
                     THEN 1 ELSE 0 END)           AS npl_count,
            MAX(ct.late_count)                     AS max_late_count
        FROM clients c
        LEFT JOIN contracts ct ON ct.client_id = c.id{join_branch}
    """

    where_parts: List[str] = []
    params: List[Any] = []
    if branch:
        params.append(branch)

    # Детекция типа поиска: ID, телефон, паспорт или ФИО
    q_clean = q.replace(" ", "").replace("-", "").replace("+", "")
    q_latin = _cyrillic_to_latin(q.replace(" ", "").replace("-", ""))
    if q.isdigit() and len(q) <= 8:
        where_parts.append("c.id = ?")
        params.append(int(q))
    elif len(q_clean) >= 9 and q_clean.isdigit():
        where_parts.append("REPLACE(REPLACE(REPLACE(c.phone,' ',''),'-',''),'+','') LIKE ?")
        params.append(f"%{q_clean}%")
    elif len(q_latin) >= 7 and q_latin[:2].isalpha() and q_latin[2:].isdigit():
        norm_pass = normalize_passport(q)
        where_parts.append("REPLACE(REPLACE(UPPER(c.passport),' ',''),'-','') = ?")
        params.append(norm_pass)
    else:
        where_parts.append("UPPER(c.full_name) LIKE UPPER(?)")
        params.append(f"%{q.strip()}%")

    where_clause = " WHERE " + " AND ".join(where_parts) if where_parts else ""

    sql = (
        base_sql
        + where_clause
        + " GROUP BY c.id ORDER BY c.full_name LIMIT ?"
    )
    params.append(limit)

    rows = db.execute(sql, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_client(client_id: int) -> Dict[str, Any] | None:
    """Полный профиль клиента с агрегатами по контрактам."""
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not client:
        db.close()
        return None
    client_d = dict(client)

    contracts = db.execute(
        """
        SELECT * FROM contracts
         WHERE client_id = ?
         ORDER BY contract_date DESC
        """,
        (client_id,),
    ).fetchall()
    client_d["contracts"] = [dict(c) for c in contracts]
    client_d["contracts_count"] = len(contracts)

    agg = db.execute(
        """
        SELECT
            COUNT(*)                                     AS cnt,
            COALESCE(SUM(debt_amount), 0)               AS total_debt,
            COALESCE(SUM(paid_amount), 0)               AS total_paid,
            COALESCE(SUM(product_amount), 0)            AS total_product,
            COALESCE(SUM(overdue_amount), 0)            AS total_overdue,
            MAX(late_count)                             AS max_late,
            -- npl_count: ONLY contracts where the bad status AND significant debt remains.
            -- The 'Ёмон'/'МИБ'/'Судда' label persists in the system even after the loan
            -- is paid off (debt ≈ 0). We use a 50 000 sum threshold to exclude paid-off
            -- historical NPL records so they don't penalise currently-clean clients.
            SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда')
                          AND debt_amount > 50000
                     THEN 1 ELSE 0 END)                AS npl_count,
            SUM(CASE WHEN status_detail IN ('МИБ','Судда')
                          AND debt_amount > 50000
                     THEN 1 ELSE 0 END)                AS mib_court_count
        FROM contracts
        WHERE client_id = ?
        """,
        (client_id,),
    ).fetchone()

    client_d["total_debt"] = agg["total_debt"]
    client_d["total_paid"] = agg["total_paid"]
    client_d["total_product"] = agg["total_product"]
    client_d["npl_count"] = agg["npl_count"]
    client_d["max_late"] = agg["max_late"] or 0
    client_d["has_mib_court"] = (agg["mib_court_count"] or 0) > 0

    db.close()
    return client_d


def seed_opex_demo_data() -> int:
    """Seed 12 months of OPEX demo data for all branches and categories if table is empty."""
    import random
    from config.settings import BRANCHES, OPEX_CATEGORIES
    from datetime import datetime

    db = get_db()
    existing = db.execute("SELECT COUNT(*) FROM opex_entries").fetchone()[0]
    if existing > 0:
        db.close()
        return 0
    now = datetime.now()
    inserted = 0
    for month_offset in range(12):
        y = now.year if now.month - month_offset > 0 else now.year - 1
        m = ((now.month - month_offset - 1) % 12) + 1
        period = f"{y}-{m:02d}"
        for br_key in BRANCHES:
            for cat in OPEX_CATEGORIES:
                base = {"Аренда": 8000000, "ФОТ": 25000000, "Маркетинг": 3000000, "IT и связь": 2000000,
                        "Транспорт": 1500000, "Коммунальные": 1200000, "Канцелярия": 500000, "Прочее": 800000}
                amount = base.get(cat, 1000000) * (0.7 + random.random() * 0.6)
                db.execute(
                    "INSERT INTO opex_entries (branch, category, amount, period, created_at) VALUES (?,?,?,?,?)",
                    (br_key, cat, round(amount, 2), period, now.isoformat()),
                )
                inserted += 1
    db.commit()
    db.close()
    return inserted


def backfill_product_type_by_amount() -> int:
    """
    Заполняет product_type по сумме договора (product_amount), если тип не задан.
    Сегменты: Микро (до 0.5 млн), Малый (0.5–2), Средний (2–8), Крупный (8–20), Премиум (20+).
    Возвращает число обновлённых строк.
    """
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        UPDATE contracts
        SET product_type = CASE
            WHEN COALESCE(product_amount, 0) <= 500000 THEN 'Микро (до 0.5 млн)'
            WHEN product_amount <= 2000000 THEN 'Малый (0.5–2 млн)'
            WHEN product_amount <= 8000000 THEN 'Средний (2–8 млн)'
            WHEN product_amount <= 20000000 THEN 'Крупный (8–20 млн)'
            ELSE 'Премиум (20+ млн)'
        END
        WHERE (product_type IS NULL OR product_type = '')
        AND product_amount IS NOT NULL AND product_amount > 0
    """)
    n = cur.rowcount
    db.commit()
    db.close()
    return n


# ═══════════════════════════════════════
#  GEO
# ═══════════════════════════════════════

def get_regions() -> List[Dict[str, Any]]:
    db = get_db()
    rows = db.execute("SELECT id, name FROM geo_regions ORDER BY name").fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_mfy_by_region(region_id: int) -> List[Dict[str, Any]]:
    db = get_db()
    rows = db.execute(
        "SELECT id, name FROM geo_mfy WHERE region_id = ? ORDER BY name",
        (region_id,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# Регионы по умолчанию, если в БД пусто (для выбора в скоринге)
DEFAULT_REGIONS = [
    "Ташкент", "Фаргона", "Самарканд", "Бухара", "Наманган", "Андижан",
    "Коканд", "Нукус", "Маргилан", "Ургенч", "Карши", "Термез", "Навои",
]


def get_region_names() -> List[str]:
    """Возвращает список регионов для UI.

    1) Если заполнена справочная таблица geo_regions — берём из неё.
    2) Иначе — DISTINCT region из clients.
    3) Если и там пусто — возвращаем DEFAULT_REGIONS.
    """
    db = get_db()
    result: List[str] = []
    try:
        rows = db.execute(
            "SELECT DISTINCT name FROM geo_regions ORDER BY name"
        ).fetchall()
        if rows:
            result = [r[0] for r in rows]
    except sqlite3.OperationalError:
        pass
    if not result:
        try:
            rows = db.execute(
                "SELECT DISTINCT region FROM clients "
                "WHERE region IS NOT NULL AND region != '' "
                "ORDER BY region"
            ).fetchall()
            result = [r[0] for r in rows]
        except sqlite3.OperationalError:
            pass
    db.close()
    if not result:
        result = list(DEFAULT_REGIONS)
    return result


def get_mfy_by_region_name(region_name: str) -> List[Dict[str, Any]]:
    """Вернуть полный список МФЙ по названию региона из geo_mfy + clients."""
    db = get_db()
    out: List[Dict[str, Any]] = []
    name = (region_name or "").strip()
    if not name:
        db.close()
        return []
    names: List[str] = []
    try:
        rows = db.execute(
            """
            SELECT DISTINCT name
            FROM geo_regions
            WHERE TRIM(name)=TRIM(?)
               OR TRIM(name) LIKE TRIM(?) || '%'
               OR TRIM(name) LIKE '%' || TRIM(?) || '%'
            ORDER BY name
            """,
            (name, name, name),
        ).fetchall()
        names = [r["name"] for r in rows if r["name"]]
    except sqlite3.OperationalError:
        names = []
    if not names:
        names = [name]
    mfy_names = set()
    try:
        for n in names:
            rows = db.execute(
                """
                SELECT gm.name
                FROM geo_mfy gm
                JOIN geo_regions gr ON gr.id = gm.region_id
                WHERE TRIM(gr.name)=TRIM(?)
                   OR TRIM(gr.name) LIKE TRIM(?) || '%'
                   OR TRIM(gr.name) LIKE '%' || TRIM(?) || '%'
                ORDER BY gm.name
                """,
                (n, n, n),
            ).fetchall()
            for r in rows:
                if r["name"]:
                    mfy_names.add(r["name"])
    except sqlite3.OperationalError:
        pass
    try:
        for n in names:
            rows = db.execute(
                """
                SELECT DISTINCT mfy AS name
                FROM clients
                WHERE mfy IS NOT NULL AND mfy != ''
                  AND (
                        TRIM(region) = TRIM(?)
                     OR TRIM(region) LIKE TRIM(?) || '%'
                     OR TRIM(region) LIKE '%' || TRIM(?) || '%'
                  )
                ORDER BY mfy
                """,
                (n, n, n),
            ).fetchall()
            for r in rows:
                if r["name"]:
                    mfy_names.add(r["name"])
    except sqlite3.OperationalError:
        pass
    out = [{"id": i + 1, "name": v} for i, v in enumerate(sorted(mfy_names))]
    db.close()
    return out


_CYR_TO_LAT = str.maketrans(
    "АВСДЕФГНИКЛМОРТХавсдефгниклмортх",
    "ABCDEFGHIKLMOPTXabcdefghiklmoptx",
)


def _cyrillic_to_latin(s: str) -> str:
    """Convert Cyrillic passport-series letters to their Latin equivalents."""
    return s.translate(_CYR_TO_LAT)


def normalize_passport(passport: str | None) -> str:
    """Canonical passport: uppercase Latin, no spaces/hyphens/nbsp. AB1234567.
    Converts Cyrillic series letters to Latin (АВ → AB)."""
    if not passport:
        return ""
    cleaned = re.sub(r"[\s\-\u00a0]", "", str(passport).strip()).upper()
    return _cyrillic_to_latin(cleaned)


def compose_passport(series: str | None = None, number: str | None = None, passport: str | None = None) -> str | None:
    """Build canonical passport from full field or series+number parts."""
    full = normalize_passport(passport)
    if full and len(full) >= 5:
        return full
    s = re.sub(r"[\s\-\u00a0]", "", (series or "").strip()).upper()
    s = _cyrillic_to_latin(s)
    n = re.sub(r"[\s\-\u00a0]", "", (number or "").strip())
    combined = s + n
    return combined if combined else None


def normalize_pinfl(pinfl: str | None) -> str | None:
    """Digits only from PINFL/JSHSHIR."""
    if not pinfl:
        return None
    digits = re.sub(r"[^\d]", "", str(pinfl))
    return digits if len(digits) >= 9 else None


_CB_COLS = "id, passport, pinfl, full_name, region, mfy, limit_type, notes"


def get_client_base_match(passport: str | None = None, pinfl: str | None = None) -> Dict[str, Any] | None:
    """Lookup in client_base by passport or PINFL. Used during scoring for restriction/incentive."""
    db = get_db()
    try:
        if pinfl:
            pinfl_clean = normalize_pinfl(pinfl)
            if pinfl_clean:
                row = db.execute(
                    f"SELECT {_CB_COLS} FROM client_base WHERE REPLACE(REPLACE(pinfl,' ',''),'-','') = ? LIMIT 1",
                    (pinfl_clean,),
                ).fetchone()
                if row:
                    return dict(row)
        if passport:
            norm = normalize_passport(passport)
            if len(norm) >= 5:
                row = db.execute(
                    f"SELECT {_CB_COLS} FROM client_base WHERE REPLACE(REPLACE(UPPER(TRIM(passport)),' ',''),'-','') = ? LIMIT 1",
                    (norm,),
                ).fetchone()
                if row:
                    return dict(row)
            tail = re.sub(r"[^\d]", "", norm)
            if tail and 6 <= len(tail) <= 8:
                row = db.execute(
                    f"SELECT {_CB_COLS} FROM client_base WHERE REPLACE(REPLACE(UPPER(TRIM(passport)),' ',''),'-','') LIKE ? LIMIT 1",
                    (f"%{tail}",),
                ).fetchone()
                if row:
                    return dict(row)
    except sqlite3.OperationalError:
        pass
    finally:
        db.close()
    return None


def search_client_base(query: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Search client_base by passport, PINFL, or FIO. Returns list of dicts with source='client_base'."""
    db = get_db()
    results = []
    try:
        q = query.strip()
        if not q:
            return []
        norm = normalize_passport(q)
        digits = re.sub(r"[^\d]", "", q)

        found_ids = set()

        def _add(row):
            if row["id"] not in found_ids:
                found_ids.add(row["id"])
                d = dict(row)
                results.append({
                    "source": "client_base",
                    "client_id": None, "client_code": None, "branch": None,
                    "full_name": d.get("full_name"), "phone": None, "age": None,
                    "passport": d.get("passport"), "pinfl": d.get("pinfl"),
                    "region": d.get("region"), "mfy": d.get("mfy"),
                    "contracts_count": 0, "total_debt": 0, "max_delay": 0, "npl_count": 0,
                    "limit_type": d.get("limit_type"), "notes": d.get("notes"),
                    "has_client_base_match": True,
                })

        if norm and len(norm) >= 5:
            for r in db.execute(
                f"SELECT {_CB_COLS} FROM client_base WHERE REPLACE(REPLACE(UPPER(TRIM(passport)),' ',''),'-','') = ? LIMIT ?",
                (norm, limit),
            ).fetchall():
                _add(r)

        if digits and 6 <= len(digits) <= 8 and len(results) < limit:
            for r in db.execute(
                f"SELECT {_CB_COLS} FROM client_base WHERE REPLACE(REPLACE(UPPER(TRIM(passport)),' ',''),'-','') LIKE ? LIMIT ?",
                (f"%{digits}", limit),
            ).fetchall():
                _add(r)

        pinfl_clean = normalize_pinfl(q)
        if pinfl_clean and len(results) < limit:
            for r in db.execute(
                f"SELECT {_CB_COLS} FROM client_base WHERE REPLACE(REPLACE(pinfl,' ',''),'-','') = ? LIMIT ?",
                (pinfl_clean, limit),
            ).fetchall():
                _add(r)

        if not results and len(q) >= 3:
            words = [w for w in q.split() if len(w) >= 2]
            if words:
                cond = " AND ".join(["UPPER(full_name) LIKE UPPER(?)"] * len(words))
                params = [f"%{w}%" for w in words] + [limit]
                for r in db.execute(
                    f"SELECT {_CB_COLS} FROM client_base WHERE {cond} LIMIT ?", params
                ).fetchall():
                    _add(r)
    except sqlite3.OperationalError:
        pass
    finally:
        db.close()
    return results[:limit]


def search_scoring_candidates(query: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Unified scoring search: one row per client (no branch duplication)."""
    db = get_db()
    client_rows = []
    try:
        q = query.strip()
        if not q:
            return []
        norm = normalize_passport(q)
        q_latin = _cyrillic_to_latin(q.replace(' ', '').replace('-', ''))
        digits = re.sub(r"[^\d]", "", q)
        base_sql = """
            SELECT
                cl.id AS client_id,
                cl.external_id AS client_code,
                (
                    SELECT c2.branch
                    FROM contracts c2
                    WHERE c2.client_id = cl.id
                    ORDER BY
                        (SELECT COUNT(*) FROM contracts c3 WHERE c3.client_id=cl.id AND c3.branch=c2.branch) DESC,
                        date(c2.contract_date) DESC,
                        c2.id DESC
                    LIMIT 1
                ) AS branch,
                cl.full_name, cl.phone, cl.age, cl.passport, cl.income_source,
                COUNT(ct.id) AS contracts_count,
                COALESCE(SUM(ct.debt_amount),0) AS total_debt,
                MAX(ct.total_late_days) AS max_delay,
                SUM(CASE WHEN ct.status_detail IN ('Ёмон','МИБ','Судда') THEN 1 ELSE 0 END) AS npl_count
            FROM clients cl
            LEFT JOIN contracts ct ON ct.client_id=cl.id
        """
        rows = []
        if re.match(r'^[A-Za-zА-Яа-яЁё]{2}\d{5,8}$', q.replace(' ', '').replace('-', '')):
            rows = db.execute(base_sql + " WHERE UPPER(REPLACE(REPLACE(cl.passport,' ',''),'-',''))=? GROUP BY cl.id", (norm,)).fetchall()
        elif digits and len(digits) >= 13:
            rows = db.execute(base_sql + " WHERE cl.external_id=? GROUP BY cl.id", (int(digits),)).fetchall()
        elif digits and len(digits) <= 8:
            rows = db.execute(base_sql + " WHERE (cl.id=? OR cl.external_id=? OR REPLACE(UPPER(cl.passport),' ','') LIKE '%'||?||'%') GROUP BY cl.id", (int(digits), int(digits), digits)).fetchall()
        elif len(digits) >= 7:
            rows = db.execute(base_sql + " WHERE REPLACE(REPLACE(REPLACE(cl.phone,' ',''),'-',''),'+','') LIKE ? GROUP BY cl.id LIMIT 50", (f"%{digits}%",)).fetchall()
        else:
            words = [w for w in q.split() if len(w) >= 2]
            if words:
                cond = " AND ".join(["UPPER(cl.full_name) LIKE UPPER(?)"] * len(words))
                rows = db.execute(base_sql + f" WHERE {cond} GROUP BY cl.id ORDER BY cl.full_name LIMIT 50", [f"%{w}%" for w in words]).fetchall()
                if not rows:
                    # Fallback: looser match for real-world typos/word order mismatches.
                    cond_or = " OR ".join(["UPPER(cl.full_name) LIKE UPPER(?)"] * len(words))
                    rows = db.execute(base_sql + f" WHERE ({cond_or}) GROUP BY cl.id ORDER BY cl.full_name LIMIT 50", [f"%{w}%" for w in words]).fetchall()
        client_rows = [dict(r) for r in rows]
        for r in client_rows:
            r["source"] = "clients"
            r["has_client_base_match"] = False
            r["limit_type"] = None
            r["notes"] = None
    except Exception:
        pass
    finally:
        db.close()

    cb_rows = search_client_base(query, limit=limit)

    seen_passports = set()
    seen_pinfls = set()
    for cr in client_rows:
        p = normalize_passport(cr.get("passport"))
        if p:
            seen_passports.add(p)
        ext = cr.get("client_code")
        if ext:
            seen_pinfls.add(str(ext))

    for cbr in cb_rows:
        p = normalize_passport(cbr.get("passport"))
        pinfl = normalize_pinfl(cbr.get("pinfl"))
        if p and p in seen_passports:
            for cr in client_rows:
                if normalize_passport(cr.get("passport")) == p:
                    cr["has_client_base_match"] = True
                    cr["limit_type"] = cbr.get("limit_type")
                    cr["notes"] = cbr.get("notes")
                    break
            continue
        if pinfl and pinfl in seen_pinfls:
            for cr in client_rows:
                if str(cr.get("client_code")) == pinfl:
                    cr["has_client_base_match"] = True
                    cr["limit_type"] = cbr.get("limit_type")
                    cr["notes"] = cbr.get("notes")
                    break
            continue
        client_rows.append(cbr)

    return client_rows[:limit]


# ═══════════════════════════════════════
#  DASHBOARD SNAPSHOT (основные цифры с картинки)
# ═══════════════════════════════════════

def get_latest_dashboard_snapshot_date() -> str | None:
    """Дата последнего снимка дашборда (Портфел, Прри, Хатар)."""
    db = get_db()
    row = db.execute(
        "SELECT snapshot_date FROM dashboard_snapshot ORDER BY snapshot_date DESC LIMIT 1"
    ).fetchone()
    db.close()
    return row[0] if row else None


def get_dashboard_snapshot(snapshot_date: str | None = None, branch_filter: str | None = None) -> Dict[str, Any] | None:
    """
    Основные цифры дашборда из снимка (картинка): Портфел, Прри, Хатар %.
    Возвращает { "snapshot_date", "kpi": {total, portfolio, prri, npl_rate, ...}, "by_branch": [...] }
    или None, если снимка нет.
    """
    db = get_db()
    date_to_use = snapshot_date or get_latest_dashboard_snapshot_date()
    if not date_to_use:
        db.close()
        return None
    flt = "AND branch = ?" if branch_filter else ""
    params: List[Any] = [date_to_use]
    if branch_filter:
        params.append(branch_filter)
    rows = db.execute(
        f"""
        SELECT branch, portfolio, prri, npl_rate_pct
        FROM dashboard_snapshot
        WHERE snapshot_date = ? {flt}
        ORDER BY CASE WHEN branch IS NULL THEN 1 ELSE 0 END, branch
        """,
        params,
    ).fetchall()
    db.close()
    if not rows:
        return None
    by_branch = [dict(r) for r in rows if r["branch"]]
    total_row = next((r for r in rows if r["branch"] is None), None)
    total_portfolio = total_row["portfolio"] if total_row else sum(r["portfolio"] for r in by_branch)
    total_prri = total_row["prri"] if total_row else sum(r["prri"] for r in by_branch)
    total_npl_pct = total_row["npl_rate_pct"] if total_row else (
        round(sum(r["portfolio"] * (r["npl_rate_pct"] or 0) for r in by_branch) / total_portfolio, 2) if total_portfolio else 0
    )
    return {
        "snapshot_date": date_to_use,
        "kpi": {
            "total": len(by_branch),
            "portfolio": total_portfolio,
            "prri": total_prri,
            "npl_rate": total_npl_pct,
            "collection_rate": round((total_portfolio - total_prri) / total_portfolio * 100, 1) if total_portfolio else 0,
        },
        "by_branch": by_branch,
    }


# ═══════════════════════════════════════
#  PORTFOLIO / ANALYTICS HELPERS
# ═══════════════════════════════════════

def get_portfolio_stats(
    branch: str | None = None,
    year: str | None = None,
    product_type: str | None = None,
) -> Dict[str, Any]:
    db = get_db()
    where = ["1=1"]
    params: List[Any] = []
    if branch:
        where.append("branch = ?")
        params.append(branch)
    if year:
        where.append("strftime('%Y', contract_date) = ?")
        params.append(year)
    if product_type:
        where.append("product_type = ?")
        params.append(product_type)
    w = " AND ".join(where)
    row = db.execute(
        f"""
        SELECT
            COUNT(*)                               AS total_contracts,
            COUNT(DISTINCT client_id)              AS total_clients,
            COALESCE(SUM(product_amount), 0)       AS portfolio,
            COALESCE(SUM(debt_amount), 0)          AS debt,
            COALESCE(SUM(paid_amount), 0)          AS paid,
            SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда')
                     THEN 1 ELSE 0 END)           AS npl_count,
            COUNT(DISTINCT branch)                 AS branch_count,
            COUNT(DISTINCT responsible_person)     AS emp_count
        FROM contracts
        WHERE {w}
        """,
        params,
    ).fetchone()
    db.close()
    stats = dict(row)
    tc = stats["total_contracts"] or 0
    portfolio = stats["portfolio"] or 0
    stats["npl_rate"] = round(stats["npl_count"] / tc * 100, 2) if tc > 0 else 0
    stats["collection_rate"] = round(stats["paid"] / portfolio * 100, 1) if portfolio > 0 else 0
    return stats


def get_npl_by_branch(
    year: str | None = None,
    product_type: str | None = None,
) -> List[Dict[str, Any]]:
    db = get_db()
    where = ["1=1"]
    params: List[Any] = []
    if year:
        where.append("strftime('%Y', contract_date) = ?")
        params.append(year)
    if product_type:
        where.append("product_type = ?")
        params.append(product_type)
    w = " AND ".join(where)
    rows = db.execute(
        f"""
        SELECT
            branch,
            COUNT(*)                                      AS total,
            SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда')
                     THEN 1 ELSE 0 END)                  AS npl_count,
            SUM(debt_amount)                             AS debt,
            SUM(paid_amount)                             AS paid,
            SUM(product_amount)                          AS portfolio
        FROM contracts
        WHERE {w}
        GROUP BY branch
        ORDER BY branch
        """,
        params,
    ).fetchall()
    db.close()
    result: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        total = d["total"] or 0
        portfolio = d["portfolio"] or 0
        d["npl_rate"] = round(d["npl_count"] / total * 100, 2) if total > 0 else 0
        d["collection_rate"] = round(d["paid"] / portfolio * 100, 1) if portfolio > 0 else 0
        result.append(d)
    return result


def get_portfolio_years() -> List[str]:
    """Distinct contract years for filters."""
    db = get_db()
    rows = db.execute(
        "SELECT DISTINCT strftime('%Y', contract_date) AS y FROM contracts "
        "WHERE contract_date IS NOT NULL ORDER BY y DESC"
    ).fetchall()
    db.close()
    return [r["y"] for r in rows if r["y"]]


def get_product_types() -> List[str]:
    """Distinct product types for filters."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT DISTINCT product_type FROM contracts "
            "WHERE product_type IS NOT NULL AND product_type != '' "
            "ORDER BY product_type"
        ).fetchall()
    except sqlite3.OperationalError:
        # Старые базы без колонки product_type: просто нет фильтра по продукту.
        db.close()
        return []
    db.close()
    return [r[0] for r in rows]


def get_import_log() -> List[Dict[str, Any]]:
    db = get_db()
    rows = db.execute(
        "SELECT * FROM import_log ORDER BY imported_at DESC LIMIT 50"
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def count_contracts(branch: str | None = None) -> int:
    db = get_db()
    if branch:
        r = db.execute(
            "SELECT COUNT(*) AS c FROM contracts WHERE branch=?",
            (branch,),
        ).fetchone()
    else:
        r = db.execute("SELECT COUNT(*) AS c FROM contracts").fetchone()
    db.close()
    return int(r["c"] or 0)


def count_clients() -> int:
    db = get_db()
    r = db.execute("SELECT COUNT(*) AS c FROM clients").fetchone()
    db.close()
    return int(r["c"] or 0)


def log_audit(user: str, action: str, entity: str | None = None, entity_id: int | None = None, details: str | None = None) -> None:
    db = get_db()
    db.execute(
        "INSERT INTO audit_log (user,action,entity,entity_id,details,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (user, action, entity, entity_id, details, datetime.now().isoformat()),
    )
    db.commit()
    db.close()

