"""
CCC — Audit Layer
Centralized audit logging for the Credit Intelligence Platform.
All security-relevant actions are logged with user, action, entity, and details.
"""
from datetime import datetime
from core.database import get_db, log_audit as _log_audit

# Re-export so that "from core.audit import log_audit" works; implementation lives in database.
def log_audit(user: str, action: str, entity: str = None, entity_id: int = None, details: str = None):
    _log_audit(user, action, entity, entity_id, details)


def get_audit_log(limit: int = 100, user: str = None, action: str = None, entity: str = None):
    """
    Read recent audit entries. Optional filters by user, action, entity.
    Returns list of dicts with user, action, entity, entity_id, details, created_at.
    """
    db = get_db()
    conditions = []
    params = []
    if user:
        conditions.append("user = ?")
        params.append(user)
    if action:
        conditions.append("action = ?")
        params.append(action)
    if entity:
        conditions.append("entity = ?")
        params.append(entity)
    where = (" AND " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    rows = db.execute(
        f"SELECT user, action, entity, entity_id, details, created_at FROM audit_log {where} ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]
