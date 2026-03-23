"""
CCC — Backup Engine
Auto-backup SQLite DB. Keep last 30 copies.
"""
import logging
import shutil
import os
import glob
from datetime import datetime
from config.settings import DB_PATH, BASE_DIR

BACKUP_DIR = os.path.join(BASE_DIR, "backups")
MAX_BACKUPS = 30
_log = logging.getLogger(__name__)


def create_backup():
    """Create timestamped backup of the database."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if not os.path.isdir(BACKUP_DIR):
        err = "Каталог backups недоступен"
        _log.warning(err)
        return {"status": "error", "error": err}
    try:
        with open(os.path.join(BACKUP_DIR, ".write_test"), "w") as _:
            pass
        os.remove(os.path.join(BACKUP_DIR, ".write_test"))
    except Exception as e:
        err = "Нет прав на запись в каталог backups: %s" % e
        _log.warning(err)
        return {"status": "error", "error": err}
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass
        conn.close()
    except Exception as e:
        _log.warning("Checkpoint before backup failed: %s", e)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"backup_{ts}.db")
    try:
        shutil.copy2(DB_PATH, dest)
        _rotate_backups()
        size_mb = os.path.getsize(dest) / 1024 / 1024
        return {"status": "ok", "file": dest, "size_mb": round(size_mb, 1), "timestamp": ts}
    except Exception as e:
        _log.exception("Backup failed: %s", e)
        return {"status": "error", "error": str(e)}

def _rotate_backups():
    """Keep only last MAX_BACKUPS files."""
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "backup_*.db")))
    while len(files) > MAX_BACKUPS:
        os.remove(files.pop(0))

def list_backups():
    """List all backups with size and date."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "backup_*.db")), reverse=True)
    result = []
    for f in files:
        st = os.stat(f)
        result.append({
            "file": os.path.basename(f),
            "size_mb": round(st.st_size / 1024 / 1024, 1),
            "date": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return result

def restore_backup(filename):
    """Restore DB from backup file."""
    src = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(src):
        return {"status": "error", "error": "Файл не найден"}
    # Backup current before restore
    create_backup()
    shutil.copy2(src, DB_PATH)
    return {"status": "ok", "restored_from": filename}
