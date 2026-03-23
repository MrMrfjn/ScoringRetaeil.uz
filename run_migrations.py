"""
Версионированные миграции схемы БД.
Файлы в migrations/ с именами NNN_description.sql или NNN_description.py выполняются по порядку.
Версия записывается в schema_versions.
"""
import os
import sqlite3
from datetime import datetime

from config.settings import DB_PATH


MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def _current_version(db: sqlite3.Connection) -> int:
    try:
        r = db.execute("SELECT MAX(version) FROM schema_versions").fetchone()
        return int(r[0] or 0)
    except Exception:
        return 0


def _migration_files():
    if not os.path.isdir(MIGRATIONS_DIR):
        return []
    out = []
    for name in os.listdir(MIGRATIONS_DIR):
        if name.startswith(".") or "_" not in name:
            continue
        try:
            num = int(name.split("_")[0])
            out.append((num, name))
        except ValueError:
            continue
    return sorted(out)


def run_pending_migrations() -> int:
    """Применить все миграции с версией больше текущей. Возвращает количество применённых."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    current = _current_version(db)
    applied = 0
    for ver, name in _migration_files():
        if ver <= current:
            continue
        path = os.path.join(MIGRATIONS_DIR, name)
        try:
            if name.endswith(".sql"):
                with open(path, "r", encoding="utf-8") as f:
                    sql = f.read()
                for stmt in sql.split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        db.execute(stmt)
            elif name.endswith(".py"):
                with open(path, "r", encoding="utf-8") as f:
                    code = f.read()
                ns = {"db": db, "sqlite3": sqlite3}
                exec(code, ns)
                if "up" in ns:
                    ns["up"](db)
            db.execute(
                "INSERT INTO schema_versions (version, applied_at) VALUES (?, ?)",
                (ver, datetime.now().isoformat()),
            )
            db.commit()
            applied += 1
        except Exception as e:
            db.rollback()
            raise RuntimeError(f"Migration {name} failed: {e}") from e
    db.close()
    return applied
