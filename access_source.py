"""
CCC — MS Access as data source (read-only).
Access = source; CCC DB = system of record.
Connect via pyodbc, read clients/contracts/payments, map to CCC schema, return normalized data.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Optional: different Access column names -> CCC field names
ACCESS_CLIENT_MAP = {
    "id": "external_id",
    "client_id": "external_id",
    "name": "full_name",
    "full_name": "full_name",
    "fio": "full_name",
    "фио": "full_name",
    "region": "region",
    "region_id": "region",
    "age": "age",
    "income_source": "income_source",
    "monthly_income": "monthly_income",
    "income": "monthly_income",
    "доход": "monthly_income",
    "phone": "phone",
    "passport": "passport",
    "gender": "gender",
    "position": "position",
    "workplace": "workplace",
    "mfy": "mfy",
    "is_synced": "is_synced",
    "last_updated_at": "last_updated_at",
}

ACCESS_CONTRACT_MAP = {
    "id": "external_id",
    "contract_id": "external_id",
    "client_id": "client_external_id",
    "client_external_id": "client_external_id",
    "product_amount": "product_amount",
    "contract_amount": "product_amount",
    "summa": "product_amount",
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
    "paid_amount": "paid_amount",
    "debt_amount": "debt_amount",
    "responsible_person": "responsible_person",
    "is_synced": "is_synced",
    "last_updated_at": "last_updated_at",
}

ACCESS_PAYMENT_MAP = {
    "contract_id": "contract_external_id",
    "contract_external_id": "contract_external_id",
    "amount": "amount",
    "payment_date": "payment_date",
    "payment_type": "payment_type",
    "delay_days": "delay_days",
    "is_synced": "is_synced",
    "last_updated_at": "last_updated_at",
}


def _row_to_dict(cursor, row) -> Dict[str, Any]:
    """Convert pyodbc row to dict by column names."""
    if row is None:
        return {}
    columns = [d[0] for d in cursor.description] if cursor.description else []
    return dict(zip(columns, row)) if columns else {}


def _normalize(row: Dict[str, Any], mapping: Dict[str, str], exclude: Optional[List[str]] = None) -> Dict[str, Any]:
    """Map source keys to CCC keys; lowercase and strip."""
    exclude = exclude or []
    out: Dict[str, Any] = {}
    for k, v in (row or {}).items():
        key = (k or "").strip().lower().replace(" ", "_")
        if key in exclude:
            continue
        if key in mapping:
            out[mapping[key]] = v
        else:
            out[key] = v
    return out


def _parse_val(v: Any, typ: str = "str") -> Any:
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    if typ == "int":
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None
    if typ == "float":
        try:
            return float(v)
        except (ValueError, TypeError):
            return None
    return str(v).strip() if v else None


def connect_access(db_path: str):
    """
    Connect to MS Access database via pyodbc.
    Returns connection or raises ImportError/Exception.
    """
    try:
        import pyodbc
    except ImportError:
        raise ImportError("pyodbc is required for MS Access. Install: pip install pyodbc")
    driver = "Microsoft Access Driver (*.mdb, *.accdb)"
    conn_str = rf"DRIVER={{{driver}}};DBQ={db_path};"
    return pyodbc.connect(conn_str)


def get_access_table_names(conn) -> List[str]:
    """Return list of table names in Access DB."""
    # Some Access setups restrict direct MSysObjects access.
    # Fallback to ODBC metadata listing if system table query is blocked.
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT Name FROM MSysObjects WHERE Type=1 AND Flags=0 ORDER BY Name")
        return [r[0] for r in cursor.fetchall() if not (r[0] or "").startswith("MSys")]
    except Exception:
        tables: List[str] = []
        try:
            for t in conn.cursor().tables(tableType="TABLE"):
                name = getattr(t, "table_name", None) or (t[2] if len(t) > 2 else None)
                if name and not str(name).startswith("MSys"):
                    tables.append(str(name))
        except Exception:
            return []
        return sorted(set(tables))


def read_clients(
    conn,
    only_unsynced: bool = False,
    synced_column: str = "is_synced",
    limit: Optional[int] = None,
    table: str = "clients",
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Read clients from Access. Tries table 'clients' or first table containing 'client'.
    Returns (list of normalized dicts for CCC, list of errors).
    """
    cursor = conn.cursor()
    errors: List[str] = []
    try:
        cursor.execute(f"SELECT * FROM [{table}]")
        columns = [d[0] for d in cursor.description]
    except Exception as e:
        errors.append(f"Table {table}: {e}")
        return [], errors

    rows: List[Dict[str, Any]] = []
    for row in cursor.fetchall():
        d = dict(zip(columns, row))
        if only_unsynced and synced_column in d and d.get(synced_column) not in (0, None, "0", False):
            continue
        normalized = _normalize(d, ACCESS_CLIENT_MAP, exclude=[synced_column, "last_updated_at"])
        # Ensure external_id and full_name
        ext_id = _parse_val(normalized.get("external_id"), "int")
        name = _parse_val(normalized.get("full_name"), "str") or _parse_val(d.get("name") or d.get("FIO") or d.get("ФИО"), "str")
        if not name and ext_id is None:
            errors.append(f"Skip client: missing name and id")
            continue
        normalized["external_id"] = ext_id
        normalized["full_name"] = name or ""
        rows.append(normalized)
        if limit and len(rows) >= limit:
            break

    return rows, errors


def read_contracts(
    conn,
    only_unsynced: bool = False,
    synced_column: str = "is_synced",
    limit: Optional[int] = None,
    table: str = "contracts",
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Read contracts from Access. Returns (list of normalized dicts, errors)."""
    cursor = conn.cursor()
    errors: List[str] = []
    try:
        cursor.execute(f"SELECT * FROM [{table}]")
        columns = [d[0] for d in cursor.description]
    except Exception as e:
        errors.append(f"Table {table}: {e}")
        return [], errors

    rows: List[Dict[str, Any]] = []
    for row in cursor.fetchall():
        d = dict(zip(columns, row))
        if only_unsynced and synced_column in d and d.get(synced_column) not in (0, None, "0", False):
            continue
        normalized = _normalize(d, ACCESS_CONTRACT_MAP, exclude=[synced_column, "last_updated_at"])
        client_ext = _parse_val(normalized.get("client_external_id") or d.get("client_id"), "int")
        if client_ext is None:
            errors.append("Skip contract: missing client_id")
            continue
        normalized["client_external_id"] = client_ext
        normalized["external_id"] = _parse_val(normalized.get("external_id") or d.get("id") or d.get("contract_id"), "int")
        rows.append(normalized)
        if limit and len(rows) >= limit:
            break

    return rows, errors


def read_payments(
    conn,
    only_unsynced: bool = False,
    synced_column: str = "is_synced",
    limit: Optional[int] = None,
    table: str = "payments",
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Read payments from Access. Returns (list of normalized dicts, errors)."""
    cursor = conn.cursor()
    errors: List[str] = []
    try:
        cursor.execute(f"SELECT * FROM [{table}]")
        columns = [d[0] for d in cursor.description]
    except Exception as e:
        errors.append(f"Table {table}: {e}")
        return [], errors

    rows: List[Dict[str, Any]] = []
    for row in cursor.fetchall():
        d = dict(zip(columns, row))
        if only_unsynced and synced_column in d and d.get(synced_column) not in (0, None, "0", False):
            continue
        normalized = _normalize(d, ACCESS_PAYMENT_MAP, exclude=[synced_column, "last_updated_at"])
        contract_ext = _parse_val(normalized.get("contract_external_id") or d.get("contract_id"), "int")
        amount = _parse_val(normalized.get("amount") or d.get("amount"), "float")
        if contract_ext is None or amount is None or amount <= 0:
            errors.append("Skip payment: missing contract_id or amount")
            continue
        normalized["contract_external_id"] = contract_ext
        normalized["amount"] = amount
        rows.append(normalized)
        if limit and len(rows) >= limit:
            break

    return rows, errors


def extract_from_access(
    db_path: str,
    only_unsynced: bool = False,
    clients_table: str = "clients",
    contracts_table: str = "contracts",
    payments_table: str = "payments",
    limit_per_entity: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Extract clients, contracts, payments from Access DB.
    Returns: {
        "clients": [...],
        "contracts": [...],
        "payments": [...],
        "errors": [...],
        "table_names": [...]
    }
    """
    result: Dict[str, Any] = {
        "clients": [],
        "contracts": [],
        "payments": [],
        "errors": [],
        "table_names": [],
    }
    conn = None
    try:
        conn = connect_access(db_path)
        result["table_names"] = get_access_table_names(conn)

        cl, err_cl = read_clients(conn, only_unsynced=only_unsynced, limit=limit_per_entity, table=clients_table)
        result["clients"] = cl
        result["errors"].extend(err_cl)

        ct, err_ct = read_contracts(conn, only_unsynced=only_unsynced, limit=limit_per_entity, table=contracts_table)
        result["contracts"] = ct
        result["errors"].extend(err_ct)

        pm, err_pm = read_payments(conn, only_unsynced=only_unsynced, limit=limit_per_entity, table=payments_table)
        result["payments"] = pm
        result["errors"].extend(err_pm)

    except Exception as e:
        result["errors"].append(f"Access connection or read failed: {e}")
        logger.exception("Access extract failed")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return result
