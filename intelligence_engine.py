"""
CCC — Intelligence Engine
Computes data assets: risk tables, training datasets, portfolio feedback.
These tables make the scoring model data-driven, not just rule-based.
"""
from datetime import datetime
from core.database import get_db

def compute_intelligence_tables():
    """
    Build risk intelligence tables from actual portfolio data.
    These feed back into scoring to make it hybrid (rules + data).
    """
    db = get_db()
    now = datetime.now().isoformat()

    # Create tables if needed
    db.execute("""CREATE TABLE IF NOT EXISTS risk_by_age (
        id INTEGER PRIMARY KEY, age_group TEXT, total INTEGER, npl INTEGER,
        pd REAL, avg_debt REAL, avg_paid REAL, computed_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS risk_by_region (
        id INTEGER PRIMARY KEY, region TEXT, total INTEGER, npl INTEGER,
        pd REAL, avg_debt REAL, computed_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS risk_by_income (
        id INTEGER PRIMARY KEY, income_source TEXT, total INTEGER, npl INTEGER,
        pd REAL, avg_debt REAL, computed_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS risk_by_branch (
        id INTEGER PRIMARY KEY, branch TEXT, total INTEGER, npl INTEGER,
        pd REAL, collection_rate REAL, avg_late_days REAL, computed_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS risk_by_product (
        id INTEGER PRIMARY KEY, product_type TEXT, total INTEGER, npl INTEGER,
        pd REAL, avg_debt REAL, avg_product_amount REAL, computed_at TEXT)""")

    # Clear old data
    for t in ['risk_by_age', 'risk_by_region', 'risk_by_income', 'risk_by_branch', 'risk_by_product']:
        try:
            db.execute(f"DELETE FROM {t}")
        except Exception:
            pass

    # ═══ RISK BY AGE ═══
    age_groups = [(18,25,'18-25'),(26,35,'26-35'),(36,45,'36-45'),(46,55,'46-55'),
                  (56,65,'56-65'),(66,75,'66-75'),(76,99,'76+')]
    for lo, hi, label in age_groups:
        row = db.execute("""
            SELECT COUNT(*) as t,
                SUM(CASE WHEN ct.status_detail IN ('Ёмон','МИБ','Судда') OR ct.total_late_days >= 90 THEN 1 ELSE 0 END) as n,
                AVG(ct.debt_amount) as ad, AVG(ct.paid_amount) as ap
            FROM contracts ct JOIN clients c ON c.id = ct.client_id
            WHERE c.age >= ? AND c.age <= ?
        """, (lo, hi)).fetchone()
        total = row['t'] or 0
        npl = row['n'] or 0
        pd = round(npl / total, 4) if total > 0 else 0
        db.execute("INSERT INTO risk_by_age VALUES (NULL,?,?,?,?,?,?,?)",
            (label, total, npl, pd, row['ad'] or 0, row['ap'] or 0, now))

    # ═══ RISK BY REGION ═══
    regions = db.execute("""
        SELECT c.region, COUNT(*) as t,
            SUM(CASE WHEN ct.status_detail IN ('Ёмон','МИБ','Судда') OR ct.total_late_days >= 90 THEN 1 ELSE 0 END) as n,
            AVG(ct.debt_amount) as ad
        FROM contracts ct JOIN clients c ON c.id = ct.client_id
        WHERE c.region IS NOT NULL AND c.region != ''
        GROUP BY c.region HAVING t >= 10
    """).fetchall()
    for r in regions:
        pd = round(r['n'] / r['t'], 4) if r['t'] > 0 else 0
        db.execute("INSERT INTO risk_by_region VALUES (NULL,?,?,?,?,?,?)",
            (r['region'], r['t'], r['n'], pd, r['ad'] or 0, now))

    # ═══ RISK BY INCOME ═══
    incomes = db.execute("""
        SELECT c.income_source, COUNT(*) as t,
            SUM(CASE WHEN ct.status_detail IN ('Ёмон','МИБ','Судда') OR ct.total_late_days >= 90 THEN 1 ELSE 0 END) as n,
            AVG(ct.debt_amount) as ad
        FROM contracts ct JOIN clients c ON c.id = ct.client_id
        WHERE c.income_source IS NOT NULL AND c.income_source != ''
        GROUP BY c.income_source
    """).fetchall()
    for r in incomes:
        pd = round(r['n'] / r['t'], 4) if r['t'] > 0 else 0
        db.execute("INSERT INTO risk_by_income VALUES (NULL,?,?,?,?,?,?)",
            (r['income_source'], r['t'], r['n'], pd, r['ad'] or 0, now))

    # ═══ RISK BY BRANCH ═══
    branches = db.execute("""
        SELECT branch, COUNT(*) as t,
            SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90 THEN 1 ELSE 0 END) as n,
            SUM(paid_amount) as paid, SUM(product_amount) as portfolio,
            AVG(total_late_days) as avg_late
        FROM contracts GROUP BY branch
    """).fetchall()
    for r in branches:
        pd = round(r['n'] / r['t'], 4) if r['t'] > 0 else 0
        coll = round(r['paid'] / r['portfolio'] * 100, 1) if r['portfolio'] > 0 else 0
        db.execute("INSERT INTO risk_by_branch VALUES (NULL,?,?,?,?,?,?,?)",
            (r['branch'], r['t'], r['n'], pd, coll, r['avg_late'] or 0, now))

    # ═══ RISK BY PRODUCT (by product amount bands) ═══
    product_bands = [
        (0, 500_000, 'micro'),
        (500_001, 2_000_000, 'small'),
        (2_000_001, 8_000_000, 'medium'),
        (8_000_001, 20_000_000, 'large'),
        (20_000_001, 1e9, 'premium'),
    ]
    for lo, hi, ptype in product_bands:
        row = db.execute("""
            SELECT COUNT(*) as t,
                SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90 THEN 1 ELSE 0 END) as n,
                AVG(debt_amount) as ad, AVG(product_amount) as ap
            FROM contracts
            WHERE product_amount > ? AND product_amount <= ?
        """, (lo, hi)).fetchone()
        total = row['t'] or 0
        npl = row['n'] or 0
        pd = round(npl / total, 4) if total > 0 else 0
        db.execute("INSERT INTO risk_by_product VALUES (NULL,?,?,?,?,?,?,?)",
            (ptype, total, npl, pd, row['ad'] or 0, row['ap'] or 0, now))

    db.commit()
    db.close()
    return {"status": "ok", "tables": ["risk_by_age", "risk_by_region", "risk_by_income", "risk_by_branch", "risk_by_product"]}


