"""
Simple CSV export engine for CCC.

Используется для выгрузки:
- клиентов;
- договоров (портфеля);
- таблиц риска / аналитики;
- аудита.
"""

from __future__ import annotations

import csv
import io
from typing import Iterable, List

from flask import Response

from core.database import get_db


def _rows_to_csv(rows: Iterable[dict]) -> str:
    rows = list(rows)
    if not rows:
        return ""
    fieldnames: List[str] = list(rows[0].keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in r.items()})
    return buf.getvalue()


def export_clients_csv() -> Response:
    db = get_db()
    rows = db.execute(
        """
        SELECT id, full_name, gender, age, income_source, monthly_income,
               position, region, mfy, phone, passport, created_at
        FROM clients
        ORDER BY id
        """
    ).fetchall()
    db.close()
    csv_text = _rows_to_csv([dict(r) for r in rows])
    return Response(
        csv_text,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=clients.csv"},
    )


def export_contracts_csv() -> Response:
    db = get_db()
    rows = db.execute(
        """
        SELECT id, client_id, branch, product_type, contract_date,
               contract_term, interest_rate, status, status_detail,
               product_amount, advance_payment, monthly_payment,
               paid_amount, debt_amount, total_late_days
        FROM contracts
        ORDER BY id
        """
    ).fetchall()
    db.close()
    csv_text = _rows_to_csv([dict(r) for r in rows])
    return Response(
        csv_text,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=contracts.csv"},
    )


def export_audit_csv() -> Response:
    db = get_db()
    rows = db.execute(
        """
        SELECT id, created_at, user, action, entity, entity_id, details
        FROM audit_log
        ORDER BY id DESC
        LIMIT 5000
        """
    ).fetchall()
    db.close()
    csv_text = _rows_to_csv([dict(r) for r in rows])
    return Response(
        csv_text,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )

