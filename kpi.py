"""
CCC — Unified KPI Engine
Single source of truth for all portfolio, NPL, collection, and employee metrics.
Used by routes, dashboards, analytics, and decision center.

NPL definition (canonical):
  status_detail IN ('Ёмон', 'МИБ', 'Судда') OR total_late_days >= 90
"""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import json
import os
import time
from core.database import get_db
from config.settings import NPL_STATUSES, BRANCHES


NPL_CONDITION = "status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90"
NPL_CASE = f"CASE WHEN {NPL_CONDITION} THEN 1 ELSE 0 END"


def _dbg745_kpi(hypothesis_id: str, location: str, message: str, data: Dict[str, Any]) -> None:
    # #region agent log
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        log_path = os.path.join(root, "debug-74510d.log")
        payload = {
            "sessionId": "74510d",
            "runId": "pre-fix",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
    # #endregion


def get_portfolio_kpi(branch: str | None = None) -> Dict[str, Any]:
    """
    Core portfolio KPI block: total portfolio, debt, paid, NPL, DPD buckets,
    current month issuance/collection. Used by executive dashboard, analytics,
    and KPI cards across the platform.
    """
    db = get_db()
    flt = "AND branch = ?" if branch else ""
    params = (branch,) if branch else ()

    row = db.execute(f"""
        SELECT
            COUNT(*)                                                    AS total_contracts,
            COUNT(DISTINCT client_id)                                   AS total_clients,
            COALESCE(SUM(product_amount), 0)                            AS total_portfolio,
            COALESCE(SUM(debt_amount), 0)                               AS remaining_debt,
            COALESCE(SUM(paid_amount), 0)                               AS total_payments,
            SUM(CASE WHEN total_late_days >= 1  AND total_late_days < 30  THEN 1 ELSE 0 END) AS dpd_1_29,
            SUM(CASE WHEN total_late_days >= 30 AND total_late_days < 60  THEN 1 ELSE 0 END) AS dpd_30_59,
            SUM(CASE WHEN total_late_days >= 60 AND total_late_days < 90  THEN 1 ELSE 0 END) AS dpd_60_89,
            SUM(CASE WHEN total_late_days >= 90 AND total_late_days < 180 THEN 1 ELSE 0 END) AS dpd_90_179,
            SUM(CASE WHEN total_late_days >= 180                          THEN 1 ELSE 0 END) AS dpd_180_plus,
            SUM(CASE WHEN total_late_days >= 30  THEN 1 ELSE 0 END) AS npl_30,
            SUM(CASE WHEN total_late_days >= 60  THEN 1 ELSE 0 END) AS npl_60,
            SUM(CASE WHEN total_late_days >= 90  THEN 1 ELSE 0 END) AS npl_90,
            SUM(CASE WHEN total_late_days >= 180 THEN 1 ELSE 0 END) AS npl_180,
            SUM({NPL_CASE})                                             AS npl_count,
            AVG(product_amount)                                         AS avg_loan_amount
        FROM contracts WHERE 1=1 {flt}
    """, params).fetchone()

    cur_month = datetime.now().strftime("%Y-%m")
    month_row = db.execute(f"""
        SELECT
            COALESCE(SUM(product_amount), 0) AS issued_this_month,
            COALESCE(SUM(paid_amount), 0)    AS collected_this_month,
            COUNT(*)                          AS contracts_this_month
        FROM contracts WHERE strftime('%%Y-%%m', contract_date) = ? {flt}
    """, (cur_month, *params) if branch else (cur_month,)).fetchone()

    db.close()

    tc = row["total_contracts"] or 0
    portfolio = row["total_portfolio"] or 0
    npl_count = row["npl_count"] or 0

    return {
        "total_contracts": tc,
        "total_clients": row["total_clients"] or 0,
        "total_portfolio": round(portfolio, 2),
        "total_portfolio_mln": round(portfolio / 1e6, 1),
        "remaining_debt": round(row["remaining_debt"] or 0, 2),
        "total_payments": round(row["total_payments"] or 0, 2),
        "collection_rate_pct": round(row["total_payments"] / portfolio * 100, 1) if portfolio else 0,
        "npl_count": npl_count,
        "npl_rate_pct": round(npl_count / tc * 100, 2) if tc else 0,
        "dpd_buckets": {
            "1_29": row["dpd_1_29"] or 0,
            "30_59": row["dpd_30_59"] or 0,
            "60_89": row["dpd_60_89"] or 0,
            "90_179": row["dpd_90_179"] or 0,
            "180_plus": row["dpd_180_plus"] or 0,
        },
        "npl_30": row["npl_30"] or 0,
        "npl_60": row["npl_60"] or 0,
        "npl_90": row["npl_90"] or 0,
        "npl_180": row["npl_180"] or 0,
        "dpd_30_pct": round((row["npl_30"] or 0) / tc * 100, 2) if tc else 0,
        "dpd_60_pct": round((row["npl_60"] or 0) / tc * 100, 2) if tc else 0,
        "dpd_90_pct": round((row["npl_90"] or 0) / tc * 100, 2) if tc else 0,
        "avg_loan_amount": round(row["avg_loan_amount"] or 0, 2),
        "issued_this_month": round(month_row["issued_this_month"] or 0, 2),
        "collected_this_month": round(month_row["collected_this_month"] or 0, 2),
        "contracts_this_month": month_row["contracts_this_month"] or 0,
    }


def get_npl_by_branch(year: str | None = None, product_type: str | None = None) -> List[Dict[str, Any]]:
    """NPL breakdown by branch with unified NPL definition."""
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
    rows = db.execute(f"""
        SELECT
            branch,
            COUNT(*)                     AS total,
            SUM({NPL_CASE})              AS npl_count,
            SUM(debt_amount)             AS debt,
            SUM(paid_amount)             AS paid,
            SUM(product_amount)          AS portfolio
        FROM contracts WHERE {w}
        GROUP BY branch ORDER BY branch
    """, params).fetchall()
    db.close()
    result = []
    for r in rows:
        d = dict(r)
        total = d["total"] or 0
        portfolio = d["portfolio"] or 0
        d["npl_rate"] = round(d["npl_count"] / total * 100, 2) if total else 0
        d["collection_rate"] = round(d["paid"] / portfolio * 100, 1) if portfolio else 0
        d["risk_level"] = "critical" if d["npl_rate"] >= 15 else "warning" if d["npl_rate"] >= 8 else "ok"
        result.append(d)
    return result


def get_aging_table(branch: str | None = None) -> Dict[str, Any]:
    """
    Aging distribution by DPD buckets.
    Returns total and per-bucket counts with percentages.
    """
    db = get_db()
    flt = "WHERE branch = ?" if branch else ""
    params = (branch,) if branch else ()
    row = db.execute(f"""
        SELECT
            COUNT(*)                                                          AS total,
            SUM(CASE WHEN COALESCE(total_late_days,0) = 0            THEN 1 ELSE 0 END) AS current,
            SUM(CASE WHEN total_late_days BETWEEN 1  AND 30          THEN 1 ELSE 0 END) AS dpd_1_30,
            SUM(CASE WHEN total_late_days BETWEEN 31 AND 60          THEN 1 ELSE 0 END) AS dpd_31_60,
            SUM(CASE WHEN total_late_days BETWEEN 61 AND 90          THEN 1 ELSE 0 END) AS dpd_61_90,
            SUM(CASE WHEN total_late_days BETWEEN 91 AND 180         THEN 1 ELSE 0 END) AS dpd_91_180,
            SUM(CASE WHEN total_late_days BETWEEN 181 AND 365        THEN 1 ELSE 0 END) AS dpd_181_365,
            SUM(CASE WHEN total_late_days > 365                      THEN 1 ELSE 0 END) AS dpd_365_plus
        FROM contracts {flt}
    """, params).fetchone()
    db.close()

    total = row["total"] or 1
    buckets = [
        {"label": "Текущие (0 дн)", "count": row["current"] or 0},
        {"label": "1–30 дн", "count": row["dpd_1_30"] or 0},
        {"label": "31–60 дн", "count": row["dpd_31_60"] or 0},
        {"label": "61–90 дн", "count": row["dpd_61_90"] or 0},
        {"label": "91–180 дн", "count": row["dpd_91_180"] or 0},
        {"label": "181–365 дн", "count": row["dpd_181_365"] or 0},
        {"label": "365+ дн", "count": row["dpd_365_plus"] or 0},
    ]
    for b in buckets:
        b["pct"] = round(b["count"] / total * 100, 1)
    return {"total": total, "buckets": buckets}


def get_npl_trend(months: int = 12) -> List[Dict[str, Any]]:
    """Monthly NPL trend from portfolio_snapshots or live calculation."""
    db = get_db()
    try:
        rows = db.execute("""
            SELECT snapshot_date, npl_rate, collection_rate, portfolio_amount,
                   npl_count, total_contracts
            FROM portfolio_snapshots
            WHERE branch = 'ALL'
            ORDER BY snapshot_date DESC LIMIT ?
        """, (months,)).fetchall()
        if rows:
            db.close()
            return [dict(r) for r in reversed(list(rows))]
    except Exception:
        pass

    rows = db.execute(f"""
        SELECT
            strftime('%Y-%m', contract_date) AS month,
            COUNT(*)                          AS total,
            SUM({NPL_CASE})                   AS npl_count,
            ROUND(100.0 * SUM({NPL_CASE}) / NULLIF(COUNT(*), 0), 2) AS npl_rate,
            COALESCE(SUM(product_amount), 0)  AS portfolio
        FROM contracts
        WHERE contract_date IS NOT NULL
        GROUP BY month ORDER BY month DESC LIMIT ?
    """, (months,)).fetchall()
    db.close()
    return [dict(r) for r in reversed(list(rows))]


def get_employee_performance(branch: str | None = None, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Employee performance metrics: portfolio, NPL, collection rate, avg loan.
    Sorted by NPL rate descending (riskiest first).
    """
    db = get_db()
    flt = "WHERE ct.branch = ?" if branch else ""
    params = (branch,) if branch else ()
    rows = db.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(ct.responsible_person), ''), 'Не указан') AS employee,
            ct.branch,
            COUNT(*)                                    AS total_contracts,
            COUNT(DISTINCT ct.client_id)                AS total_clients,
            COALESCE(SUM(ct.product_amount), 0)         AS portfolio,
            COALESCE(SUM(ct.debt_amount), 0)            AS debt,
            COALESCE(SUM(ct.paid_amount), 0)            AS paid,
            SUM({NPL_CASE})                              AS npl_count,
            AVG(ct.product_amount)                       AS avg_loan
        FROM contracts ct
        {flt}
        GROUP BY ct.responsible_person, ct.branch
        HAVING COUNT(*) >= 3
        ORDER BY npl_count DESC, portfolio DESC
        LIMIT ?
    """, (*params, limit)).fetchall()
    db.close()

    result = []
    for r in rows:
        d = dict(r)
        tc = d["total_contracts"] or 0
        portfolio = d["portfolio"] or 0
        d["npl_rate"] = round(d["npl_count"] / tc * 100, 2) if tc else 0
        d["collection_rate"] = round(d["paid"] / portfolio * 100, 1) if portfolio else 0
        d["avg_loan"] = round(d["avg_loan"] or 0, 0)
        d["risk_level"] = "critical" if d["npl_rate"] >= 20 else "warning" if d["npl_rate"] >= 10 else "ok"
        result.append(d)
    return result


def get_client_segments() -> List[Dict[str, Any]]:
    """
    Risk matrix by client age segments.
    Returns: segment label, count, NPL count, NPL rate.
    """
    db = get_db()
    rows = db.execute(f"""
        SELECT
            CASE
                WHEN c.age BETWEEN 18 AND 25 THEN '18–25'
                WHEN c.age BETWEEN 26 AND 35 THEN '26–35'
                WHEN c.age BETWEEN 36 AND 45 THEN '36–45'
                WHEN c.age BETWEEN 46 AND 55 THEN '46–55'
                WHEN c.age BETWEEN 56 AND 65 THEN '56–65'
                WHEN c.age > 65              THEN '65+'
                ELSE 'Не указан'
            END AS segment,
            COUNT(*)                            AS total,
            SUM({NPL_CASE})                      AS npl_count,
            COALESCE(SUM(ct.product_amount), 0) AS portfolio,
            COALESCE(SUM(ct.debt_amount), 0)    AS debt
        FROM contracts ct
        JOIN clients c ON c.id = ct.client_id
        GROUP BY segment
        ORDER BY MIN(c.age)
    """).fetchall()
    db.close()

    result = []
    for r in rows:
        d = dict(r)
        tc = d["total"] or 0
        d["npl_rate"] = round(d["npl_count"] / tc * 100, 2) if tc else 0
        d["risk_level"] = "critical" if d["npl_rate"] >= 15 else "warning" if d["npl_rate"] >= 8 else "ok"
        result.append(d)
    return result


def get_income_segments() -> List[Dict[str, Any]]:
    """Risk breakdown by income source."""
    db = get_db()
    rows = db.execute(f"""
        SELECT
            COALESCE(NULLIF(c.income_source, ''), 'Не указан') AS segment,
            COUNT(*)                            AS total,
            SUM({NPL_CASE})                      AS npl_count,
            COALESCE(SUM(ct.product_amount), 0) AS portfolio
        FROM contracts ct
        JOIN clients c ON c.id = ct.client_id
        GROUP BY segment
        HAVING COUNT(*) >= 5
        ORDER BY npl_count DESC
    """).fetchall()
    db.close()

    result = []
    for r in rows:
        d = dict(r)
        tc = d["total"] or 0
        d["npl_rate"] = round(d["npl_count"] / tc * 100, 2) if tc else 0
        result.append(d)
    return result


def get_product_segments() -> List[Dict[str, Any]]:
    """Risk breakdown by product type."""
    db = get_db()
    rows = db.execute(f"""
        SELECT
            COALESCE(NULLIF(product_type, ''), 'Не указан') AS segment,
            COUNT(*)                        AS total,
            SUM({NPL_CASE})                  AS npl_count,
            COALESCE(SUM(product_amount), 0) AS portfolio,
            AVG(product_amount)              AS avg_amount
        FROM contracts
        GROUP BY segment
        HAVING COUNT(*) >= 3
        ORDER BY npl_count DESC
    """).fetchall()
    db.close()

    result = []
    for r in rows:
        d = dict(r)
        tc = d["total"] or 0
        d["npl_rate"] = round(d["npl_count"] / tc * 100, 2) if tc else 0
        d["avg_amount"] = round(d["avg_amount"] or 0, 0)
        result.append(d)
    return result


def generate_recommendations(branch: str | None = None) -> List[Dict[str, Any]]:
    """
    Decision center: generate actionable recommendations based on current data.
    Each recommendation has: type (critical/warning/info), area, message, action_url.
    """
    recommendations: List[Dict[str, Any]] = []

    branches = get_npl_by_branch()
    for b in branches:
        if b["npl_rate"] >= 20:
            recommendations.append({
                "priority": "critical",
                "area": f"Филиал {b['branch']}",
                "message": f"NPL {b['npl_rate']}% — критический уровень. Рекомендуется приостановить выдачи и провести ревизию портфеля.",
                "action": "restrict_issuance",
                "action_url": f"/portfolio?branch={b['branch']}",
            })
        elif b["npl_rate"] >= 12:
            recommendations.append({
                "priority": "warning",
                "area": f"Филиал {b['branch']}",
                "message": f"NPL {b['npl_rate']}% — повышенный уровень. Усилить мониторинг и ужесточить скоринг.",
                "action": "tighten_scoring",
                "action_url": f"/portfolio?branch={b['branch']}",
            })
        if b.get("collection_rate", 100) < 50 and b["total"] >= 50:
            recommendations.append({
                "priority": "warning",
                "area": f"Филиал {b['branch']}",
                "message": f"Сбор {b['collection_rate']}% — низкий уровень. Усилить коллекшн.",
                "action": "improve_collection",
                "action_url": f"/portfolio?branch={b['branch']}",
            })

    employees = get_employee_performance(branch=branch, limit=100)
    for emp in employees:
        if emp["npl_rate"] >= 25 and emp["total_contracts"] >= 10:
            recommendations.append({
                "priority": "critical",
                "area": f"Сотрудник: {emp['employee']}",
                "message": f"NPL {emp['npl_rate']}% при {emp['total_contracts']} договорах. Пересмотреть лимиты или приостановить выдачи.",
                "action": "restrict_employee",
                "action_url": "/employees",
            })

    segments = get_client_segments()
    for seg in segments:
        if seg["npl_rate"] >= 20 and seg["total"] >= 20:
            recommendations.append({
                "priority": "warning",
                "area": f"Сегмент: {seg['segment']} лет",
                "message": f"NPL {seg['npl_rate']}% в возрастной группе. Пересмотреть лимиты для сегмента.",
                "action": "review_segment_limits",
                "action_url": "/risk-intelligence",
            })

    for b in branches:
        if b["npl_rate"] < 5 and b["total"] >= 100 and b.get("collection_rate", 0) > 80:
            recommendations.append({
                "priority": "info",
                "area": f"Филиал {b['branch']}",
                "message": f"NPL {b['npl_rate']}%, сбор {b['collection_rate']}%. Успешная модель — рассмотреть масштабирование.",
                "action": "scale_branch",
                "action_url": f"/portfolio?branch={b['branch']}",
            })

    recommendations.sort(key=lambda x: {"critical": 0, "warning": 1, "info": 2}.get(x["priority"], 3))
    return recommendations


def get_monthly_issuance(year: str, branch: str | None = None) -> List[Dict[str, Any]]:
    """Monthly breakdown within a year: contracts, portfolio, NPL, collection."""
    db = get_db()
    flt = " AND branch = ?" if branch else ""
    params = [year] + ([branch] if branch else [])
    rows = db.execute(f"""
        SELECT
            strftime('%m', contract_date)                        AS month,
            COUNT(*)                                             AS total_contracts,
            COALESCE(SUM(product_amount), 0) / 1e6              AS portfolio_mln,
            SUM({NPL_CASE})                                      AS npl_count,
            ROUND(100.0 * SUM({NPL_CASE}) / NULLIF(COUNT(*), 0), 1) AS npl_rate,
            CASE WHEN COALESCE(SUM(product_amount), 0) > 0
                 THEN ROUND(100.0 * COALESCE(SUM(paid_amount), 0) / SUM(product_amount), 1)
                 ELSE 0 END                                      AS collection_rate
        FROM contracts
        WHERE strftime('%Y', contract_date) = ?{flt}
        GROUP BY month ORDER BY month
    """, tuple(params)).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ═══ ТЗ 11: Портфель — сегментация, слабые места филиалов ─══

def _build_debt_filter_sql(bands: List[str], params: List[Any], col: str = "debt_amount") -> str:
    """
    bands: список label из DEBT_BANDS.
    Возвращает SQL-фрагмент AND (cond1 OR cond2 OR ...) и дополняет params.
    col: имя колонки (debt_amount или ct.debt_amount).
    """
    if not bands:
        return ""
    from config.settings import DEBT_BANDS
    band_map = {b[0]: (b[1], b[2]) for b in DEBT_BANDS}
    conds = []
    for label in bands:
        if label not in band_map:
            continue
        mn, mx = band_map[label]
        if mx is None:
            conds.append(f"({col} >= ?)")
            params.append(mn)
        else:
            conds.append(f"({col} BETWEEN ? AND ?)")
            params.append(mn)
            params.append(mx)
    if not conds:
        return ""
    return " AND (" + " OR ".join(conds) + ")"


def get_portfolio_segments(
    branch: str | None = None,
    year: str | None = None,
    product_type: str | None = None,
    debt_bands: List[str] | None = None,
) -> Dict[str, Any]:
    """
    ТЗ 11: Продукт × Сегмент клиента и Регион × МФЙ.
    threshold — минимальный порог из выбранных бэндов (для «Долг ≥ X»).
    """
    from config.settings import DEBT_BANDS
    db = get_db()
    where = ["1=1"]
    params: List[Any] = []
    if branch:
        where.append("ct.branch = ?")
        params.append(branch)
    if year:
        where.append("strftime('%Y', ct.contract_date) = ?")
        params.append(year)
    if product_type:
        where.append("(COALESCE(NULLIF(TRIM(ct.product_type),''),'Не указан') = ?)")
        params.append(product_type)
    debt_sql = _build_debt_filter_sql(debt_bands or [], params, col="ct.debt_amount")
    w = " AND ".join(where) + debt_sql
    _dbg745_kpi(
        "H-portfolio-filter-where",
        "core/kpi.py:get_portfolio_segments",
        "segments_filters",
        {
            "branch": branch,
            "year": year,
            "product_type": product_type,
            "debt_bands": debt_bands or [],
            "where_sql": w,
            "params_count": len(params),
        },
    )

    # Порог X: минимальный min из выбранных бэндов
    threshold = 4_000_000
    if debt_bands:
        band_map = {b[0]: b[1] for b in DEBT_BANDS}
        mins = [band_map.get(lbl, 999999999) for lbl in debt_bands if lbl in band_map]
        if mins:
            threshold = min(mins)

    # Продукт × Сегмент клиента
    params_ps = [threshold] + list(params)
    rows_ps = db.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(ct.product_type), ''), 'Не указан') AS product_type,
            CASE
                WHEN c.age BETWEEN 18 AND 25 THEN '18–25'
                WHEN c.age BETWEEN 26 AND 35 THEN '26–35'
                WHEN c.age BETWEEN 36 AND 45 THEN '36–45'
                WHEN c.age BETWEEN 46 AND 55 THEN '46–55'
                WHEN c.age >= 56 OR c.age IS NULL THEN '55+'
                ELSE 'Не указан'
            END AS client_segment,
            COUNT(DISTINCT ct.client_id) AS clients,
            COUNT(*) AS contracts,
            COALESCE(SUM(ct.product_amount), 0) AS portfolio,
            COALESCE(SUM(ct.debt_amount), 0) AS debt_total,
            COALESCE(SUM(CASE WHEN ct.debt_amount >= ? THEN ct.debt_amount ELSE 0 END), 0) AS debt_above_x,
            ROUND(100.0 * SUM({NPL_CASE}) / NULLIF(COUNT(*), 0), 2) AS npl_pct
        FROM contracts ct
        JOIN clients c ON c.id = ct.client_id
        WHERE {w}
        GROUP BY product_type, client_segment
        HAVING COUNT(*) >= 1
        ORDER BY product_type, client_segment
    """, params_ps).fetchall()

    products_segments = []
    for r in rows_ps:
        d = dict(r)
        debt_tot = d.get("debt_total") or 0
        debt_ax = d.get("debt_above_x") or 0
        d["debt_above_x_pct"] = round(100 * debt_ax / debt_tot, 1) if debt_tot else 0
        products_segments.append(d)

    # Регион × МФЙ
    params_rm = [threshold] + list(params)
    rows_rm = db.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(c.region), ''), 'Не указан') AS region,
            COALESCE(NULLIF(TRIM(c.mfy), ''), 'Не указан') AS mfy,
            COUNT(DISTINCT ct.client_id) AS clients,
            COUNT(*) AS contracts,
            COALESCE(SUM(ct.debt_amount), 0) AS debt_total,
            COALESCE(SUM(CASE WHEN ct.debt_amount >= ? THEN ct.debt_amount ELSE 0 END), 0) AS debt_above_x
        FROM contracts ct
        JOIN clients c ON c.id = ct.client_id
        WHERE {w}
        GROUP BY region, mfy
        HAVING COUNT(*) >= 1
        ORDER BY debt_total DESC
    """, params_rm).fetchall()

    regions_mfy = []
    for r in rows_rm:
        d = dict(r)
        d["avg_debt_above_x"] = round((d.get("debt_above_x") or 0) / max(1, d.get("clients") or 1), 0)
        regions_mfy.append(d)

    missing_client_link = db.execute(
        f"SELECT COUNT(*) AS cnt FROM contracts ct LEFT JOIN clients c ON c.id=ct.client_id WHERE {w} AND c.id IS NULL",
        params,
    ).fetchone()
    _dbg745_kpi(
        "H-portfolio-empty-join",
        "core/kpi.py:get_portfolio_segments",
        "segments_result",
        {
            "products_segments": len(products_segments),
            "regions_mfy": len(regions_mfy),
            "missing_client_link": int((missing_client_link or {}).get("cnt", 0) if isinstance(missing_client_link, dict) else (missing_client_link["cnt"] if missing_client_link else 0)),
            "threshold": threshold,
        },
    )

    db.close()
    return {
        "threshold": threshold,
        "products_segments": products_segments,
        "regions_mfy": regions_mfy,
    }


