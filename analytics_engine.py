"""
CCC FINAL — Analytics Engine
RULE: Precompute metrics into snapshots. UI reads from snapshots.
Пороги алертов синхронизированы с config.settings (смягчены ~30%).
"""
from datetime import datetime, timedelta
from core.database import get_db, backfill_product_type_by_amount
from config.settings import (
    ALERT_NPL_BRANCH_CRITICAL_PCT,
    ALERT_NPL_BRANCH_WARNING_PCT,
    ALERT_RISKY_EMPLOYEE_NPL_PCT,
    ALERT_EMPLOYEE_MIN_CONTRACTS,
    ALERT_LOW_COLLECTION_PCT,
    ALERT_LOW_COLLECTION_MIN_CONTRACTS,
)


def compute_daily_snapshot():
    """
    Run once per day (or on demand).
    Fills product_type from product_amount bands if empty; then computes portfolio metrics.
    """
    try:
        backfill_product_type_by_amount()
    except Exception:
        pass
    db = get_db()
    today = datetime.now().strftime('%Y-%m-%d')

    # Delete today's snapshots (idempotent)
    db.execute("DELETE FROM portfolio_snapshots WHERE snapshot_date = ?", (today,))

    # Compute per-branch
    branches = db.execute("SELECT DISTINCT branch FROM contracts WHERE branch IS NOT NULL").fetchall()
    branches = [r[0] for r in branches] + [None]  # None = total

    for branch in branches:
        w = " AND branch = ?" if branch else ""
        p = (branch,) if branch else ()

        row = db.execute(f"""
            SELECT COUNT(*) as total_contracts,
                COUNT(DISTINCT client_id) as total_clients,
                COALESCE(SUM(product_amount), 0) as portfolio,
                COALESCE(SUM(debt_amount), 0) as debt,
                COALESCE(SUM(paid_amount), 0) as paid,
                SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90 THEN 1 ELSE 0 END) as npl_count,
                COALESCE(SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90 THEN debt_amount ELSE 0 END), 0) as npl_amount,
                SUM(CASE WHEN total_late_days >= 30 THEN 1 ELSE 0 END) as dpd30,
                SUM(CASE WHEN total_late_days >= 60 THEN 1 ELSE 0 END) as dpd60,
                SUM(CASE WHEN total_late_days >= 90 THEN 1 ELSE 0 END) as dpd90,
                SUM(CASE WHEN total_late_days >= 180 THEN 1 ELSE 0 END) as dpd180
            FROM contracts WHERE 1=1 {w}
        """, p).fetchone()

        tc = row['total_contracts'] or 0
        portfolio = row['portfolio'] or 0
        npl_rate = round(row['npl_count'] / tc * 100, 2) if tc > 0 else 0
        coll_rate = round(row['paid'] / portfolio * 100, 1) if portfolio > 0 else 0

        db.execute("""
            INSERT INTO portfolio_snapshots
            (snapshot_date, branch, total_contracts, total_clients, portfolio_amount,
             debt_amount, paid_amount, npl_count, npl_amount, npl_rate, collection_rate,
             dpd30, dpd60, dpd90, dpd180, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (today, branch or 'ALL', tc, row['total_clients'],
              portfolio, row['debt'], row['paid'],
              row['npl_count'], row['npl_amount'],
              npl_rate, coll_rate,
              row['dpd30'], row['dpd60'], row['dpd90'], row['dpd180'],
              datetime.now().isoformat()))

    db.commit()
    db.close()
    return True


def get_latest_snapshot(branch=None):
    """Get most recent snapshot."""
    db = get_db()
    br = branch or 'ALL'
    row = db.execute("""
        SELECT * FROM portfolio_snapshots
        WHERE branch = ? ORDER BY snapshot_date DESC LIMIT 1
    """, (br,)).fetchone()
    db.close()
    return dict(row) if row else None


def get_snapshot_history(branch=None, days=90):
    """Get snapshot trend for charts."""
    db = get_db()
    br = branch or 'ALL'
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    rows = db.execute("""
        SELECT * FROM portfolio_snapshots
        WHERE branch = ? AND snapshot_date >= ?
        ORDER BY snapshot_date
    """, (br, cutoff)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def run_alerts(branch=None, days: int = 365):
    """Early warning system based on latest data (last `days` days)."""
    db = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    alerts = []

    w = " AND branch = ?" if branch else ""
    p = (cutoff,) + ((branch,) if branch else ())

    # NPL by branch (last X days) — основание для алертов уровня филиала
    rows = db.execute(f"""
        SELECT branch, COUNT(*) as total,
            SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90 THEN 1 ELSE 0 END) as npl
        FROM contracts WHERE contract_date >= ? {w}
        GROUP BY branch
    """, p).fetchall()

    for r in rows:
        rate = r['npl'] / r['total'] * 100 if r['total'] > 0 else 0
        if rate >= ALERT_NPL_BRANCH_CRITICAL_PCT:
            alerts.append({
                "kind": "npl_branch",
                "severity": "critical",
                "icon": "📉",
                "title": f"Критичный НПЛ по филиалу {r['branch']}",
                "message": f"НПЛ {rate:.1f}% ({r['npl']}/{r['total']} договоров) за последние {days} дн.",
                "details": {
                    "branch": r["branch"],
                    "npl_rate": rate,
                    "npl_count": r["npl"],
                    "contracts": r["total"],
                    "period_days": days,
                },
            })
        elif rate >= ALERT_NPL_BRANCH_WARNING_PCT:
            alerts.append({
                "kind": "npl_branch",
                "severity": "warning",
                "icon": "📉",
                "title": f"Высокий НПЛ по филиалу {r['branch']}",
                "message": f"НПЛ {rate:.1f}% ({r['npl']}/{r['total']} договоров) за последние {days} дн.",
                "details": {
                    "branch": r["branch"],
                    "npl_rate": rate,
                    "npl_count": r["npl"],
                    "contracts": r["total"],
                    "period_days": days,
                },
            })

    # Risky employees (last 12 months)
    emp_rows = db.execute(f"""
        SELECT responsible_person, branch, COUNT(*) as total,
            SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90 THEN 1 ELSE 0 END) as npl
        FROM contracts WHERE contract_date >= ? {w}
        AND responsible_person IS NOT NULL AND responsible_person != ''
        GROUP BY responsible_person, branch HAVING total >= ? AND npl * 100.0 / total > ?
    """, p + (ALERT_EMPLOYEE_MIN_CONTRACTS, ALERT_RISKY_EMPLOYEE_NPL_PCT)).fetchall()

    for e in emp_rows:
        rate = e['npl'] / e['total'] * 100
        alerts.append({
            "kind": "risky_employee",
            "severity": "warning",
            "icon": "👤",
            "title": f"Рискованный сотрудник: {e['responsible_person']}",
            "message": f"НПЛ {rate:.1f}% ({e['npl']}/{e['total']}) в филиале {e['branch']} за последние {days} дн.",
            "details": {
                "employee": e["responsible_person"],
                "branch": e["branch"],
                "npl_rate": rate,
                "npl_count": e["npl"],
                "contracts": e["total"],
                "period_days": days,
            },
        })

    # Low collection
    for r in rows:
        total_paid = db.execute(f"SELECT SUM(paid_amount) as p, SUM(product_amount) as t FROM contracts WHERE branch=? AND contract_date >= ?",
            (r['branch'], cutoff)).fetchone()
        if total_paid and total_paid['t'] and total_paid['t'] > 0:
            coll = total_paid['p'] / total_paid['t'] * 100
            if coll < ALERT_LOW_COLLECTION_PCT and r['total'] > ALERT_LOW_COLLECTION_MIN_CONTRACTS:
                alerts.append({
                    "kind": "low_collection",
                    "severity": "critical",
                    "icon": "💰",
                    "title": f"Низкий сбор по филиалу {r['branch']}",
                    "message": f"Сбор {coll:.1f}% при {r['total']} договорах за последние {days} дн.",
                    "details": {
                        "branch": r["branch"],
                        "collection_rate": coll,
                        "contracts": r["total"],
                        "period_days": days,
                    },
                })

    alerts.sort(key=lambda x: {"critical": 0, "warning": 1, "info": 2}.get(x['severity'], 9))
    db.close()
    return alerts