ALLOWED_RISK_TABLES = ("risk_by_age", "risk_by_region", "risk_by_income", "risk_by_branch", "risk_by_product")


def get_risk_table(table_name):
    """Read an intelligence table. table_name must be in ALLOWED_RISK_TABLES."""
    if table_name not in ALLOWED_RISK_TABLES:
        return []
    db = get_db()
    rows = db.execute(f"SELECT * FROM {table_name} ORDER BY pd DESC").fetchall()
    db.close()
    return [dict(r) for r in rows]


def _ensure_training_dataset_columns(db):
    """Ensure training_dataset has score, decision, loan_outcome, dpd_30, dpd_60, dpd_90."""
    for col, ctype in [
        ("score", "INTEGER"), ("decision", "TEXT"), ("loan_outcome", "INTEGER"),
        ("dpd_30", "INTEGER"), ("dpd_60", "INTEGER"), ("dpd_90", "INTEGER"),
    ]:
        try:
            db.execute(f"ALTER TABLE training_dataset ADD COLUMN {col} {ctype}")
        except Exception:
            pass


def prepare_training_dataset():
    """
    Build ML training dataset from actual loan outcomes.
    Each row = one contract with features + label (default/non-default).
    Fields: client_id, score, decision, loan_outcome, dpd_30, dpd_60, dpd_90, plus feature columns.
    """
    db = get_db()

    db.execute("""CREATE TABLE IF NOT EXISTS training_dataset (
        id INTEGER PRIMARY KEY,
        client_id INTEGER, contract_id INTEGER,
        age INTEGER, gender TEXT, income_source TEXT, region TEXT,
        has_car INTEGER, has_card INTEGER, contract_term INTEGER,
        interest_rate REAL, product_amount REAL, advance_payment REAL,
        monthly_payment REAL, late_count INTEGER, total_late_days REAL,
        debt_ratio REAL,
        label INTEGER,
        score INTEGER, decision TEXT, loan_outcome INTEGER,
        dpd_30 INTEGER, dpd_60 INTEGER, dpd_90 INTEGER,
        created_at TEXT)""")
    _ensure_training_dataset_columns(db)

    db.execute("DELETE FROM training_dataset")

    now_ts = datetime.now().isoformat()
    db.execute("""
        INSERT INTO training_dataset
        (client_id, contract_id, age, gender, income_source, region,
         has_car, has_card, contract_term, interest_rate,
         product_amount, advance_payment, monthly_payment,
         late_count, total_late_days, debt_ratio, label,
         score, decision, loan_outcome, dpd_30, dpd_60, dpd_90, created_at)
        SELECT
            c.id, ct.id, c.age, c.gender, c.income_source, c.region,
            c.has_car, c.has_card, ct.contract_term, ct.interest_rate,
            ct.product_amount, ct.advance_payment, ct.monthly_payment,
            ct.late_count, ct.total_late_days,
            CASE WHEN ct.product_amount > 0 THEN ct.debt_amount * 1.0 / ct.product_amount ELSE 0 END,
            CASE WHEN ct.status_detail IN ('Ёмон','МИБ','Судда') OR ct.total_late_days >= 90 THEN 1 ELSE 0 END,
            (SELECT total_score FROM scoring_log sl WHERE sl.client_id = c.id ORDER BY scored_at DESC LIMIT 1),
            (SELECT decision FROM scoring_log sl WHERE sl.client_id = c.id ORDER BY scored_at DESC LIMIT 1),
            CASE WHEN ct.status_detail IN ('Ёмон','МИБ','Судда') OR ct.total_late_days >= 90 THEN 1 ELSE 0 END,
            CASE WHEN (ct.total_late_days IS NOT NULL AND ct.total_late_days >= 30) THEN 1 ELSE 0 END,
            CASE WHEN (ct.total_late_days IS NOT NULL AND ct.total_late_days >= 60) THEN 1 ELSE 0 END,
            CASE WHEN (ct.total_late_days IS NOT NULL AND ct.total_late_days >= 90) THEN 1 ELSE 0 END,
            ?
        FROM contracts ct
        JOIN clients c ON c.id = ct.client_id
    """, (now_ts,))

    count = db.execute("SELECT COUNT(*) FROM training_dataset").fetchone()[0]
    pos = db.execute("SELECT COUNT(*) FROM training_dataset WHERE label=1").fetchone()[0]
    db.commit()
    db.close()

    return {
        "status": "ok",
        "total_records": count,
        "positive_labels": pos,
        "default_rate": round(pos / count * 100, 2) if count > 0 else 0,
    }


