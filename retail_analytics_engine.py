"""
CCC Retail Analytics Engine (TZ 12)
Product/category analytics for sales, margin, assortment, supplier proxy.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from core.database import get_db


def _table_columns(db, table: str) -> set[str]:
    try:
        rows = db.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r["name"]) for r in rows}
    except Exception:
        return set()


def _build_filters(
    branch: str | None,
    year: str | None,
    month: str | None,
    category: str | None,
    supplier: str | None,
    has_product_category: bool,
    has_supplier: bool,
) -> Tuple[str, List[Any], str, str]:
    where = ["1=1"]
    params: List[Any] = []
    category_expr = (
        "COALESCE(NULLIF(TRIM(product_category),''), COALESCE(NULLIF(TRIM(product_type),''),'Не указан'))"
        if has_product_category
        else "COALESCE(NULLIF(TRIM(product_type),''),'Не указан')"
    )
    supplier_expr = (
        "COALESCE(NULLIF(TRIM(supplier),''),'N/A')" if has_supplier else "'N/A'"
    )
    if branch:
        where.append("branch = ?")
        params.append(branch)
    if year:
        where.append("strftime('%Y', contract_date) = ?")
        params.append(year)
    if month:
        where.append("strftime('%m', contract_date) = ?")
        params.append(month.zfill(2))
    if category:
        where.append(f"{category_expr} = ?")
        params.append(category)
    if supplier:
        where.append(f"{supplier_expr} = ?")
        params.append(supplier)
    return " AND ".join(where), params, category_expr, supplier_expr


def _calc_row_finance(row: Dict[str, Any]) -> Dict[str, Any]:
    sales = float(row.get("sales_volume") or 0)
    revenue = float(row.get("revenue") or 0)
    margin = float(row.get("margin") or 0)
    row["margin_pct"] = round((margin / sales * 100.0), 2) if sales > 0 else 0.0
    row["revenue"] = round(revenue, 2)
    row["margin"] = round(margin, 2)
    row["sales_volume"] = round(sales, 2)
    return row


def get_product_performance(filters: Dict[str, Any]) -> Dict[str, Any]:
    db = get_db()
    cols = _table_columns(db, "contracts")
    has_product_category = "product_category" in cols
    has_supplier = "supplier" in cols
    where_sql, params, category_expr, supplier_expr = _build_filters(
        filters.get("branch"),
        filters.get("year"),
        filters.get("month"),
        filters.get("category"),
        filters.get("supplier"),
        has_product_category,
        has_supplier,
    )
    rows = db.execute(
        f"""
        SELECT
            COALESCE(NULLIF(TRIM(product_type),''),'Не указан') AS product,
            {category_expr} AS category,
            branch,
            strftime('%Y', contract_date) AS year,
            strftime('%m', contract_date) AS month,
            {supplier_expr} AS supplier,
            COUNT(*) AS contract_count,
            COALESCE(SUM(product_amount),0) AS sales_volume,
            COALESCE(SUM(CASE WHEN paid_amount > advance_payment THEN paid_amount - advance_payment ELSE 0 END),0) AS revenue,
            COALESCE(SUM(CASE WHEN paid_amount > advance_payment THEN paid_amount - advance_payment ELSE 0 END),0) AS margin
        FROM contracts
        WHERE {where_sql}
        GROUP BY product, category, branch, year, month, supplier
        ORDER BY revenue DESC
        """
        ,
        params,
    ).fetchall()
    performance = [_calc_row_finance(dict(r)) for r in rows]

    cat_rows = db.execute(
        f"""
        SELECT
            {category_expr} AS category,
            COALESCE(SUM(product_amount),0) AS sales_volume,
            COALESCE(SUM(CASE WHEN paid_amount > advance_payment THEN paid_amount - advance_payment ELSE 0 END),0) AS revenue,
            COALESCE(SUM(CASE WHEN paid_amount > advance_payment THEN paid_amount - advance_payment ELSE 0 END),0) AS margin,
            COUNT(*) AS contract_count
        FROM contracts
        WHERE {where_sql}
        GROUP BY category
        ORDER BY revenue DESC
        """
        ,
        params,
    ).fetchall()
    category_summary = [_calc_row_finance(dict(r)) for r in cat_rows]
    total_revenue = sum(x["revenue"] for x in category_summary) or 1.0
    for x in category_summary:
        x["share_of_revenue_pct"] = round(x["revenue"] * 100.0 / total_revenue, 2)

    trend_rows = db.execute(
        f"""
        SELECT
            strftime('%Y-%m', contract_date) AS period,
            COALESCE(SUM(product_amount),0) AS sales_volume,
            COALESCE(SUM(CASE WHEN paid_amount > advance_payment THEN paid_amount - advance_payment ELSE 0 END),0) AS revenue,
            COALESCE(SUM(CASE WHEN paid_amount > advance_payment THEN paid_amount - advance_payment ELSE 0 END),0) AS margin
        FROM contracts
        WHERE {where_sql}
        GROUP BY period
        ORDER BY period
        """
        ,
        params,
    ).fetchall()
    trend = [_calc_row_finance(dict(r)) for r in trend_rows]
    db.close()
    return {
        "performance": performance,
        "category_summary": category_summary,
        "trend": trend,
        "meta": {"supplier_mode": "real" if has_supplier else "proxy_n_a"},
    }


def get_abc_xyz(filters: Dict[str, Any]) -> Dict[str, Any]:
    perf = get_product_performance(filters)["performance"]
    by_product: Dict[str, Dict[str, Any]] = {}
    for r in perf:
        p = r["product"]
        item = by_product.setdefault(
            p, {"product": p, "revenue": 0.0, "sales_volume": 0.0, "months": []}
        )
        item["revenue"] += float(r.get("revenue") or 0)
        item["sales_volume"] += float(r.get("sales_volume") or 0)
        item["months"].append(float(r.get("sales_volume") or 0))

    rows = sorted(by_product.values(), key=lambda x: x["revenue"], reverse=True)
    total_rev = sum(r["revenue"] for r in rows) or 1.0
    cum = 0.0
    for r in rows:
        cum += r["revenue"]
        cum_pct = (cum / total_rev) * 100.0
        if cum_pct <= 80:
            abc = "A"
        elif cum_pct <= 95:
            abc = "B"
        else:
            abc = "C"
        m = r["months"] or [0.0]
        mean = sum(m) / max(1, len(m))
        if mean <= 0:
            cv = 999.0
        else:
            var = sum((x - mean) ** 2 for x in m) / max(1, len(m))
            cv = (var ** 0.5) / mean
        xyz = "X" if cv <= 0.5 else "Y" if cv <= 1.0 else "Z"
        r.update(
            {
                "cum_revenue_pct": round(cum_pct, 2),
                "abc_class": abc,
                "cv": round(cv, 3),
                "xyz_class": xyz,
            }
        )
        r.pop("months", None)
    return {"items": rows}


def get_growth_strategy(filters: Dict[str, Any]) -> Dict[str, Any]:
    perf = get_product_performance(filters)["performance"]
    by_product: Dict[str, Dict[str, Any]] = {}
    for r in perf:
        p = r["product"]
        item = by_product.setdefault(
            p, {"product": p, "sales_volume": 0.0, "revenue": 0.0, "margin": 0.0, "contracts": 0}
        )
        item["sales_volume"] += float(r.get("sales_volume") or 0)
        item["revenue"] += float(r.get("revenue") or 0)
        item["margin"] += float(r.get("margin") or 0)
        item["contracts"] += int(r.get("contract_count") or 0)
    items = list(by_product.values())
    for x in items:
        sales = x["sales_volume"] or 1.0
        margin_pct = x["margin"] / sales
        x["margin_pct"] = round(margin_pct * 100, 2)
        x["scenario_volume_10_profit"] = round(x["margin"] * 1.10, 2)
        x["scenario_margin_5_profit"] = round(x["margin"] * 1.05, 2)
        x["strategy"] = (
            "Рост объема + кросс-селл" if x["margin_pct"] >= 3 else "Поднять маржу / пересмотр условий"
        )
    items.sort(key=lambda z: z["margin"], reverse=True)
    top = items[:20]
    return {
        "top_products": top,
        "summary": {
            "current_profit": round(sum(i["margin"] for i in top), 2),
            "profit_if_volume_10": round(sum(i["scenario_volume_10_profit"] for i in top), 2),
            "profit_if_margin_5": round(sum(i["scenario_margin_5_profit"] for i in top), 2),
        },
    }


def get_assortment(filters: Dict[str, Any]) -> Dict[str, Any]:
    abcxyz = get_abc_xyz(filters)["items"]
    perf = {r["product"]: r for r in get_growth_strategy(filters)["top_products"]}
    tiers = []
    for r in abcxyz:
        p = r["product"]
        growth = perf.get(p, {})
        revenue = float(growth.get("revenue") or r.get("revenue") or 0)
        loss_10d = round((revenue / 30.0) * 10.0, 2)
        loss_30d = round(revenue, 2)
        if r["abc_class"] == "A" and r["xyz_class"] in ("X", "Y"):
            tier = "Tier 1"
            role = "must_stock"
        elif r["abc_class"] in ("A", "B"):
            tier = "Tier 2"
            role = "optional"
        else:
            tier = "Tier 3"
            role = "remove_candidate"
        tiers.append(
            {
                "product": p,
                "abc_class": r["abc_class"],
                "xyz_class": r["xyz_class"],
                "tier": tier,
                "role": role,
                "loss_10d": loss_10d,
                "loss_30d": loss_30d,
            }
        )
    tiers.sort(key=lambda x: (x["tier"], -x["loss_30d"]))
    return {"items": tiers}


def get_supplier_ranking(filters: Dict[str, Any]) -> Dict[str, Any]:
    db = get_db()
    cols = _table_columns(db, "contracts")
    has_product_category = "product_category" in cols
    has_supplier = "supplier" in cols
    where_sql, params, _, supplier_expr = _build_filters(
        filters.get("branch"),
        filters.get("year"),
        filters.get("month"),
        filters.get("category"),
        filters.get("supplier"),
        has_product_category,
        has_supplier,
    )
    rows = db.execute(
        f"""
        SELECT
            {supplier_expr} AS supplier,
            COUNT(*) AS contracts,
            COALESCE(SUM(product_amount),0) AS sales_volume,
            COALESCE(SUM(CASE WHEN paid_amount > advance_payment THEN paid_amount - advance_payment ELSE 0 END),0) AS revenue,
            COALESCE(SUM(CASE WHEN paid_amount > advance_payment THEN paid_amount - advance_payment ELSE 0 END),0) AS margin
        FROM contracts
        WHERE {where_sql}
        GROUP BY supplier
        ORDER BY margin DESC
        """
        ,
        params,
    ).fetchall()
    items = [_calc_row_finance(dict(r)) for r in rows]
    total_margin = sum(i["margin"] for i in items) or 1.0
    for i in items:
        contrib = i["margin"] * 100.0 / total_margin
        i["contribution_pct"] = round(contrib, 2)
        i["class"] = "A" if contrib >= 20 else "B" if contrib >= 5 else "C"
        i["action"] = (
            "increase_volume" if i["class"] == "A" else "renegotiate" if i["class"] == "B" else "replace_or_reduce"
        )
    db.close()
    return {"items": items, "mode": "supplier" if has_supplier else "proxy_n_a"}
