"""
CCC — SaaS Engine
Subscription plans, API keys, billing events.
Supports marketplace scoring API and developer portal.
"""
import secrets
from datetime import datetime
from core.database import get_db


def init_saas_tables():
    """Create SaaS tables: subscription_plans, api_keys, billing_events."""
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS subscription_plans (
        id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
        monthly_price REAL DEFAULT 0, yearly_price REAL DEFAULT 0,
        score_limit_per_month INTEGER DEFAULT 10000,
        features TEXT, is_active INTEGER DEFAULT 1, created_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS api_keys (
        id INTEGER PRIMARY KEY, tenant_id INTEGER DEFAULT 1,
        key_hash TEXT UNIQUE NOT NULL, key_prefix TEXT NOT NULL,
        name TEXT, plan_id INTEGER DEFAULT 1,
        is_active INTEGER DEFAULT 1, created_at TEXT,
        last_used_at TEXT, FOREIGN KEY (plan_id) REFERENCES subscription_plans(id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS billing_events (
        id INTEGER PRIMARY KEY, tenant_id INTEGER DEFAULT 1, api_key_id INTEGER,
        event_type TEXT, quantity INTEGER DEFAULT 1, amount REAL DEFAULT 0,
        details TEXT, created_at TEXT)""")
    # Seed default plans if empty
    if db.execute("SELECT COUNT(*) FROM subscription_plans").fetchone()[0] == 0:
        now = datetime.now().isoformat()
        db.execute("INSERT INTO subscription_plans (id,name,monthly_price,yearly_price,score_limit_per_month,features,is_active,created_at) VALUES (1,'Starter',0,0,1000,'API scoring',1,?)", (now,))
        db.execute("INSERT INTO subscription_plans (id,name,monthly_price,yearly_price,score_limit_per_month,features,is_active,created_at) VALUES (2,'Professional',99,990,50000,'API + dashboards',1,?)", (now,))
        db.execute("INSERT INTO subscription_plans (id,name,monthly_price,yearly_price,score_limit_per_month,features,is_active,created_at) VALUES (3,'Enterprise',299,2990,500000,'Full platform',1,?)", (now,))
    db.commit()
    db.close()


def _hash_key(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()


def create_api_key(tenant_id: int = 1, name: str = "", plan_id: int = 1) -> dict:
    """Generate new API key. Returns raw key (show once) and key_prefix for display."""
    raw = "sk_" + secrets.token_hex(24)
    key_hash = _hash_key(raw)
    key_prefix = raw[:12] + "…"
    db = get_db()
    db.execute("""INSERT INTO api_keys (tenant_id, key_hash, key_prefix, name, plan_id, is_active, created_at)
        VALUES (?,?,?,?,?,1,?)""", (tenant_id, key_hash, key_prefix, name or "API Key", plan_id, datetime.now().isoformat()))
    db.commit()
    pk = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.close()
    return {"id": pk, "api_key": raw, "key_prefix": key_prefix, "name": name or "API Key", "plan_id": plan_id}


def validate_api_key(key: str) -> dict | None:
    """Validate API key; return key record (id, tenant_id, plan_id) or None."""
    if not key or not key.startswith("sk_"):
        return None
    key_hash = _hash_key(key)
    db = get_db()
    row = db.execute(
        "SELECT id, tenant_id, plan_id, name FROM api_keys WHERE key_hash = ? AND is_active = 1",
        (key_hash,),
    ).fetchone()
    if not row:
        db.close()
        return None
    db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (datetime.now().isoformat(), row["id"]))
    db.commit()
    db.close()
    return {"id": row["id"], "tenant_id": row["tenant_id"], "plan_id": row["plan_id"], "name": row["name"]}


def list_api_keys(tenant_id: int = 1) -> list:
    """List API keys for tenant (no raw key, only prefix)."""
    db = get_db()
    rows = db.execute(
        """SELECT id, key_prefix, name, plan_id, is_active, created_at, last_used_at
           FROM api_keys WHERE tenant_id = ? ORDER BY created_at DESC""",
        (tenant_id,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def revoke_api_key(key_id: int, tenant_id: int = 1) -> bool:
    """Deactivate an API key."""
    db = get_db()
    cur = db.execute("UPDATE api_keys SET is_active = 0 WHERE id = ? AND tenant_id = ?", (key_id, tenant_id))
    db.commit()
    db.close()
    return cur.rowcount > 0


def record_billing_event(tenant_id: int, api_key_id: int, event_type: str, quantity: int = 1, amount: float = 0, details: str = ""):
    """Record usage for billing (e.g. score_request)."""
    db = get_db()
    db.execute("""INSERT INTO billing_events (tenant_id, api_key_id, event_type, quantity, amount, details, created_at)
        VALUES (?,?,?,?,?,?,?)""", (tenant_id, api_key_id, event_type, quantity, amount, details, datetime.now().isoformat()))
    db.commit()
    db.close()


def get_usage_count(tenant_id: int, api_key_id: int | None, event_type: str = "score_request", month_start: str = None) -> int:
    """Count events in current month (or given month_start) for rate limiting."""
    db = get_db()
    if not month_start:
        from datetime import date
        month_start = date.today().replace(day=1).isoformat()
    sql = "SELECT COALESCE(SUM(quantity),0) FROM billing_events WHERE tenant_id = ? AND event_type = ? AND created_at >= ?"
    params = [tenant_id, event_type, month_start]
    if api_key_id is not None:
        sql += " AND api_key_id = ?"
        params.append(api_key_id)
    row = db.execute(sql, params).fetchone()
    db.close()
    return int(row[0] or 0)


def list_plans() -> list:
    """List subscription plans."""
    db = get_db()
    rows = db.execute("SELECT * FROM subscription_plans WHERE is_active = 1 ORDER BY monthly_price").fetchall()
    db.close()
    return [dict(r) for r in rows]
