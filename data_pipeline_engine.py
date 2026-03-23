"""
CCC — Data Pipeline Engine
Unified pipeline: import (CSV/rows) + validation + feature refresh + behavioral/hybrid + portfolio/ML.
Orchestrates existing engines without replacing them.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.database import get_db


# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════

def _parse_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        if hasattr(v, "strip"):
            v = v.strip()
        if v == "":
            return None
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _parse_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        if hasattr(v, "strip"):
            v = v.strip()
        if v == "":
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def _str_clean(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip() if v != "" else None
    return s if s else None


# ═══════════════════════════════════════════════════════════════
#  CLIENT IMPORT
# ═══════════════════════════════════════════════════════════════

# CSV/JSON column aliases -> DB field
CLIENT_COLUMN_MAP = {
    "client_id": "external_id",
    "external_id": "external_id",
    "name": "full_name",
    "full_name": "full_name",
    "fio": "full_name",
    "фио": "full_name",
    "age": "age",
    "region": "region",
    "income_source": "income_source",
    "monthly_income": "monthly_income",
    "доход": "monthly_income",
    "income": "monthly_income",
}


def _normalize_client_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, val in (row or {}).items():
        k = (key or "").strip().lower().replace(" ", "_")
        if k in CLIENT_COLUMN_MAP:
            out[CLIENT_COLUMN_MAP[k]] = val
    return out


def import_clients_csv(
    rows: List[Dict[str, Any]],
    batch_size: int = 500,
    validate_only: bool = False,
) -> Dict[str, Any]:
    """
    Import clients from list of dicts (e.g. from CSV).
    Columns: client_id/external_id, name/full_name, age, region, income_source, monthly_income.
    Duplicate external_id: update existing row.
    """
    errors: List[Dict[str, Any]] = []
    inserted = 0
    updated = 0
    skipped = 0

    db = get_db()
    try:
        for i, raw in enumerate(rows):
            row = _normalize_client_row(raw) if isinstance(raw, dict) else {}
            if not row:
                row = raw if isinstance(raw, dict) else {}

            external_id = _parse_int(row.get("external_id") or row.get("client_id"))
            full_name = _str_clean(row.get("full_name") or row.get("name"))
            if not full_name and external_id is None:
                errors.append({"row": i + 1, "message": "missing full_name and external_id"})
                skipped += 1
                continue

            age = _parse_int(row.get("age"))
            region = _str_clean(row.get("region"))
            income_source = _str_clean(row.get("income_source"))
            monthly_income = _parse_float(row.get("monthly_income"))

            if validate_only:
                inserted += 1
                continue

            try:
                existing = None
                if external_id is not None:
                    r = db.execute(
                        "SELECT id FROM clients WHERE external_id = ?", (external_id,)
                    ).fetchone()
                    existing = dict(r) if r else None
                if existing:
                    db.execute(
                        """UPDATE clients SET full_name = ?, age = ?, region = ?, income_source = ?, monthly_income = ?
                           WHERE id = ?""",
                        (full_name or "", age, region or "", income_source or "", monthly_income, existing["id"]),
                    )
                    updated += 1
                else:
                    db.execute(
                        """INSERT INTO clients (external_id, full_name, age, region, income_source, monthly_income)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (external_id, full_name or "", age, region or "", income_source or "", monthly_income),
                    )
                    inserted += 1
            except Exception as e:
                errors.append({"row": i + 1, "message": str(e)})
                skipped += 1

            if (i + 1) % batch_size == 0:
                db.commit()
    finally:
        db.commit()
        db.close()

    return {
        "status": "ok",
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "errors": errors[:100],
        "total_rows": len(rows),
    }


# ═══════════════════════════════════════════════════════════════
#  CONTRACT IMPORT
# ═══════════════════════════════════════════════════════════════

