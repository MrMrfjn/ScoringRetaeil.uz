"""
CCC - Data Quality Engine
Safe deduplication and enrichment for scoring/analytics readiness.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Dict, List

from config.settings import BASE_DIR
from core.database import get_db


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _set_busy_timeout(db: sqlite3.Connection, ms: int = 30000) -> None:
    try:
        db.execute(f"PRAGMA busy_timeout={int(ms)}")
    except Exception:
        pass


def _norm_passport(p: str | None) -> str | None:
    if not p:
        return None
    s = re.sub(r"[^\w]", "", str(p).upper())
    return s or None


def _norm_name(v: str | None) -> str:
    if not v:
        return ""
    s = str(v).lower()
    s = re.sub(r"[^a-zа-яё0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _name_match(a: str | None, b: str | None) -> bool:
    na = _norm_name(a)
    nb = _norm_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta = set(na.split())
    tb = set(nb.split())
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / max(1, len(ta | tb))
    return overlap >= 0.7


def _client_branch_set(db: sqlite3.Connection, client_id: int) -> set[str]:
    rows = db.execute(
        "SELECT DISTINCT branch FROM contracts WHERE client_id=? AND branch IS NOT NULL",
        (client_id,),
    ).fetchall()
    return {r["branch"] for r in rows if r["branch"]}


def _safe_passport_pair(db: sqlite3.Connection, keep: sqlite3.Row, dup: sqlite3.Row) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    b1 = _client_branch_set(db, keep["id"])
    b2 = _client_branch_set(db, dup["id"])
    if not (b1 and b2 and (b1 & b2)):
        reasons.append("branch_mismatch")
    if not _name_match(keep["full_name"], dup["full_name"]):
        reasons.append("name_mismatch")
    g1 = (keep["gender"] or "").strip().lower()
    g2 = (dup["gender"] or "").strip().lower()
    if g1 and g2 and g1 != g2:
        reasons.append("gender_conflict")
    a1 = keep["age"]
    a2 = dup["age"]
    if a1 is not None and a2 is not None and abs(int(a1) - int(a2)) > 2:
        reasons.append("age_conflict")
    return (len(reasons) == 0, reasons)


def get_data_quality_profile() -> Dict[str, object]:
    db = get_db()
    _set_busy_timeout(db)
    try:
        clients_total = db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        contracts_total = db.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
        payments_total = db.execute("SELECT COUNT(*) FROM payments").fetchone()[0] if _table_exists(db, "payments") else 0

        missing_age = db.execute(
            "SELECT COUNT(*) FROM clients WHERE age IS NULL OR age < 18 OR age > 120"
        ).fetchone()[0]
        missing_region = db.execute(
            "SELECT COUNT(*) FROM clients WHERE region IS NULL OR TRIM(region)=''"
        ).fetchone()[0]
        missing_income = db.execute(
            "SELECT COUNT(*) FROM clients WHERE income_source IS NULL OR TRIM(income_source)=''"
        ).fetchone()[0]

        dup_client_external_groups = db.execute(
            "SELECT COUNT(*) FROM (SELECT external_id FROM clients WHERE external_id IS NOT NULL GROUP BY external_id HAVING COUNT(*) > 1)"
        ).fetchone()[0]

        pass_rows = db.execute(
            "SELECT passport FROM clients WHERE passport IS NOT NULL AND TRIM(passport)!=''"
        ).fetchall()
        pass_counts: Dict[str, int] = {}
        for r in pass_rows:
            k = _norm_passport(r[0])
            if not k:
                continue
            pass_counts[k] = pass_counts.get(k, 0) + 1
        dup_client_passport_groups = sum(1 for v in pass_counts.values() if v > 1)

        dup_contract_groups = db.execute(
            "SELECT COUNT(*) FROM (SELECT external_id, branch FROM contracts WHERE external_id IS NOT NULL GROUP BY external_id, branch HAVING COUNT(*) > 1)"
        ).fetchone()[0]

        completeness = {
            "age_fill_rate_pct": round((clients_total - missing_age) * 100.0 / clients_total, 2) if clients_total else 0,
            "region_fill_rate_pct": round((clients_total - missing_region) * 100.0 / clients_total, 2) if clients_total else 0,
            "income_fill_rate_pct": round((clients_total - missing_income) * 100.0 / clients_total, 2) if clients_total else 0,
        }

        return {
            "timestamp": datetime.now().isoformat(),
            "clients_total": clients_total,
            "contracts_total": contracts_total,
            "payments_total": payments_total,
            "missing_age_clients": missing_age,
            "missing_region_clients": missing_region,
            "missing_income_clients": missing_income,
            "duplicate_client_external_groups": dup_client_external_groups,
            "duplicate_client_passport_groups": dup_client_passport_groups,
            "duplicate_contract_external_branch_groups": dup_contract_groups,
            "completeness": completeness,
        }
    finally:
        db.close()


def _merge_clients_by_external_id(db: sqlite3.Connection) -> int:
    db.execute("DROP TABLE IF EXISTS _tmp_client_map")
    db.execute(
        """
        CREATE TEMP TABLE _tmp_client_map AS
        SELECT id AS dup_id,
               MIN(id) OVER (PARTITION BY external_id) AS keep_id
        FROM clients
        WHERE external_id IS NOT NULL
        """
    )
    merged = db.execute(
        "SELECT COUNT(*) FROM _tmp_client_map WHERE dup_id != keep_id"
    ).fetchone()[0]
    if merged == 0:
        db.execute("DROP TABLE IF EXISTS _tmp_client_map")
        return 0

    db.execute(
        """
        UPDATE clients
        SET age = COALESCE(age, (
                SELECT MAX(c2.age) FROM clients c2
                WHERE c2.external_id = clients.external_id AND c2.age BETWEEN 18 AND 120
            )),
            region = COALESCE(NULLIF(region,''), (
                SELECT MAX(NULLIF(c2.region,'')) FROM clients c2
                WHERE c2.external_id = clients.external_id
            )),
            income_source = COALESCE(NULLIF(income_source,''), (
                SELECT MAX(NULLIF(c2.income_source,'')) FROM clients c2
                WHERE c2.external_id = clients.external_id
            )),
            mfy = COALESCE(NULLIF(mfy,''), (
                SELECT MAX(NULLIF(c2.mfy,'')) FROM clients c2
                WHERE c2.external_id = clients.external_id
            )),
            updated_at = ?
        WHERE external_id IS NOT NULL
        """,
        (datetime.now().isoformat(),),
    )

    db.execute(
        """
        UPDATE contracts
        SET client_id = (
            SELECT keep_id FROM _tmp_client_map m WHERE m.dup_id = contracts.client_id
        )
        WHERE client_id IN (SELECT dup_id FROM _tmp_client_map WHERE dup_id != keep_id)
        """
    )
    if _table_exists(db, "payments"):
        db.execute(
            """
            UPDATE payments
            SET client_id = (
                SELECT keep_id FROM _tmp_client_map m WHERE m.dup_id = payments.client_id
            )
            WHERE client_id IN (SELECT dup_id FROM _tmp_client_map WHERE dup_id != keep_id)
            """
        )

    db.execute(
        "DELETE FROM clients WHERE id IN (SELECT dup_id FROM _tmp_client_map WHERE dup_id != keep_id)"
    )
    db.execute("DROP TABLE IF EXISTS _tmp_client_map")
    return int(merged)


def _passport_merge_dry_run(db: sqlite3.Connection) -> Dict[str, object]:
    rows = db.execute(
        "SELECT id, full_name, gender, age, passport FROM clients WHERE passport IS NOT NULL AND TRIM(passport)!='' ORDER BY id"
    ).fetchall()
    groups: Dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        k = _norm_passport(r["passport"])
        if not k:
            continue
        groups.setdefault(k, []).append(r)

    candidate_groups = 0
    safe_groups = 0
    blocked_groups = 0
    safe_pairs = 0
    blocked_pairs = 0
    blocked_reasons: Dict[str, int] = {}
    sample_blocked: List[dict] = []
    merge_plan: List[tuple[int, int]] = []

    for passport, g in groups.items():
        if len(g) < 2:
            continue
        candidate_groups += 1
        keep = min(g, key=lambda x: x["id"])
        group_ok = True
        for dup in g:
            if dup["id"] == keep["id"]:
                continue
            ok, reasons = _safe_passport_pair(db, keep, dup)
            if ok:
                safe_pairs += 1
                merge_plan.append((keep["id"], dup["id"]))
            else:
                group_ok = False
                blocked_pairs += 1
                for r in reasons:
                    blocked_reasons[r] = blocked_reasons.get(r, 0) + 1
                if len(sample_blocked) < 10:
                    sample_blocked.append(
                        {
                            "passport": passport,
                            "keep_id": keep["id"],
                            "dup_id": dup["id"],
                            "reasons": reasons,
                        }
                    )
        if group_ok:
            safe_groups += 1
        else:
            blocked_groups += 1

    return {
        "candidate_groups": candidate_groups,
        "safe_groups": safe_groups,
        "blocked_groups": blocked_groups,
        "safe_pairs": safe_pairs,
        "blocked_pairs": blocked_pairs,
        "blocked_reasons": blocked_reasons,
        "sample_blocked": sample_blocked,
        "merge_plan": merge_plan,
    }


def _merge_clients_by_passport_safe(db: sqlite3.Connection, dry_run: bool = True) -> Dict[str, object]:
    report = _passport_merge_dry_run(db)
    if dry_run:
        return {
            "mode": "dry_run",
            **{k: v for k, v in report.items() if k != "merge_plan"},
        }

    merged = 0
    for keep_id, dup_id in report["merge_plan"]:
        src = db.execute("SELECT * FROM clients WHERE id=?", (dup_id,)).fetchone()
        if not src:
            continue
        db.execute(
            """
            UPDATE clients
            SET full_name=COALESCE(NULLIF(full_name,''), ?),
                gender=COALESCE(NULLIF(gender,''), ?),
                age=COALESCE(age, ?),
                income_source=COALESCE(NULLIF(income_source,''), ?),
                position=COALESCE(NULLIF(position,''), ?),
                workplace=COALESCE(NULLIF(workplace,''), ?),
                region=COALESCE(NULLIF(region,''), ?),
                mfy=COALESCE(NULLIF(mfy,''), ?),
                phone=COALESCE(NULLIF(phone,''), ?),
                passport=COALESCE(NULLIF(passport,''), ?),
                updated_at=?
            WHERE id=?
            """,
            (
                src["full_name"], src["gender"], src["age"], src["income_source"],
                src["position"], src["workplace"], src["region"], src["mfy"],
                src["phone"], src["passport"], datetime.now().isoformat(), keep_id,
            ),
        )
        db.execute("UPDATE contracts SET client_id=? WHERE client_id=?", (keep_id, dup_id))
        if _table_exists(db, "payments"):
            db.execute("UPDATE payments SET client_id=? WHERE client_id=?", (keep_id, dup_id))
        db.execute("DELETE FROM clients WHERE id=?", (dup_id,))
        merged += 1

    return {
        "mode": "apply",
        "merged_clients": merged,
        **{k: v for k, v in report.items() if k != "merge_plan"},
    }


def _dedupe_contracts(db: sqlite3.Connection) -> int:
    db.execute("DROP TABLE IF EXISTS _tmp_contract_map")
    db.execute(
        """
        CREATE TEMP TABLE _tmp_contract_map AS
        SELECT id AS dup_id,
               MIN(id) OVER (PARTITION BY external_id, branch) AS keep_id
        FROM contracts
        WHERE external_id IS NOT NULL
        """
    )
    removed = db.execute(
        "SELECT COUNT(*) FROM _tmp_contract_map WHERE dup_id != keep_id"
    ).fetchone()[0]
    if removed == 0:
        db.execute("DROP TABLE IF EXISTS _tmp_contract_map")
        return 0

    db.execute(
        """
        UPDATE contracts
        SET paid_amount = (
                SELECT MAX(COALESCE(c2.paid_amount,0)) FROM contracts c2
                WHERE c2.external_id = contracts.external_id AND c2.branch = contracts.branch
            ),
            debt_amount = (
                SELECT MAX(COALESCE(c2.debt_amount,0)) FROM contracts c2
                WHERE c2.external_id = contracts.external_id AND c2.branch = contracts.branch
            ),
            overdue_amount = (
                SELECT MAX(COALESCE(c2.overdue_amount,0)) FROM contracts c2
                WHERE c2.external_id = contracts.external_id AND c2.branch = contracts.branch
            ),
            total_late_days = (
                SELECT MAX(COALESCE(c2.total_late_days,0)) FROM contracts c2
                WHERE c2.external_id = contracts.external_id AND c2.branch = contracts.branch
            ),
            late_count = (
                SELECT MAX(COALESCE(c2.late_count,0)) FROM contracts c2
                WHERE c2.external_id = contracts.external_id AND c2.branch = contracts.branch
            )
        WHERE external_id IS NOT NULL
        """
    )

    if _table_exists(db, "payments"):
        db.execute(
            """
            UPDATE payments
            SET contract_id = (
                SELECT keep_id FROM _tmp_contract_map m WHERE m.dup_id = payments.contract_id
            )
            WHERE contract_id IN (SELECT dup_id FROM _tmp_contract_map WHERE dup_id != keep_id)
            """
        )
    db.execute(
        "DELETE FROM contracts WHERE id IN (SELECT dup_id FROM _tmp_contract_map WHERE dup_id != keep_id)"
    )
    db.execute("DROP TABLE IF EXISTS _tmp_contract_map")
    return int(removed)


def _enrich_missing_client_fields(db: sqlite3.Connection) -> int:
    updated = 0
    groups = db.execute(
        "SELECT external_id FROM clients WHERE external_id IS NOT NULL GROUP BY external_id HAVING COUNT(*) > 1"
    ).fetchall()
    for g in groups:
        ext = g["external_id"]
        src = db.execute(
            """
            SELECT
                MAX(CASE WHEN age IS NOT NULL AND age BETWEEN 18 AND 120 THEN age END) AS age,
                MAX(CASE WHEN region IS NOT NULL AND TRIM(region)!='' THEN region END) AS region,
                MAX(CASE WHEN income_source IS NOT NULL AND TRIM(income_source)!='' THEN income_source END) AS income_source,
                MAX(CASE WHEN mfy IS NOT NULL AND TRIM(mfy)!='' THEN mfy END) AS mfy
            FROM clients WHERE external_id=?
            """,
            (ext,),
        ).fetchone()
        if not src:
            continue
        r = db.execute(
            """
            UPDATE clients
            SET age=COALESCE(age, ?),
                region=COALESCE(NULLIF(region,''), ?),
                income_source=COALESCE(NULLIF(income_source,''), ?),
                mfy=COALESCE(NULLIF(mfy,''), ?),
                updated_at=?
            WHERE external_id=?
            """,
            (src["age"], src["region"], src["income_source"], src["mfy"], datetime.now().isoformat(), ext),
        )
        updated += r.rowcount or 0
    return updated


def optimize_data_quality(passport_mode: str = "dry_run") -> Dict[str, object]:
    before = get_data_quality_profile()
    db = get_db()
    _set_busy_timeout(db)
    try:
        passport_mode = (passport_mode or "dry_run").strip().lower()
        if passport_mode not in ("off", "dry_run", "apply"):
            passport_mode = "dry_run"

        merged_external = _merge_clients_by_external_id(db)
        passport_report = _merge_clients_by_passport_safe(db, dry_run=(passport_mode != "apply")) if passport_mode != "off" else {"mode": "off"}
        removed_contract_dups = _dedupe_contracts(db)
        enriched_clients = _enrich_missing_client_fields(db)

        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_contracts_external_branch "
            "ON contracts(external_id, branch) WHERE external_id IS NOT NULL"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS ix_clients_external_id ON clients(external_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS ix_clients_passport ON clients(passport)"
        )
        db.commit()
    except sqlite3.OperationalError as e:
        db.rollback()
        return {
            "ok": False,
            "error": str(e),
            "message": "Не удалось выполнить оптимизацию: база занята (database is locked).",
        }
    finally:
        db.close()
    after = get_data_quality_profile()
    return {
        "ok": True,
        "message": "Оптимизация данных завершена",
        "passport_mode": passport_mode,
        "before": before,
        "after": after,
        "actions": {
            "merged_clients_by_external_id": merged_external,
            "passport_merge_report": passport_report,
            "removed_duplicate_contracts": removed_contract_dups,
            "enriched_clients": enriched_clients,
        },
    }


def get_data_readiness_scores(branch: str | None = None) -> Dict[str, object]:
    db = get_db()
    _set_busy_timeout(db)
    try:
        branches = db.execute(
            "SELECT DISTINCT branch FROM contracts WHERE branch IS NOT NULL ORDER BY branch"
        ).fetchall()
        items: list[dict] = []
        for br_row in branches:
            br = br_row["branch"]
            if branch and br != branch:
                continue
            contract_counts = db.execute(
                """
                SELECT
                    COUNT(*) AS contracts_total,
                    SUM(CASE WHEN contract_date IS NULL OR TRIM(contract_date)='' THEN 1 ELSE 0 END) AS missing_contract_date,
                    SUM(CASE WHEN COALESCE(product_amount,0)<=0 THEN 1 ELSE 0 END) AS missing_product_amount
                FROM contracts
                WHERE branch = ?
                """,
                (br,),
            ).fetchone()
            client_counts = db.execute(
                """
                WITH branch_clients AS (
                    SELECT DISTINCT client_id FROM contracts WHERE branch = ?
                )
                SELECT
                    COUNT(*) AS clients_total,
                    SUM(CASE WHEN c.age IS NULL OR c.age < 18 OR c.age > 120 THEN 1 ELSE 0 END) AS missing_age,
                    SUM(CASE WHEN c.region IS NULL OR TRIM(c.region)='' THEN 1 ELSE 0 END) AS missing_region,
                    SUM(CASE WHEN c.income_source IS NULL OR TRIM(c.income_source)='' THEN 1 ELSE 0 END) AS missing_income,
                    SUM(CASE WHEN c.passport IS NULL OR TRIM(c.passport)='' THEN 1 ELSE 0 END) AS missing_passport,
                    SUM(CASE WHEN c.external_id IS NULL THEN 1 ELSE 0 END) AS missing_external_id
                FROM clients c
                WHERE c.id IN (SELECT client_id FROM branch_clients)
                """,
                (br,),
            ).fetchone()

            contracts_total = contract_counts["contracts_total"] or 0
            clients_total = client_counts["clients_total"] or 0
            if contracts_total == 0 or clients_total == 0:
                continue

            dup_client_groups = db.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT c.external_id
                    FROM contracts ct JOIN clients c ON c.id=ct.client_id
                    WHERE ct.branch=? AND c.external_id IS NOT NULL
                    GROUP BY c.external_id
                    HAVING COUNT(*)>1
                )
                """,
                (br,),
            ).fetchone()[0]

            duplicate_rate = min(100.0, (dup_client_groups * 100.0 / max(1, clients_total)))
            field_missing_rate = (
                (client_counts["missing_age"] or 0)
                + (client_counts["missing_region"] or 0)
                + (client_counts["missing_income"] or 0)
                + (client_counts["missing_passport"] or 0)
                + (client_counts["missing_external_id"] or 0)
            ) / max(1, clients_total * 5) * 100.0
            contract_valid_rate = 100.0 - (
                ((contract_counts["missing_contract_date"] or 0) + (contract_counts["missing_product_amount"] or 0))
                / max(1, contracts_total * 2) * 100.0
            )
            freshness_rate = 100.0 if contracts_total > 0 else 0.0

            score = (
                (100.0 - field_missing_rate) * 0.50
                + max(0.0, 100.0 - duplicate_rate) * 0.20
                + max(0.0, contract_valid_rate) * 0.20
                + freshness_rate * 0.10
            )
            score = round(max(0.0, min(100.0, score)), 2)

            reasons = []
            if field_missing_rate > 15:
                reasons.append("низкая заполненность клиентских полей")
            if duplicate_rate > 2:
                reasons.append("высокая доля дублей по external_id")
            if contract_valid_rate < 95:
                reasons.append("неполные данные договоров")
            if not reasons:
                reasons.append("критичных проблем не обнаружено")

            items.append(
                {
                    "branch": br,
                    "score": score,
                    "status": "green" if score >= 85 else "yellow" if score >= 65 else "red",
                    "clients_total": clients_total,
                    "contracts_total": contracts_total,
                    "field_missing_rate_pct": round(field_missing_rate, 2),
                    "duplicate_rate_pct": round(duplicate_rate, 2),
                    "contract_valid_rate_pct": round(max(0.0, contract_valid_rate), 2),
                    "top_reasons": reasons[:3],
                }
            )

        items.sort(key=lambda x: x["branch"])
        return {"generated_at": datetime.now().isoformat(), "items": items}
    finally:
        db.close()


def get_reference_dataset_profile() -> Dict[str, object]:
    ref_db = f"{BASE_DIR}/external/claude_update_v2/credit_control.db"
    out: Dict[str, object] = {
        "reference_db_path": ref_db,
        "available": False,
    }
    try:
        rdb = sqlite3.connect(ref_db)
        rdb.row_factory = sqlite3.Row
        tables = [x[0] for x in rdb.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "clients" not in tables:
            rdb.close()
            return out
        total = rdb.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        unique_clients = rdb.execute("SELECT COUNT(DISTINCT kod_mijoz) FROM clients").fetchone()[0]
        npl_count = rdb.execute(
            "SELECT SUM(CASE WHEN COALESCE(jami_kechikkan_kun,0) > 90 THEN 1 ELSE 0 END) FROM clients"
        ).fetchone()[0] or 0
        rdb.close()
        out.update(
            {
                "available": True,
                "reference_rows": total,
                "reference_unique_clients": unique_clients,
                "reference_npl_count": npl_count,
                "reference_npl_rate_pct": round(npl_count * 100.0 / total, 2) if total else 0,
            }
        )
        return out
    except Exception as e:
        out["error"] = str(e)
        return out