def get_portfolio_feedback():
    """
    Portfolio feedback report: identifies high-risk segments
    that should adjust scoring weights.
    """
    db = get_db()

    # Branch risk index
    branch_risk = db.execute("""
        SELECT branch, pd, collection_rate, avg_late_days, total
        FROM risk_by_branch ORDER BY pd DESC
    """).fetchall()

    # Top risky regions
    risky_regions = db.execute("""
        SELECT region, pd, total FROM risk_by_region
        WHERE total >= 50 ORDER BY pd DESC LIMIT 10
    """).fetchall()

    # Risky age groups
    risky_age = db.execute("""
        SELECT age_group, pd, total FROM risk_by_age ORDER BY pd DESC
    """).fetchall()

    # Income risk
    income_risk = db.execute("""
        SELECT income_source, pd, total FROM risk_by_income ORDER BY pd DESC
    """).fetchall()

    db.close()

    return {
        "branch_risk_index": [dict(r) for r in branch_risk],
        "risky_regions_top10": [dict(r) for r in risky_regions],
        "age_risk": [dict(r) for r in risky_age],
        "income_risk": [dict(r) for r in income_risk],
        "recommendations": _generate_recommendations(branch_risk, risky_regions, risky_age),
    }


def _generate_recommendations(branches, regions, ages):
    """Auto-generate portfolio adjustment recommendations."""
    recs = []
    for b in branches:
        if b['pd'] > 0.10:
            recs.append(f"🔴 Филиал {b['branch']}: PD={b['pd']*100:.1f}%, рекомендуется ужесточить скоринг")
        elif b['pd'] > 0.05:
            recs.append(f"🟡 Филиал {b['branch']}: PD={b['pd']*100:.1f}%, мониторинг")

    for r in regions[:3]:
        if r['pd'] > 0.10:
            recs.append(f"🔴 Регион '{r['region']}': PD={r['pd']*100:.1f}% — ограничить выдачу")

    for a in ages:
        if a['pd'] > 0.08:
            recs.append(f"🟡 Возраст {a['age_group']}: PD={a['pd']*100:.1f}% — усилить проверку")

    return recs
