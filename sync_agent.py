#!/usr/bin/env python3
"""
CCC — Branch Sync Agent.
Runs in filial: reads new records from MS Access, sends to CCC server via REST API, marks as synced.
Run every 5 minutes (configurable). Uses X-API-Key. Retries on failure. Logs all operations.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

# Ensure project root is on path when running from any directory
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
from typing import Any, Dict, List, Optional

# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════

DEFAULT_INTERVAL_SEC = 300  # 5 minutes
DEFAULT_RETRIES = 3
RETRY_DELAY_SEC = 5
BATCH_SIZE = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("sync_agent")


def _parse_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _parse_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _str_clean(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip() if v != "" else None
    return s if s else None


# ═══════════════════════════════════════════════════════════════
#  ACCESS READ (unsynced only)
# ═══════════════════════════════════════════════════════════════

def fetch_new_clients(conn, synced_column: str = "is_synced", limit: int = BATCH_SIZE) -> List[Dict[str, Any]]:
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM clients WHERE [{synced_column}] = 0 OR [{synced_column}] IS NULL")
        columns = [d[0] for d in cursor.description]
        rows = [dict(zip(columns, r)) for r in cursor.fetchall()[:limit]]
        return rows
    except Exception as e:
        logger.warning("fetch_new_clients: %s", e)
        return []


def fetch_new_contracts(conn, synced_column: str = "is_synced", limit: int = BATCH_SIZE) -> List[Dict[str, Any]]:
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM contracts WHERE [{synced_column}] = 0 OR [{synced_column}] IS NULL")
        columns = [d[0] for d in cursor.description]
        rows = [dict(zip(columns, r)) for r in cursor.fetchall()[:limit]]
        return rows
    except Exception as e:
        logger.warning("fetch_new_contracts: %s", e)
        return []


def fetch_new_payments(conn, synced_column: str = "is_synced", limit: int = BATCH_SIZE) -> List[Dict[str, Any]]:
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM payments WHERE [{synced_column}] = 0 OR [{synced_column}] IS NULL")
        columns = [d[0] for d in cursor.description]
        rows = [dict(zip(columns, r)) for r in cursor.fetchall()[:limit]]
        return rows
    except Exception as e:
        logger.warning("fetch_new_payments: %s", e)
        return []


def mark_synced(conn, table: str, id_column: str, id_value: Any, synced_column: str = "is_synced") -> bool:
    try:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE [{table}] SET [{synced_column}] = 1 WHERE [{id_column}] = ?", (id_value,))
        conn.commit()
        return True
    except Exception as e:
        logger.warning("mark_synced %s %s: %s", table, id_value, e)
        return False


# ═══════════════════════════════════════════════════════════════
#  MAP TO CCC SCHEMA
# ═══════════════════════════════════════════════════════════════

def map_client(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map Access row to CCC client payload."""
    def get(k, *alt):
        for key in [k] + list(alt):
            if key in row and row[key] is not None and str(row[key]).strip() != "":
                return row[key]
            low = (key or "").lower().replace(" ", "_")
            for rk, rv in row.items():
                if (rk or "").lower().replace(" ", "_") == low and rv is not None and str(rv).strip() != "":
                    return rv
            if key in ("ФИО", "fio", "name"):
                fio = row.get("ФИО") or row.get("fio") or row.get("name") or row.get("full_name")
                if fio is not None and str(fio).strip() != "":
                    return fio
        return None
    return {
        "external_id": _parse_int(get("id", "client_id")),
        "full_name": _str_clean(get("full_name", "name", "fio", "ФИО")) or "",
        "age": _parse_int(get("age")),
        "region": _str_clean(get("region")),
        "income_source": _str_clean(get("income_source")),
        "monthly_income": _parse_float(get("monthly_income", "income", "доход")),
        "phone": _str_clean(get("phone")),
        "passport": _str_clean(get("passport")),
    }


def map_contract(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map Access row to CCC contract payload (client_external_id, external_id, ...)."""
    def pick(*keys):
        for key in keys:
            if key in row and row[key] is not None and str(row[key]).strip() != "":
                return row[key]
            low = (key or "").lower().replace(" ", "_")
            for rk, rv in row.items():
                if (rk or "").lower().replace(" ", "_") == low and rv is not None and str(rv).strip() != "":
                    return rv
        return None
    return {
        "client_external_id": _parse_int(pick("client_id", "client_external_id")),
        "external_id": _parse_int(pick("id", "contract_id", "external_id")),
        "product_amount": _parse_float(pick("product_amount", "contract_amount", "summa")) or 0,
        "advance_payment": _parse_float(pick("advance_payment")) or 0,
        "contract_term": _parse_int(pick("contract_term", "term_months")),
        "branch": _str_clean(pick("branch")),
        "product_type": _str_clean(pick("product_type")),
        "product_category": _str_clean(pick("product_category", "category")),
        "supplier": _str_clean(pick("supplier", "provider", "vendor")),
        "contract_date": _str_clean(pick("contract_date")),
        "status": _str_clean(pick("status")),
        "status_detail": _str_clean(pick("status_detail")),
        "responsible_person": _str_clean(pick("responsible_person")),
    }


def map_payment(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map Access row to CCC payment payload."""
    return {
        "contract_external_id": _parse_int(row.get("contract_id")),
        "amount": _parse_float(row.get("amount")) or 0,
        "payment_date": _str_clean(row.get("payment_date")),
        "payment_type": _str_clean(row.get("payment_type")),
        "delay_days": _parse_int(row.get("delay_days")) or 0,
    }


# ═══════════════════════════════════════════════════════════════
#  SEND TO SERVER (with retry)
# ═══════════════════════════════════════════════════════════════

def send_to_server(
    api_url: str,
    api_key: str,
    endpoint: str,
    payload: Dict[str, Any],
    retries: int = DEFAULT_RETRIES,
) -> bool:
    try:
        import requests
    except ImportError:
        logger.error("requests not installed. pip install requests")
        return False

    url = f"{api_url.rstrip('/')}{endpoint}"
    headers = {"Content-Type": "application/json", "X-API-Key": api_key}
    for attempt in range(retries):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=30)
            if r.status_code in (200, 201):
                return True
            logger.warning("POST %s status=%s body=%s", endpoint, r.status_code, r.text[:200])
        except Exception as e:
            logger.warning("POST %s attempt %s: %s", endpoint, attempt + 1, e)
        if attempt < retries - 1:
            time.sleep(RETRY_DELAY_SEC)
    return False


# ═══════════════════════════════════════════════════════════════
#  RUN SYNC CYCLE
# ═══════════════════════════════════════════════════════════════

def get_id_column(conn, table: str) -> str:
    """Default primary key column name for Access."""
    for c in ("id", "ID", "Id"):
        try:
            conn.cursor().execute(f"SELECT TOP 1 [{c}] FROM [{table}]")
            return c
        except Exception:
            pass
    return "id"


def run_sync_cycle(
    db_path: str,
    api_url: str,
    api_key: str,
    branch: Optional[str] = None,
    synced_column: str = "is_synced",
) -> Dict[str, int]:
    """One sync cycle: read from Access -> send to API -> mark synced."""
    stats = {"clients": 0, "contracts": 0, "payments": 0, "errors": 0}

    try:
        from data_sources.access_source import connect_access
    except ImportError:
        try:
            import pyodbc
            def connect_access(path):
                return pyodbc.connect(rf'DRIVER={{Microsoft Access Driver (*.mdb, *.accdb)}};DBQ={path};')
        except ImportError:
            logger.error("pyodbc required. pip install pyodbc")
            return stats

    conn = None
    try:
        conn = connect_access(db_path)
    except Exception as e:
        logger.error("Access connect failed: %s", e)
        stats["errors"] += 1
        return stats

    try:
        # Clients
        rows = fetch_new_clients(conn, synced_column=synced_column)
        id_col = get_id_column(conn, "clients")
        for row in rows:
            payload = map_client(row)
            if not payload.get("full_name") and payload.get("external_id") is None:
                continue
            if send_to_server(api_url, api_key, "/api/sync/client", payload):
                mark_synced(conn, "clients", id_col, row.get(id_col), synced_column)
                stats["clients"] += 1
            else:
                stats["errors"] += 1
        if rows:
            logger.info("Clients synced: %s", stats["clients"])

        # Contracts (depend on clients being present)
        rows = fetch_new_contracts(conn, synced_column=synced_column)
        id_col = get_id_column(conn, "contracts")
        for row in rows:
            payload = map_contract(row)
            if branch:
                payload["branch"] = branch
            if payload.get("client_external_id") is None:
                continue
            if send_to_server(api_url, api_key, "/api/sync/contract", payload):
                mark_synced(conn, "contracts", id_col, row.get(id_col), synced_column)
                stats["contracts"] += 1
            else:
                stats["errors"] += 1
        if rows:
            logger.info("Contracts synced: %s", stats["contracts"])

        # Payments
        rows = fetch_new_payments(conn, synced_column=synced_column)
        id_col = get_id_column(conn, "payments")
        for row in rows:
            payload = map_payment(row)
            if payload.get("contract_external_id") is None or payload.get("amount", 0) <= 0:
                continue
            if send_to_server(api_url, api_key, "/api/sync/payment", payload):
                mark_synced(conn, "payments", id_col, row.get(id_col), synced_column)
                stats["payments"] += 1
            else:
                stats["errors"] += 1
        if rows:
            logger.info("Payments synced: %s", stats["payments"])

        # Optional: call flush to run engines (once per cycle if any data sent)
        if stats["clients"] + stats["contracts"] + stats["payments"] > 0:
            send_to_server(api_url, api_key, "/api/sync/flush", {}, retries=1)

    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return stats