def get_branch_weaknesses(
    date_from: str | None = None,
    date_to: str | None = None,
    profit_margin_pct: float = 20,
) -> List[Dict[str, Any]]:
    """
    ТЗ 11: Слабые места филиалов — портфель, просрочка, риск %, утечка прибыли.
    """
    from config.settings import PROFIT_MARGIN_PCT
    margin = profit_margin_pct or PROFIT_MARGIN_PCT
    db = get_db()
    date_flt = ""
    dp = []
    if date_from:
        date_flt += " AND contract_date >= ?"
        dp.append(date_from)
    if date_to:
        date_flt += " AND contract_date <= ?"
        dp.append(date_to)

    rows = db.execute(f"""
        SELECT
            branch,
            COALESCE(SUM(product_amount), 0) AS portfolio,
            COALESCE(SUM(CASE WHEN total_late_days > 0 THEN debt_amount ELSE 0 END), 0) AS overdue_debt,
            ROUND(100.0 * SUM({NPL_CASE}) / NULLIF(COUNT(*), 0), 2) AS risk_pct
        FROM contracts
        WHERE 1=1 {date_flt}
        GROUP BY branch
        ORDER BY branch
    """, dp).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        overdue = d.get("overdue_debt") or 0
        d["profit_leakage"] = round(overdue * margin / 100, 0)
        result.append(d)
    db.close()
    return result