CONTRACT_COLUMN_MAP = {
    "contract_id": "external_id",
    "external_id": "external_id",
    "client_id": "client_external_id",
    "client_external_id": "client_external_id",
    "product_amount": "product_amount",
    "contract_amount": "product_amount",
    "advance_payment": "advance_payment",
    "term_months": "contract_term",
    "contract_term": "contract_term",
    "branch": "branch",
    "product_type": "product_type",
    "product_category": "product_category",
    "category": "product_category",
    "supplier": "supplier",
    "provider": "supplier",
    "vendor": "supplier",
    "contract_date": "contract_date",
    "status": "status",
    "status_detail": "status_detail",
}


def _normalize_contract_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, val in (row or {}).items():
        k = (key or "").strip().lower().replace(" ", "_")
        if k in CONTRACT_COLUMN_MAP:
            out[CONTRACT_COLUMN_MAP[k]] = val
    return out


def import_contracts_csv(
    rows: List[Dict[str, Any]],
    batch_size: int = 500,
    validate_only: bool = False,
) -> Dict[str, Any]:
    """
    Import contracts. client_id in row = client external_id; resolved to internal client_id.
    """
    errors: List[Dict[str, Any]] = []
    inserted = 0
    updated = 0
    skipped = 0

    db = get_db()
    for i, raw in enumerate(rows):
        row = _normalize_contract_row(raw) if isinstance(raw, dict) else {}
        if not row:
            row = raw if isinstance(raw, dict) else {}

        client_ext = _parse_int(row.get("client_external_id") or row.get("client_id"))
        external_id = _parse_int(row.get("external_id") or row.get("contract_id"))
        product_amount = _parse_float(row.get("product_amount")) or 0
        advance_payment = _parse_float(row.get("advance_payment")) or 0
        contract_term = _parse_int(row.get("contract_term") or row.get("term_months"))
        branch = _str_clean(row.get("branch"))
        product_type = _str_clean(row.get("product_type"))
        product_category = _str_clean(row.get("product_category") or row.get("category"))
        supplier = _str_clean(row.get("supplier") or row.get("provider") or row.get("vendor"))
        contract_date = _str_clean(row.get("contract_date"))
        status = _str_clean(row.get("status"))
        status_detail = _str_clean(row.get("status_detail"))

        if client_ext is None:
            errors.append({"row": i + 1, "message": "missing client_id (external)"})
            skipped += 1
            continue

        client_row = db.execute(
            "SELECT id FROM clients WHERE external_id = ?", (client_ext,)
        ).fetchone()
        if not client_row:
            errors.append({"row": i + 1, "message": f"client external_id {client_ext} not found"})
            skipped += 1
            continue

        client_id = client_row[0]

        if validate_only:
            inserted += 1
            continue

        try:
            existing = None
            if external_id is not None:
                r = db.execute(
                    "SELECT id FROM contracts WHERE external_id = ? AND client_id = ?",
                    (external_id, client_id),
                ).fetchone()
                existing = dict(r) if r else None
            now = datetime.now().isoformat()
            if existing:
                db.execute(
                    """UPDATE contracts SET product_amount = ?, advance_payment = ?, contract_term = ?,
                       branch = ?, product_type = ?, product_category = ?, supplier = ?, contract_date = ?, status = ?, status_detail = ?
                       WHERE id = ?""",
                    (product_amount, advance_payment, contract_term, branch or "", product_type or "",
                     product_category or "", supplier or "", contract_date or "", status or "", status_detail or "", existing["id"]),
                )
                updated += 1
            else:
                db.execute(
                    """INSERT INTO contracts (client_id, external_id, branch, product_type, product_category, supplier, contract_date,
                       contract_term, product_amount, advance_payment, status, status_detail, imported_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (client_id, external_id, branch or "", product_type or "", product_category or "", supplier or "", contract_date or "",
                     contract_term, product_amount, advance_payment, status or "", status_detail or "", now),
                )
                inserted += 1
        except Exception as e:
            errors.append({"row": i + 1, "message": str(e)})
            skipped += 1

        if (i + 1) % batch_size == 0:
            db.commit()

    db.commit()
    db.close()

    return {
        "status": "ok",
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "errors": errors[:100],
        "total_rows": len(rows),
    }


# ═══════════════════════════════════════════════════════════════
#  PAYMENTS IMPORT
# ═══════════════════════════════════════════════════════════════

PAYMENT_COLUMN_MAP = {
    "contract_id": "contract_external_id",
    "contract_external_id": "contract_external_id",
    "amount": "amount",
    "payment_date": "payment_date",
    "payment_type": "payment_type",
    "delay_days": "delay_days",
}


def _normalize_payment_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, val in (row or {}).items():
        k = (key or "").strip().lower().replace(" ", "_")
        if k in PAYMENT_COLUMN_MAP:
            out[PAYMENT_COLUMN_MAP[k]] = val
    return out


def import_payments_csv(
    rows: List[Dict[str, Any]],
    batch_size: int = 500,
    validate_only: bool = False,
) -> Dict[str, Any]:
    """
    Import payments. contract_id in row = contract external_id; resolved to internal contract_id.
    amount required and must be > 0.
    """
    errors: List[Dict[str, Any]] = []
    inserted = 0
    skipped = 0

    db = get_db()
    for i, raw in enumerate(rows):
        row = _normalize_payment_row(raw) if isinstance(raw, dict) else {}
        if not row:
            row = raw if isinstance(raw, dict) else {}

        contract_ext = _parse_int(row.get("contract_external_id") or row.get("contract_id"))
        amount = _parse_float(row.get("amount"))
        payment_date = _str_clean(row.get("payment_date"))
        payment_type = _str_clean(row.get("payment_type"))
        delay_days = _parse_int(row.get("delay_days"))

        if contract_ext is None:
            errors.append({"row": i + 1, "message": "missing contract_id (external)"})
            skipped += 1
            continue
        if amount is None or amount <= 0:
            errors.append({"row": i + 1, "message": "missing or invalid amount"})
            skipped += 1
            continue

        contract_row = db.execute(
            "SELECT id, client_id FROM contracts WHERE external_id = ?", (contract_ext,)
        ).fetchone()
        if not contract_row:
            errors.append({"row": i + 1, "message": f"contract external_id {contract_ext} not found"})
            skipped += 1
            continue

        contract_id = contract_row[0]
        client_id = contract_row[1]

        if validate_only:
            inserted += 1
            continue

        try:
            now = datetime.now().isoformat()
            db.execute(
                """INSERT INTO payments (contract_id, client_id, amount, payment_date, payment_type, delay_days, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (contract_id, client_id, amount, payment_date or "", payment_type or "", delay_days or 0, now, now),
            )
            inserted += 1
        except Exception as e:
            errors.append({"row": i + 1, "message": str(e)})
            skipped += 1

        if (i + 1) % batch_size == 0:
            db.commit()

    db.commit()
    db.close()

    return {
        "status": "ok",
        "inserted": inserted,
        "skipped": skipped,
        "errors": errors[:100],
        "total_rows": len(rows),
    }


# ═══════════════════════════════════════════════════════════════
#  RECALCULATE (orchestrate engines)
# ═══════════════════════════════════════════════════════════════

def run_recalculate(limit: int = 1000) -> Dict[str, Any]:
    """
    Run full pipeline: feature store -> behavioral scores -> daily snapshot -> intelligence -> training dataset.
    """
    results: Dict[str, Any] = {}
    try:
        from engines.feature_store_engine import refresh_feature_store
        results["feature_store"] = refresh_feature_store(limit=limit)
    except Exception as e:
        results["feature_store"] = {"status": "error", "message": str(e)}

    try:
        from engines.behavioral_scoring_engine import compute_behavioral_score_batch
        results["behavioral"] = compute_behavioral_score_batch(limit=limit)
    except Exception as e:
        results["behavioral"] = {"status": "error", "message": str(e)}

    try:
        from engines.analytics_engine import compute_daily_snapshot
        compute_daily_snapshot()
        results["analytics"] = {"status": "ok"}
    except Exception as e:
        results["analytics"] = {"status": "error", "message": str(e)}

    try:
        from engines.intelligence_engine import compute_intelligence_tables, prepare_training_dataset
        results["intelligence"] = compute_intelligence_tables()
        results["training"] = prepare_training_dataset()
    except Exception as e:
        results["intelligence"] = {"status": "error", "message": str(e)}
        results["training"] = {}

    all_ok = all(
        r.get("status") == "ok" or "status" not in r or isinstance(r, dict) and "error" not in str(r).lower()
        for r in results.values() if isinstance(r, dict)
    )
    return {
        "status": "ok" if all_ok else "partial",
        "results": results,
    }


def parse_csv_to_rows(content: bytes | str, encoding: str = "utf-8-sig") -> List[Dict[str, Any]]:
    """Parse CSV file content to list of dicts (first row = headers)."""
    if isinstance(content, bytes):
        content = content.decode(encoding, errors="replace")
    reader = csv.DictReader(io.StringIO(content), delimiter=",", quotechar='"')
    return list(reader)


# ═══════════════════════════════════════════════════════════════
#  IMPORT FROM MS ACCESS (data source, not primary DB)
# ═══════════════════════════════════════════════════════════════

def import_from_access(
    db_path: str,
    branch: Optional[str] = None,
    only_unsynced: bool = False,
    run_engines: bool = True,
    clients_table: str = "clients",
    contracts_table: str = "contracts",
    payments_table: str = "payments",
    limit_per_entity: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Extract from MS Access -> validate -> map -> insert into CCC DB -> trigger engines.
    Access is data source only; CCC remains system of record.
    Returns: status, clients: {inserted, updated, skipped, errors}, contracts: {...}, payments: {...}, engines: {...}.
    """
    import logging
    log = logging.getLogger(__name__)
    result: Dict[str, Any] = {
        "status": "ok",
        "clients": {"inserted": 0, "updated": 0, "skipped": 0, "errors": []},
        "contracts": {"inserted": 0, "updated": 0, "skipped": 0, "errors": []},
        "payments": {"inserted": 0, "skipped": 0, "errors": []},
        "engines": {},
        "extract_errors": [],
    }

    try:
        from data_sources.access_source import extract_from_access as extract
    except ImportError as e:
        result["status"] = "error"
        result["extract_errors"].append(f"Access source not available: {e}")
        return result

    extracted = extract(
        db_path,
        only_unsynced=only_unsynced,
        clients_table=clients_table,
        contracts_table=contracts_table,
        payments_table=payments_table,
        limit_per_entity=limit_per_entity,
    )
    result["extract_errors"] = extracted.get("errors", [])

    if extracted["clients"]:
        cr = import_clients_csv(extracted["clients"], validate_only=False)
        result["clients"]["inserted"] = cr.get("inserted", 0)
        result["clients"]["updated"] = cr.get("updated", 0)
        result["clients"]["skipped"] = cr.get("skipped", 0)
        result["clients"]["errors"] = cr.get("errors", [])[:50]
        log.info("Access import clients: inserted=%s updated=%s skipped=%s", cr.get("inserted"), cr.get("updated"), cr.get("skipped"))

    if extracted["contracts"]:
        if branch:
            for r in extracted["contracts"]:
                r["branch"] = branch
        ct = import_contracts_csv(extracted["contracts"], validate_only=False)
        result["contracts"]["inserted"] = ct.get("inserted", 0)
        result["contracts"]["updated"] = ct.get("updated", 0)
        result["contracts"]["skipped"] = ct.get("skipped", 0)
        result["contracts"]["errors"] = ct.get("errors", [])[:50]
        log.info("Access import contracts: inserted=%s updated=%s skipped=%s", ct.get("inserted"), ct.get("updated"), ct.get("skipped"))

    if extracted["payments"]:
        pm = import_payments_csv(extracted["payments"], validate_only=False)
        result["payments"]["inserted"] = pm.get("inserted", 0)
        result["payments"]["skipped"] = pm.get("skipped", 0)
        result["payments"]["errors"] = pm.get("errors", [])[:50]
        log.info("Access import payments: inserted=%s skipped=%s", pm.get("inserted"), pm.get("skipped"))

    if run_engines:
        try:
            result["engines"] = run_recalculate(limit=2000)
        except Exception as e:
            result["engines"] = {"status": "error", "message": str(e)}
            log.exception("Engines after Access import failed")

    return result