def parse_branch_sources(
    db: Optional[str],
    branch: Optional[str],
    branch_access: Optional[List[str]] = None,
    branch_access_file: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Returns list like [{"branch":"Ф1","db":"C:\\f1.accdb"}, ...].
    Priority: --branch-access-file / --branch-access > --db (+ optional --branch).
    """
    sources: List[Dict[str, str]] = []
    if branch_access_file:
        with open(branch_access_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict):
            for b, path in payload.items():
                if b and path:
                    sources.append({"branch": str(b).strip(), "db": str(path).strip()})
        elif isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and item.get("branch") and item.get("db"):
                    sources.append({"branch": str(item["branch"]).strip(), "db": str(item["db"]).strip()})
    elif branch_access:
        for item in branch_access:
            # format: Ф1=C:\path\to\db.accdb
            if "=" not in item:
                continue
            b, path = item.split("=", 1)
            b = (b or "").strip()
            path = (path or "").strip()
            if b and path:
                sources.append({"branch": b, "db": path})
    elif db:
        sources.append({"branch": (branch or "").strip(), "db": db})
    return sources


def run_multi_sync_cycle(
    sources: List[Dict[str, str]],
    api_url: str,
    api_key: str,
    synced_column: str = "is_synced",
) -> Dict[str, int]:
    total = {"clients": 0, "contracts": 0, "payments": 0, "errors": 0}
    for src in sources:
        b = src.get("branch") or None
        db_path = src.get("db") or ""
        if not db_path:
            total["errors"] += 1
            continue
        logger.info("Sync source: branch=%s db=%s", b or "N/A", db_path)
        stats = run_sync_cycle(db_path, api_url, api_key, b, synced_column)
        for k in total:
            total[k] += int(stats.get(k, 0))
    return total


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="CCC Branch Sync Agent — send Access data to CCC server")
    p.add_argument("--db", required=False, help="Path to MS Access .accdb or .mdb file")
    p.add_argument("--api-url", required=True, help="CCC server base URL (e.g. http://server:5000)")
    p.add_argument("--api-key", required=True, help="X-API-Key for sync (from Developer Portal)")
    p.add_argument("--branch", default=None, help="Branch code to assign to contracts (e.g. Ф1)")
    p.add_argument("--branch-access", action="append", default=[],
                   help="Branch Access source mapping, repeatable: --branch-access Ф1=C:\\f1.accdb")
    p.add_argument("--branch-access-file", default=None,
                   help="JSON file with per-branch Access sources: {\"Ф1\":\"C:\\\\f1.accdb\",...} or list of {branch,db}")
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SEC, help="Sync interval in seconds (default 300)")
    p.add_argument("--once", action="store_true", help="Run once and exit (no loop)")
    p.add_argument("--synced-column", default="is_synced", help="Column name for synced flag in Access tables")
    args = p.parse_args()

    sources = parse_branch_sources(
        db=args.db,
        branch=args.branch,
        branch_access=args.branch_access,
        branch_access_file=args.branch_access_file,
    )
    if not sources:
        logger.error("No Access sources configured. Use --db or --branch-access/--branch-access-file")
        return 2
    logger.info("Sync agent started: sources=%s api=%s interval=%s", len(sources), args.api_url, args.interval)

    if args.once:
        stats = run_multi_sync_cycle(sources, args.api_url, args.api_key, args.synced_column)
        logger.info("Cycle done: %s", stats)
        return 0 if stats["errors"] == 0 else 1

    while True:
        try:
            stats = run_multi_sync_cycle(sources, args.api_url, args.api_key, args.synced_column)
            if stats["errors"]:
                logger.warning("Cycle had errors: %s", stats)
        except Exception as e:
            logger.exception("Cycle failed: %s", e)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main() if not sys.flags.interactive else 0)