def get_branch_drill_detail(
    branch: str,
    threshold: int = 4_000_000,
    date_from: str | None = None,
    date_to: str | None = None,
) -> Dict[str, Any]:
    """
    ТЗ 11: Детальный срез по филиалу — пол, возраст, регион/МФЙ, тип дохода.
    """
    db = get_db()
    date_flt = ""
    dp = []
    if date_from:
        date_flt += " AND ct.contract_date >= ?"
        dp.append(date_from)
    if date_to:
        date_flt += " AND ct.contract_date <= ?"
        dp.append(date_to)

    base_params = [threshold, branch] + dp

    # По полу
    by_gender = db.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(c.gender), ''), 'Не указан') AS gender,
            COUNT(DISTINCT ct.client_id) AS clients,
            COUNT(*) AS contracts,
            COALESCE(SUM(ct.debt_amount), 0) AS debt_total,
            COALESCE(SUM(CASE WHEN ct.debt_amount >= ? THEN ct.debt_amount ELSE 0 END), 0) AS debt_above_x,
            ROUND(100.0 * SUM({NPL_CASE}) / NULLIF(COUNT(*), 0), 2) AS npl_pct
        FROM contracts ct
        JOIN clients c ON c.id = ct.client_id
        WHERE ct.branch = ? {date_flt}
        GROUP BY gender
    """, base_params).fetchall()

    # По возрасту
    by_age = db.execute(f"""
        SELECT
            CASE
                WHEN c.age BETWEEN 18 AND 25 THEN '18–25'
                WHEN c.age BETWEEN 26 AND 35 THEN '26–35'
                WHEN c.age BETWEEN 36 AND 45 THEN '36–45'
                WHEN c.age BETWEEN 46 AND 55 THEN '46–55'
                WHEN c.age >= 56 OR c.age IS NULL THEN '55+'
                ELSE 'Не указан'
            END AS age_bucket,
            COUNT(DISTINCT ct.client_id) AS clients,
            COUNT(*) AS contracts,
            COALESCE(SUM(ct.debt_amount), 0) AS debt_total,
            COALESCE(SUM(CASE WHEN ct.debt_amount >= ? THEN ct.debt_amount ELSE 0 END), 0) AS debt_above_x,
            ROUND(100.0 * SUM({NPL_CASE}) / NULLIF(COUNT(*), 0), 2) AS npl_pct
        FROM contracts ct
        JOIN clients c ON c.id = ct.client_id
        WHERE ct.branch = ? {date_flt}
        GROUP BY age_bucket
    """, base_params).fetchall()

    # По региону / МФЙ
    by_region_mfy = db.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(c.region), ''), 'Не указан') AS region,
            COALESCE(NULLIF(TRIM(c.mfy), ''), 'Не указан') AS mfy,
            COUNT(DISTINCT ct.client_id) AS clients,
            COALESCE(SUM(ct.debt_amount), 0) AS debt_total,
            COALESCE(SUM(CASE WHEN ct.debt_amount >= ? THEN ct.debt_amount ELSE 0 END), 0) AS debt_above_x,
            ROUND(100.0 * SUM({NPL_CASE}) / NULLIF(COUNT(*), 0), 2) AS npl_pct
        FROM contracts ct
        JOIN clients c ON c.id = ct.client_id
        WHERE ct.branch = ? {date_flt}
        GROUP BY region, mfy
        ORDER BY debt_total DESC
    """, base_params).fetchall()

    # По типу дохода
    by_income = db.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(c.income_source), ''), 'Не указан') AS income_source,
            COUNT(DISTINCT ct.client_id) AS clients,
            COALESCE(SUM(ct.debt_amount), 0) AS debt_total,
            ROUND(100.0 * SUM({NPL_CASE}) / NULLIF(COUNT(*), 0), 2) AS npl_pct
        FROM contracts ct
        JOIN clients c ON c.id = ct.client_id
        WHERE ct.branch = ? {date_flt}
        GROUP BY income_source
        ORDER BY debt_total DESC
    """, [branch] + dp).fetchall()

    db.close()
    return {
        "by_gender": [dict(r) for r in by_gender],
        "by_age": [dict(r) for r in by_age],
        "by_region_mfy": [dict(r) for r in by_region_mfy],
        "by_income": [dict(r) for r in by_income],
    }


def generate_branch_recommendations(branch: str, drill: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    ТЗ 11: Rule-based выводы для выбранного филиала.
    """
    recs = []
    avg_npl_network = 0
    try:
        branches = get_npl_by_branch()
        total_npl = sum(b.get("npl_count", 0) or 0 for b in branches)
        total_cnt = sum(b.get("total", 0) or 0 for b in branches)
        avg_npl_network = round(100 * total_npl / total_cnt, 1) if total_cnt else 0
    except Exception:
        pass

    # Анализ по возрасту и типу дохода
    by_age = drill.get("by_age", [])
    by_income = drill.get("by_income", [])
    by_region = drill.get("by_region_mfy", [])

    for a in by_age:
        if (a.get("npl_pct") or 0) > avg_npl_network + 10 and (a.get("contracts") or 0) >= 10:
            recs.append({
                "level": "critical",
                "text": f"В возрасте {a.get('age_bucket', '')} NPL {a.get('npl_pct')}% — выше среднего по сети на {round((a.get('npl_pct') or 0) - avg_npl_network, 1)} п.п.",
            })
    for inc in by_income:
        if (inc.get("npl_pct") or 0) >= 25 and (inc.get("clients") or 0) >= 5:
            recs.append({
                "level": "warning",
                "text": f"Тип дохода «{inc.get('income_source', '')}»: NPL {inc.get('npl_pct')}% — усилить проверки.",
            })
    for r in by_region:
        if (r.get("npl_pct") or 0) >= 30 and (r.get("clients") or 0) >= 5:
            recs.append({
                "level": "warning",
                "text": f"Регион {r.get('region', '')}, МФЙ {r.get('mfy', '')}: NPL {r.get('npl_pct')}% — проблемная зона.",
            })

    if not recs:
        recs.append({"level": "info", "text": "Критических отклонений по сегментам не выявлено."})
    return recs
