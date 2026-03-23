"""
Import engine для CCC: автораспознавание типа файла, гибкое сопоставление колонок,
мини-анализ (агрегаты + превью) перед импортом.

Поддерживает: клиенты, договоры, платежи. CSV с заголовком, UTF‑8/CP1251.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.database import get_db


def safe_read_file(filepath: str, filename: str):
    """
    Читает Excel или CSV с автоподбором кодировок и разделителей.
    Возвращает (df, error). df — pandas DataFrame или None.
    """
    try:
        import pandas as pd  # type: ignore
    except ImportError:
        return None, "Требуется библиотека pandas"

    ext = os.path.splitext(filename)[1].lower()

    if ext in (".xls", ".xlsx", ".xlsm"):
        try:
            xl = pd.ExcelFile(filepath)
            sheets = xl.sheet_names
            frames = []
            for sheet in sheets:
                try:
                    df = pd.read_excel(filepath, sheet_name=sheet, header=0)
                    df["_sheet"] = sheet
                    frames.append(df)
                except Exception:
                    pass
            if frames:
                return pd.concat(frames, ignore_index=True), None
            return None, "Не удалось прочитать ни один лист Excel"
        except Exception as e:
            return None, f"Ошибка Excel: {e}"

    if ext == ".csv":
        encodings = ["utf-8-sig", "utf-8", "cp1251", "latin-1"]
        separators = [",", ";", "\t", "|"]
        for enc in encodings:
            for sep in separators:
                try:
                    df = pd.read_csv(
                        filepath,
                        encoding=enc,
                        sep=sep,
                        on_bad_lines="skip",
                        engine="python",
                        dtype=str,
                    )
                    if len(df.columns) > 2 and len(df) > 0:
                        return df, None
                except TypeError:
                    try:
                        df = pd.read_csv(
                            filepath,
                            encoding=enc,
                            sep=sep,
                            error_bad_lines=False,  # type: ignore
                            engine="python",
                            dtype=str,
                        )
                        if len(df.columns) > 2 and len(df) > 0:
                            return df, None
                    except Exception:
                        pass
                except Exception:
                    pass
        return None, "Не удалось прочитать CSV. Попробуйте сохранить файл как .xlsx"

    try:
        df = pd.read_excel(filepath)
        return df, None
    except Exception as e:
        return None, f"Неподдерживаемый формат: {ext}. Ошибка: {e}"


# Сигнатуры колонок для автораспознавания типа (нормализованные: нижний регистр, без пробелов)
CLIENT_SIGNATURES = [
    "fio", "full_name", "фио", "client_name", "name", "фам", "имя",
    "phone", "телефон", "phone_number", "region", "регион", "город", "city",
    "mfy", "махалла", "мфй", "mahalla", "age", "возраст", "income", "доход",
    "passport", "паспорт", "income_source", "должность", "position",
]
CONTRACT_SIGNATURES = [
    "client_id", "клиент", "client", "clientid", "id_клиент",
    "branch", "филиал", "filial", "product_amount", "сумма", "товар", "sum",
    "contract_date", "дата", "date", "contractdate", "срок", "term",
    "debt_amount", "долг", "debt", "paid_amount", "оплачено", "paid",
    "advance_payment", "первоначальный", "взнос", "monthly_payment", "ежемесяч",
    "interest_rate", "ставка", "rate", "status", "статус",
]
PAYMENT_SIGNATURES = [
    "contract_id", "договор", "contract", "contractid", "id_договор",
    "amount", "сумма", "оплата", "payment", "payment_date", "дата", "datepay",
    "paymentdate",
]


@dataclass
class ImportResult:
    rows_total: int
    rows_imported: int
    rows_skipped: int
    errors: List[str]
    detected_type: Optional[str] = None
    analysis: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict:
        d = {
            "rows_total": self.rows_total,
            "rows_imported": self.rows_imported,
            "rows_skipped": self.rows_skipped,
            "errors": self.errors,
        }
        if self.detected_type:
            d["detected_type"] = self.detected_type
        if self.analysis:
            d["analysis"] = self.analysis
        return d


def _open_csv(file_storage) -> Tuple[List[Dict[str, str]], List[str], List[str]]:
    """
    Читает CSV из Flask FileStorage.
    Возвращает (rows, columns, errors). columns — список исходных имён колонок.
    """
    errors: List[str] = []
    try:
        if hasattr(file_storage, "seek"):
            file_storage.seek(0)
        raw = file_storage.read()
        filename = getattr(file_storage, "filename", "") or ""
        lower_name = filename.lower()

        # JSON
        if lower_name.endswith(".json"):
            try:
                text = raw.decode("utf-8-sig")
                data = json.loads(text)
                if isinstance(data, dict):
                    # допускаем формат {"rows":[...]}
                    data = data.get("rows") or []
                if not isinstance(data, list):
                    return [], [], ["Ожидался JSON-массив объектов."]
                rows = [dict(r) for r in data]
                cols = list(rows[0].keys()) if rows else []
                return rows, cols, errors
            except Exception as e:
                return [], [], [f"Ошибка чтения JSON: {e}"]

        # Excel / CSV — через safe_read_file (устойчиво к кириллице и newline)
        if lower_name.endswith((".xlsx", ".xls", ".xlsm")) or lower_name.endswith(".csv"):
            fd, tmp_path = tempfile.mkstemp(suffix=os.path.splitext(filename)[1] or ".csv")
            try:
                os.write(fd, raw)
                os.close(fd)
                df, err = safe_read_file(tmp_path, filename or "file.csv")
                if err:
                    return [], [], [err]
                if df is not None and len(df) > 0:
                    df = df.astype(str).fillna("")
                    rows = df.to_dict(orient="records")
                    cols = list(df.columns)
                    return rows, [str(c) for c in cols], errors
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            return [], [], ["Не удалось прочитать файл (Excel/CSV)."]

        # CSV без расширения .csv — fallback через декодер
        for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                text = ""
        if not text or not text.strip():
            return [], [], ["Не удалось декодировать файл (ожидается UTF-8 / CP1251 / Excel / JSON)."]
        first_line = text.split("\n")[0] if "\n" in text else text
        sep = ";" if ";" in first_line and first_line.count(";") >= first_line.count(",") else ","
        f = io.StringIO(text)
        reader = csv.DictReader(f, delimiter=sep)
        columns = reader.fieldnames or []
        columns = [c.strip() if c else "" for c in columns]
        rows = []
        for row in reader:
            rows.append({(k or "").strip(): (v.strip() if v is not None else "") for k, v in row.items()})
        return rows, columns, errors
    except Exception as e:
        return [], [], [f"Ошибка чтения файла: {e}"]


def _norm(s: str) -> str:
    """Нормализация для сопоставления: нижний регистр, без пробелов и подчёркиваний."""
    if not s:
        return ""
    s = s.lower().strip().replace(" ", "").replace("\u00a0", "").replace("_", "")
    return re.sub(r"[^\w]", "", s)


def _norm_key_map(row: Dict[str, str]) -> Dict[str, str]:
    """Нормализованное имя колонки -> исходное имя."""
    out: Dict[str, str] = {}
    for k in row.keys():
        n = _norm(k)
        if n:
            out[n] = k
    return out


def _score_columns(normalized_columns: List[str], signatures: List[str]) -> int:
    """Сколько сигнатур совпало с колонками (по вхождению подстроки или точному совпадению)."""
    score = 0
    for col in normalized_columns:
        for sig in signatures:
            if sig in col or col in sig or _norm(sig) == col:
                score += 1
                break
    return score


def detect_file_type(rows: List[Dict[str, str]], columns: List[str]) -> str:
    """
    Автораспознавание типа: clients | contracts | payments | unknown.
    """
    if not rows or not columns:
        return "unknown"
    norm_cols = [_norm(c) for c in columns if _norm(c)]
    s_cli = _score_columns(norm_cols, CLIENT_SIGNATURES)
    s_con = _score_columns(norm_cols, CONTRACT_SIGNATURES)
    s_pay = _score_columns(norm_cols, PAYMENT_SIGNATURES)
    if s_pay >= 2 and (s_pay >= s_con and s_pay >= s_cli):
        return "payments"
    if s_con >= 2 and s_con >= s_cli:
        return "contracts"
    if s_cli >= 2:
        return "clients"
    if s_con >= 1:
        return "contracts"
    if s_cli >= 1:
        return "clients"
    return "unknown"


def _safe_float(v: Any) -> float:
    if v is None or v == "":
        return 0.0
    try:
        s = str(v).replace(",", ".").replace("\u00a0", "")
        return float(re.sub(r"[^\d.\-]", "", s) or 0)
    except Exception:
        return 0.0


def _safe_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).replace(",", ".").split(".")[0]))
    except Exception:
        return None


def analyze_file(rows: List[Dict[str, str]], columns: List[str], detected_type: str) -> Dict[str, Any]:
    """
    Мини-анализ: агрегаты и превью по типу данных.
    """
    if not rows:
        return {
            "columns": columns,
            "row_count": 0,
            "sample": [],
            "aggregates": {},
            "detected_type": detected_type,
        }
    key_map = _norm_key_map(rows[0])

    def g(row: Dict[str, str], *names: str) -> str:
        for n in names:
            for norm, orig in key_map.items():
                if _norm(n) == norm or n in norm or norm in _norm(n):
                    return row.get(orig, "").strip()
        return ""

    sample = []
    for i, row in enumerate(rows[:10]):
        sample.append({orig: row.get(orig, "")[:50] for _, orig in key_map.items()})

    aggregates: Dict[str, Any] = {
        "row_count": len(rows),
        "columns": columns,
        "column_count": len(columns),
    }

    if detected_type == "clients":
        with_region = sum(1 for r in rows if g(r, "region", "регион", "город"))
        with_phone = sum(1 for r in rows if g(r, "phone", "телефон"))
        aggregates["with_region"] = with_region
        aggregates["with_phone"] = with_phone
        aggregates["summary"] = f"Клиентов: {len(rows)}, с регионом: {with_region}, с телефоном: {with_phone}"

    elif detected_type == "contracts":
        sums: Dict[str, float] = defaultdict(float)
        by_branch: Dict[str, int] = defaultdict(int)
        for r in rows:
            branch = g(r, "branch", "филиал") or "—"
            by_branch[branch] += 1
            sums["product_amount"] += _safe_float(g(r, "product_amount", "сумма", "товар"))
            sums["debt_amount"] += _safe_float(g(r, "debt_amount", "долг"))
            sums["paid_amount"] += _safe_float(g(r, "paid_amount", "оплачено"))
        aggregates["by_branch"] = dict(by_branch)
        aggregates["total_product_amount"] = round(sums["product_amount"], 2)
        aggregates["total_debt"] = round(sums["debt_amount"], 2)
        aggregates["total_paid"] = round(sums["paid_amount"], 2)
        aggregates["summary"] = (
            f"Договоров: {len(rows)}, портфель: {sums['product_amount']:,.0f}, "
            f"долг: {sums['debt_amount']:,.0f}, оплачено: {sums['paid_amount']:,.0f}"
        )

    elif detected_type == "payments":
        total_amount = sum(_safe_float(g(r, "amount", "сумма", "оплата")) for r in rows)
        by_contract = defaultdict(float)
        for r in rows:
            cid = g(r, "contract_id", "договор", "contract")
            if cid:
                by_contract[cid] += _safe_float(g(r, "amount", "сумма", "оплата"))
        aggregates["total_amount"] = round(total_amount, 2)
        aggregates["unique_contracts"] = len(by_contract)
        aggregates["summary"] = f"Платежей: {len(rows)}, сумма: {total_amount:,.0f}, договоров: {len(by_contract)}"

    else:
        aggregates["summary"] = f"Строк: {len(rows)}. Тип не определён — выберите тип вручную или добавьте заголовки."

    return {
        "columns": columns,
        "row_count": len(rows),
        "sample": sample,
        "aggregates": aggregates,
        "detected_type": detected_type,
    }


def analyze_upload(file_storage) -> Dict[str, Any]:
    """
    Только анализ: прочитать файл, определить тип, вернуть мини-дашборд.
    Не импортирует данные.
    """
    rows, columns, errors = _open_csv(file_storage)
    if errors:
        return {"ok": False, "error": "; ".join(errors), "detected_type": "unknown"}
    if not rows:
        return {"ok": True, "detected_type": "unknown", "analysis": analyze_file([], columns, "unknown")}
    detected = detect_file_type(rows, columns)
    analysis = analyze_file(rows, columns, detected)
    return {"ok": True, "detected_type": detected, "analysis": analysis}


def _get_value(row: Dict[str, str], key_map: Dict[str, str], *aliases: str) -> str:
    for a in aliases:
        n = _norm(a)
        for norm, orig in key_map.items():
            if n == norm or n in norm or norm in n:
                return row.get(orig, "").strip()
    return ""


def import_clients_file(file_storage) -> Dict:
    rows, _, errors = _open_csv(file_storage)
    if not rows:
        return ImportResult(0, 0, 0, errors or ["Нет данных"]).to_dict()

    db = get_db()
    imported = skipped = 0
    key_map = _norm_key_map(rows[0])

    clients_aliases = [
        ("full_name", "fio", "full_name", "фио", "client_name", "name"),
        ("gender", "gender", "пол"),
        ("age", "age", "возраст"),
        ("income_source", "income_source", "источникдохода"),
        ("monthly_income", "monthly_income", "доход", "ойлик"),
        ("position", "position", "должность"),
        ("region", "region", "регион", "город"),
        ("mfy", "mfy", "махалла", "мфй"),
        ("phone", "phone", "телефон"),
        ("passport", "passport", "паспорт"),
    ]

    for row in rows:
        full_name = _get_value(row, key_map, "fio", "full_name", "фио", "client_name", "name")
        if not full_name:
            skipped += 1
            continue
        try:
            vals = {"full_name": full_name}
            for col, *als in clients_aliases:
                if col == "full_name":
                    continue
                v = _get_value(row, key_map, *als)
                if col == "age":
                    vals[col] = _safe_int(v)
                elif col == "monthly_income":
                    vals[col] = _safe_float(v) or 0
                else:
                    vals[col] = v or None
            db.execute(
                """
                INSERT INTO clients (
                    full_name, gender, age, income_source, monthly_income,
                    position, region, mfy, phone, passport, created_at, updated_at
                ) VALUES (
                    :full_name, :gender, :age, :income_source, :monthly_income,
                    :position, :region, :mfy, :phone, :passport,
                    datetime('now'), datetime('now')
                )
                """,
                {
                    "full_name": vals["full_name"],
                    "gender": vals.get("gender"),
                    "age": vals.get("age"),
                    "income_source": vals.get("income_source") or "",
                    "monthly_income": vals.get("monthly_income", 0),
                    "position": vals.get("position") or "",
                    "region": vals.get("region") or "",
                    "mfy": vals.get("mfy") or "",
                    "phone": vals.get("phone") or "",
                    "passport": vals.get("passport") or "",
                },
            )
            imported += 1
        except Exception as e:
            errors.append(f"{full_name}: {e}")
            skipped += 1

    db.commit()
    db.close()
    analysis = analyze_file(rows, list(rows[0].keys()), "clients") if rows else {}
    return ImportResult(len(rows), imported, skipped, errors, "clients", analysis).to_dict()


def import_contracts_file(file_storage) -> Dict:
    rows, _, errors = _open_csv(file_storage)
    if not rows:
        return ImportResult(0, 0, 0, errors or ["Нет данных"]).to_dict()

    db = get_db()
    imported = skipped = 0
    key_map = _norm_key_map(rows[0])

    for row in rows:
        client_id = _get_value(row, key_map, "client_id", "клиент", "client", "clientid")
        if not client_id:
            skipped += 1
            continue
        try:
            product_amount = _safe_float(_get_value(row, key_map, "product_amount", "сумма", "товар", "sum"))
            db.execute(
                """
                INSERT INTO contracts (
                    client_id, branch, product_type, contract_date,
                    contract_term, interest_rate, status, status_detail,
                    product_amount, advance_payment, monthly_payment,
                    paid_amount, debt_amount
                ) VALUES (
                    :client_id, :branch, :product_type, :contract_date,
                    :contract_term, :interest_rate, :status, :status_detail,
                    :product_amount, :advance_payment, :monthly_payment,
                    :paid_amount, :debt_amount
                )
                """,
                {
                    "client_id": int(_safe_float(client_id) or 0),
                    "branch": _get_value(row, key_map, "branch", "филиал"),
                    "product_type": _get_value(row, key_map, "product_type", "продукт") or None,
                    "contract_date": _get_value(row, key_map, "contract_date", "дата", "datestart") or None,
                    "contract_term": _safe_int(_get_value(row, key_map, "contract_term", "срок")),
                    "interest_rate": _safe_float(_get_value(row, key_map, "interest_rate", "ставка")) or None,
                    "status": _get_value(row, key_map, "status", "статус"),
                    "status_detail": _get_value(row, key_map, "status_detail", "статусдетально"),
                    "product_amount": product_amount,
                    "advance_payment": _safe_float(_get_value(row, key_map, "advance_payment", "первоначальныйвзнос")),
                    "monthly_payment": _safe_float(_get_value(row, key_map, "monthly_payment", "ежемесячныйплатеж")),
                    "paid_amount": _safe_float(_get_value(row, key_map, "paid_amount", "оплачено")),
                    "debt_amount": _safe_float(_get_value(row, key_map, "debt_amount", "долг")),
                },
            )
            imported += 1
        except Exception as e:
            errors.append(f"client_id={client_id}: {e}")
            skipped += 1

    db.commit()
    db.close()
    analysis = analyze_file(rows, list(rows[0].keys()), "contracts") if rows else {}
    return ImportResult(len(rows), imported, skipped, errors, "contracts", analysis).to_dict()


def import_payments_file(file_storage) -> Dict:
    rows, _, errors = _open_csv(file_storage)
    if not rows:
        return ImportResult(0, 0, 0, errors or ["Нет данных"]).to_dict()

    db = get_db()
    imported = skipped = 0
    key_map = _norm_key_map(rows[0])

    for row in rows:
        contract_id = _get_value(row, key_map, "contract_id", "договор", "contract", "contractid")
        if not contract_id:
            skipped += 1
            continue
        try:
            db.execute(
                """
                INSERT INTO payments (
                    contract_id, payment_date, amount, created_at
                ) VALUES (
                    :contract_id, :payment_date, :amount, datetime('now')
                )
                """,
                {
                    "contract_id": int(_safe_float(contract_id) or 0),
                    "payment_date": _get_value(row, key_map, "payment_date", "дата", "datepay") or None,
                    "amount": _safe_float(_get_value(row, key_map, "amount", "сумма", "оплата")),
                },
            )
            imported += 1
        except Exception as e:
            errors.append(f"contract_id={contract_id}: {e}")
            skipped += 1

    db.commit()
    db.close()
    analysis = analyze_file(rows, list(rows[0].keys()), "payments") if rows else {}
    return ImportResult(len(rows), imported, skipped, errors, "payments", analysis).to_dict()


def import_auto(file_storage, force_type: Optional[str] = None) -> Dict:
    """
    Автораспознавание типа файла и импорт. Если force_type задан (clients|contracts|payments),
    используется он; иначе — detect_file_type.
    """
    rows, columns, errors = _open_csv(file_storage)
    if errors:
        return {"ok": False, "error": "; ".join(errors)}
    if not rows:
        return {"ok": False, "error": "Файл пуст или без строк данных."}
    detected = force_type or detect_file_type(rows, columns)
    if detected == "clients":
        return import_clients_file(file_storage)
    if detected == "contracts":
        return import_contracts_file(file_storage)
    if detected == "payments":
        return import_payments_file(file_storage)
    return {"ok": False, "error": "Тип данных не распознан. Добавьте заголовки или выберите тип вручную.", "detected_type": "unknown"}


def import_unified(file_storage, branch: str = "Ф1") -> Dict[str, Any]:
    """
    Импорт через унификатор: извлекает данные из любых колонок (разные форматы/стили),
    сопоставляет с БД по паспорту/ПИНФЛ/ФИО, объединяет без дубликатов.
    branch — филиал для договоров (Ф1–Ф7).
    """
    try:
        from core.data_unifier import (
            build_column_map,
            row_to_dict,
            unify_and_import_row,
            CANONICAL_CLIENT_COLUMNS,
            CANONICAL_CONTRACT_COLUMNS,
        )
    except ImportError as e:
        return {"ok": False, "error": str(e), "imported_clients": 0, "imported_contracts": 0, "updated_clients": 0, "updated_contracts": 0}

    rows, columns, errors = _open_csv(file_storage)
    if errors:
        return {"ok": False, "error": "; ".join(errors), "imported_clients": 0, "imported_contracts": 0, "updated_clients": 0, "updated_contracts": 0}
    if not rows or not columns:
        return {"ok": False, "error": "Файл пуст или без заголовков.", "imported_clients": 0, "imported_contracts": 0, "updated_clients": 0, "updated_contracts": 0}

    client_col_map = build_column_map(columns, CANONICAL_CLIENT_COLUMNS)
    contract_col_map = build_column_map(columns, CANONICAL_CONTRACT_COLUMNS)

    if not client_col_map.get("full_name") and not client_col_map.get("kod_mijoz"):
        return {"ok": False, "error": "Не найдена колонка с ФИО или кодом клиента.", "imported_clients": 0, "imported_contracts": 0, "updated_clients": 0, "updated_contracts": 0}

    db = get_db()
    ci, cu, ki, ku = 0, 0, 0, 0
    try:
        for row in rows:
            row_dict = dict(row) if isinstance(row, dict) else dict(zip(columns, row)) if hasattr(row, "__iter__") else {}
            if not row_dict:
                continue
            try:
                _, _, ca, cta = unify_and_import_row(db, row_dict, client_col_map, contract_col_map, branch, datemode=0)
                if ca == "inserted":
                    ci += 1
                elif ca == "updated":
                    cu += 1
                if cta == "inserted":
                    ki += 1
                elif cta == "updated":
                    ku += 1
            except Exception:
                continue
        db.commit()
        return {"ok": True, "imported_clients": ci, "imported_contracts": ki, "updated_clients": cu, "updated_contracts": ku}
    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e), "imported_clients": ci, "imported_contracts": ki, "updated_clients": cu, "updated_contracts": ku}
    finally:
        db.close()


# ═══════════════════════════════════════
#  ИМПОРТ СПРАВОЧНИКА МФЙ ИЗ EXCEL
# ═══════════════════════════════════════

def import_mfy_from_excel(filepath: str, filename: str, region_name: str) -> Dict[str, Any]:
    """
    Загружает названия МФЙ из Excel (.xls/.xlsx).
    - Читает все листы; колонка с названием МФЙ: заголовок «МФЙ номи» или первая колонка.
    - Привязывает записи к региону region_name (если нет в geo_regions — создаётся).
    Возвращает: {ok, imported, skipped, error?}.
    """
    try:
        import pandas as pd  # type: ignore
    except ImportError:
        return {"ok": False, "error": "Требуется pandas. pip install pandas openpyxl xlrd", "imported": 0, "skipped": 0}

    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".xls", ".xlsx", ".xlsm"):
        return {"ok": False, "error": "Поддерживаются только Excel: .xls, .xlsx", "imported": 0, "skipped": 0}

    try:
        xl = pd.ExcelFile(filepath)
    except Exception as e:
        return {"ok": False, "error": f"Не удалось открыть Excel: {e}", "imported": 0, "skipped": 0}

    mfy_names: List[str] = []
    for sheet in xl.sheet_names:
        try:
            df = pd.read_excel(filepath, sheet_name=sheet, header=0)
            if df is None or len(df) == 0:
                continue
            df = df.astype(str).fillna("")
            # Колонка с названием МФЙ: заголовок содержит "мфй" или "номи", иначе первая колонка
            mfy_col = None
            for c in df.columns:
                c_str = (c or "").strip().lower()
                if "мфй" in c_str or "номи" in c_str:
                    mfy_col = c
                    break
            if mfy_col is None and len(df.columns) > 0:
                mfy_col = df.columns[0]
            if mfy_col is None:
                continue
            for v in df[mfy_col]:
                name = (v or "").strip()
                if name and name.lower() not in ("nan", "мфй номи", "мфй"):
                    mfy_names.append(name)
        except Exception:
            continue

    if not mfy_names:
        return {"ok": False, "error": "В файле не найдено ни одного названия МФЙ (колонка «МФЙ номи» или первая колонка).", "imported": 0, "skipped": 0}

    # Уникальные, сохраняя порядок
    seen: set = set()
    unique_mfy: List[str] = []
    for n in mfy_names:
        key = n.strip()
        if key and key not in seen:
            seen.add(key)
            unique_mfy.append(key)

    region_name = (region_name or "").strip()
    if not region_name:
        return {"ok": False, "error": "Укажите регион для привязки МФЙ.", "imported": 0, "skipped": 0}

    db = get_db()
    try:
        row = db.execute(
            "SELECT id FROM geo_regions WHERE TRIM(name) = TRIM(?)",
            (region_name,),
        ).fetchone()
        if not row:
            db.execute("INSERT INTO geo_regions (name) VALUES (?)", (region_name,))
            db.commit()
            row = db.execute(
                "SELECT id FROM geo_regions WHERE TRIM(name) = TRIM(?)",
                (region_name,),
            ).fetchone()
        if not row:
            db.close()
            return {"ok": False, "error": "Не удалось создать или найти регион.", "imported": 0, "skipped": 0}
        region_id = row["id"]

        imported = 0
        skipped = 0
        for name in unique_mfy:
            try:
                cur = db.execute(
                    "INSERT OR IGNORE INTO geo_mfy (region_id, name) VALUES (?, ?)",
                    (region_id, name),
                )
                if cur.rowcount and cur.rowcount > 0:
                    imported += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1
        db.commit()
        db.close()
        return {"ok": True, "imported": imported, "skipped": skipped, "total": len(unique_mfy)}
    except Exception as e:
        try:
            db.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e), "imported": 0, "skipped": 0}


# ═══════════════════════════════════════
#  ИМПОРТ БАЗЫ КЛИЕНТОВ (ПАСПОРТ, ПИНФЛ) ДЛЯ ОГРАНИЧЕНИЙ/ПООЩРЕНИЙ В СКОРИНГЕ
# ═══════════════════════════════════════

def _detect_column(df_columns: List[str], *candidates: str) -> Optional[str]:
    """Найти колонку по списку возможных заголовков (нижний регистр, подстрока)."""
    norm = _norm
    for col in df_columns:
        nc = norm(str(col))
        for c in candidates:
            if c in nc or nc in c:
                return col
    return None


def import_client_base_from_excel(filepath: str, filename: str) -> Dict[str, Any]:
    """
    Загружает базу клиентов из Excel/CSV: паспорт, ПИНФЛ, ФИО, регион, МФЙ, тип (ограничение/поощрение).
    Колонки определяются по заголовкам: паспорт/серия/номер, пинфл, фио, регион, мфй, тип/ограничение/поощрение.
    Возвращает: {ok, imported, skipped, error?}.
    """
    try:
        import pandas as pd  # type: ignore
    except ImportError:
        return {"ok": False, "error": "Требуется pandas. pip install pandas openpyxl xlrd", "imported": 0, "skipped": 0}

    ext = os.path.splitext(filename)[1].lower()
    rows_list: List[Dict[str, str]] = []
    if ext in (".xls", ".xlsx", ".xlsm"):
        try:
            xl = pd.ExcelFile(filepath)
            for sheet in xl.sheet_names:
                try:
                    df = pd.read_excel(filepath, sheet_name=sheet, header=0)
                    if df is None or len(df) == 0:
                        continue
                    df = df.astype(str).fillna("")
                    cols = list(df.columns)
                    passport_col = _detect_column(cols, "паспорт", "passport")
                    series_col = _detect_column(cols, "серия", "series", "seriya")
                    number_col = _detect_column(cols, "номер", "number", "passport_number", "паспорт_раками", "pasport_raqami")
                    if not passport_col and not series_col:
                        passport_col = cols[0] if cols else None
                    pinfl_col = _detect_column(cols, "пинфл", "pinfl", "jshshir")
                    fio_col = _detect_column(cols, "фио", "фам", "имя", "fio", "full_name", "name")
                    region_col = _detect_column(cols, "регион", "region")
                    mfy_col = _detect_column(cols, "мфй", "махалля", "mfy")
                    limit_col = _detect_column(cols, "тип", "ограничение", "поощрение", "limit", "категория")
                    for _, r in df.iterrows():
                        from core.database import compose_passport, normalize_pinfl
                        passport_raw = (r.get(passport_col) or "").strip() if passport_col else ""
                        series_raw = (r.get(series_col) or "").strip() if series_col else ""
                        number_raw = (r.get(number_col) or "").strip() if number_col else ""
                        passport = compose_passport(series=series_raw, number=number_raw, passport=passport_raw) or ""
                        pinfl_raw = (r.get(pinfl_col) or "").strip()
                        pinfl = normalize_pinfl(pinfl_raw) or ""
                        full_name = (r.get(fio_col) or "").strip() if fio_col else ""
                        region = (r.get(region_col) or "").strip() if region_col else ""
                        mfy = (r.get(mfy_col) or "").strip() if mfy_col else ""
                        limit_val = (r.get(limit_col) or "").strip().lower() if limit_col else ""
                        if limit_val and ("огранич" in limit_val or "отказ" in limit_val or "restriction" in limit_val):
                            limit_type = "restriction"
                        elif limit_val and ("поощр" in limit_val or "одобр" in limit_val or "incentive" in limit_val):
                            limit_type = "incentive"
                        else:
                            limit_type = None
                        if passport or pinfl:
                            rows_list.append({
                                "passport": passport or None,
                                "pinfl": pinfl if len(pinfl) >= 9 else None,
                                "full_name": full_name or None,
                                "region": region or None,
                                "mfy": mfy or None,
                                "limit_type": limit_type,
                            })
                except Exception:
                    continue
        except Exception as e:
            return {"ok": False, "error": f"Ошибка чтения Excel: {e}", "imported": 0, "skipped": 0}
    elif ext == ".csv":
        df, err = safe_read_file(filepath, filename)
        if err or df is None or len(df) == 0:
            return {"ok": False, "error": err or "Файл пуст", "imported": 0, "skipped": 0}
        df = df.astype(str).fillna("")
        cols = list(df.columns)
        passport_col = _detect_column(cols, "паспорт", "passport")
        series_col = _detect_column(cols, "серия", "series", "seriya")
        number_col = _detect_column(cols, "номер", "number", "passport_number", "паспорт_раками", "pasport_raqami")
        if not passport_col and not series_col:
            passport_col = cols[0] if cols else None
        pinfl_col = _detect_column(cols, "пинфл", "pinfl", "jshshir")
        fio_col = _detect_column(cols, "фио", "фам", "name", "fio")
        region_col = _detect_column(cols, "регион", "region")
        mfy_col = _detect_column(cols, "мфй", "махалля", "mfy")
        limit_col = _detect_column(cols, "тип", "ограничение", "поощрение", "limit")
        for _, r in df.iterrows():
            from core.database import compose_passport, normalize_pinfl
            passport_raw = (r.get(passport_col) or "").strip() if passport_col else ""
            series_raw = (r.get(series_col) or "").strip() if series_col else ""
            number_raw = (r.get(number_col) or "").strip() if number_col else ""
            passport = compose_passport(series=series_raw, number=number_raw, passport=passport_raw) or ""
            pinfl_raw = (r.get(pinfl_col) or "").strip()
            pinfl = normalize_pinfl(pinfl_raw) or ""
            full_name = (r.get(fio_col) or "").strip() if fio_col else ""
            region = (r.get(region_col) or "").strip() if region_col else ""
            mfy = (r.get(mfy_col) or "").strip() if mfy_col else ""
            limit_val = (r.get(limit_col) or "").strip().lower() if limit_col else ""
            if limit_val and ("огранич" in limit_val or "отказ" in limit_val or "restriction" in limit_val):
                limit_type = "restriction"
            elif limit_val and ("поощр" in limit_val or "одобр" in limit_val or "incentive" in limit_val):
                limit_type = "incentive"
            else:
                limit_type = None
            if passport or pinfl:
                rows_list.append({
                    "passport": passport or None,
                    "pinfl": pinfl if len(pinfl) >= 9 else None,
                    "full_name": full_name or None,
                    "region": region or None,
                    "mfy": mfy or None,
                    "limit_type": limit_type,
                })
    else:
        return {"ok": False, "error": "Поддерживаются только .xls, .xlsx, .xlsm, .csv", "imported": 0, "skipped": 0}

    if not rows_list:
        return {"ok": False, "error": "Не найдено ни одной строки с паспортом или ПИНФЛ.", "imported": 0, "skipped": 0}

    imported_at = datetime.now().isoformat()
    db = get_db()
    imported = 0
    skipped = 0
    try:
        for row in rows_list:
            try:
                cur = db.execute(
                    """INSERT INTO client_base (passport, pinfl, full_name, region, mfy, limit_type, notes, imported_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row.get("passport"),
                        row.get("pinfl"),
                        row.get("full_name"),
                        row.get("region"),
                        row.get("mfy"),
                        row.get("limit_type"),
                        None,
                        imported_at,
                    ),
                )
                imported += 1
            except Exception:
                skipped += 1
        db.commit()
        db.close()
        return {"ok": True, "imported": imported, "skipped": skipped, "total": len(rows_list)}
    except Exception as e:
        try:
            db.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e), "imported": 0, "skipped": 0}


# ═══════════════════════════════════════
#  ИМПОРТ ФАЙЛА СКОРИНГА (КЛИЕНТЫ + ДОГОВОРЫ) — Ф1…Ф7, ДЛЯ АНАЛИТИКИ
# ═══════════════════════════════════════

def _norm_col(name: str) -> str:
    """Нормализация имени колонки: нижний регистр, пробелы в подчёркивания."""
    if not name:
        return ""
    return str(name).strip().lower().replace(" ", "_").replace("-", "_")


def _safe_float_val(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        s = str(val).strip().replace(",", ".").replace("\xa0", "").replace(" ", "")
        if not s or s.lower() == "nan":
            return 0.0
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _safe_int_val(val: Any) -> int | None:
    if val is None:
        return None
    try:
        s = str(val).strip()
        if not s or s.lower() == "nan":
            return None
        m = re.search(r"\d+", s)
        if m:
            return int(m.group())
        n = _safe_float_val(val)
        return int(n) if n == int(n) else None
    except (ValueError, TypeError):
        return None


def _date_str_val(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return None
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return s


def _age_from_birth(birth_str: str) -> Optional[int]:
    """Вычисляет возраст из даты рождения (YYYY-MM-DD или год)."""
    if not birth_str or not isinstance(birth_str, str):
        return None
    s = birth_str.strip()
    if not s or s.lower() == "nan":
        return None
    try:
        from datetime import date
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            y, m, d = int(s[:4]), int(s[5:7]), int(s[8:10])
            birth = date(y, m, d)
        else:
            m = re.search(r"(\d{4})", s)
            if not m:
                return None
            y = int(m.group(1))
            birth = date(y, 1, 1)
        today = date.today()
        age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
        return age if 1 <= age <= 120 else None
    except (ValueError, TypeError):
        return None


def import_scoring_full_excel(filepath: str, filename: str, branch: str) -> Dict[str, Any]:
    """
    Импорт полного файла скоринга: ФИО, Серия, Паспорт_раками, JSHSHIR (ПИНФЛ),
    Жинси, Yoshi, region, mfy, Daromad_mj, Лавозими, avtomobili, pl_kartasi,
    Шартнома санаси, Шарт_тугаш санаси, Шарт_муддати, foizstav, Статуси, Неча марта кечикган.
    Создаёт клиентов и договоры по филиалу (Ф1…Ф7). Статуси → status_detail для аналитики.
    """
    try:
        import pandas as pd  # type: ignore
    except ImportError:
        return {"ok": False, "error": "Требуется pandas. pip install pandas openpyxl xlrd", "imported_clients": 0, "imported_contracts": 0, "skipped": 0}

    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".xls", ".xlsx", ".xlsm"):
        return {"ok": False, "error": "Поддерживаются только Excel: .xls, .xlsx", "imported_clients": 0, "imported_contracts": 0, "skipped": 0}

    try:
        df = pd.read_excel(filepath, sheet_name=0, header=0)
    except Exception as e:
        return {"ok": False, "error": f"Ошибка чтения Excel: {e}", "imported_clients": 0, "imported_contracts": 0, "skipped": 0}
    if df is None or len(df) == 0:
        return {"ok": False, "error": "Файл пуст", "imported_clients": 0, "imported_contracts": 0, "skipped": 0}

    df = df.astype(str).fillna("")
    col_map = {_norm_col(c): c for c in df.columns}

    def get(row: Any, *keys: str) -> str:
        for k in keys:
            if k in col_map and col_map[k] in row.index:
                v = row.get(col_map[k], "")
                if v is not None and str(v).strip() and str(v).lower() != "nan":
                    return str(v).strip()
        return ""

    branch = (branch or "Ф1").strip()
    db = get_db()
    imported_clients = 0
    imported_contracts = 0
    skipped = 0

    for idx, row in df.iterrows():
        try:
            full_name = get(row, "фо", "фио", "full_name", "ф.и.о.")
            if not full_name:
                skipped += 1
                continue

            series = get(row, "серия", "series").replace(" ", "")
            num = get(row, "паспорт_раками", "номер", "passport_number", "number").replace(" ", "")
            passport = (series + num) if (series or num) else None
            pinfl_raw = get(row, "jshshir", "пинфл", "pinfl", "jsheshir")
            pinfl_clean = re.sub(r"[^\d]", "", pinfl_raw) if pinfl_raw else ""
            external_id = int(pinfl_clean) if len(pinfl_clean) >= 9 else None

            client_id = None
            if external_id:
                r = db.execute("SELECT id FROM clients WHERE external_id = ? LIMIT 1", (external_id,)).fetchone()
                if r:
                    client_id = r["id"]
            if not client_id and passport:
                r = db.execute(
                    "SELECT id FROM clients WHERE REPLACE(REPLACE(TRIM(COALESCE(passport,'')),' ',''),'-','') = ? LIMIT 1",
                    (re.sub(r"[^\w]", "", passport or ""),),
                ).fetchone()
                if r:
                    client_id = r["id"]
            if not client_id:
                r = db.execute("SELECT id FROM clients WHERE TRIM(full_name) = ? LIMIT 1", (full_name.strip(),)).fetchone()
                if r:
                    client_id = r["id"]

            if not client_id:
                age_val = _safe_int_val(get(row, "yoshi", "age", "возраст", "узол"))
                if age_val is None or age_val <= 0:
                    birth_str = get(row, "tugilgan_sana", "birth_date", "дата_рождения", "birth")
                    from_age = _age_from_birth(birth_str)
                    if from_age is not None:
                        age_val = from_age
                gender = get(row, "жинси", "gender", "пол") or None
                region_val = get(row, "region", "регион") or None
                mfy_val = get(row, "mfy", "мфй", "махалла") or None
                income_src = get(row, "daromad_mj", "income_source", "источник_дохода") or None
                position_val = get(row, "лавозими", "position", "должность") or None
                workplace_val = get(row, "иш_жойи", "workplace", "место_работы") or None
                avto = get(row, "avtomobili", "avtomobil").upper()
                has_car = 1 if "ИСТИНА" in avto or "TRUE" in avto or avto == "1" else 0
                pl_kart = get(row, "pl_kartasi", "pl_kartasi").upper()
                has_card = 1 if "ИСТИНА" in pl_kart or "TRUE" in pl_kart or pl_kart == "1" else 0
                phone_val = get(row, "telefon_raqami", "phone", "телефон") or None
                now_iso = datetime.now().isoformat()
                db.execute(
                    """INSERT INTO clients (full_name, passport, external_id, gender, age, region, mfy, income_source, position, workplace, has_car, has_card, phone, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (full_name, passport, external_id, gender, age_val, region_val, mfy_val, income_src, position_val, workplace_val, has_car, has_card, phone_val, now_iso, now_iso),
                )
                client_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()[0]
                imported_clients += 1
            else:
                pass

            contract_date = _date_str_val(get(row, "шартнома_санаси", "contract_date", "дата"))
            contract_end = _date_str_val(get(row, "шарт_тугаш_санаси", "contract_end_date"))
            term = _safe_int_val(get(row, "шарт_муддати", "contract_term", "срок")) or 12
            rate = _safe_float_val(get(row, "foizstav", "interest_rate", "ставка"))
            if rate <= 0:
                rate = 3.0
            status_class = get(row, "статуси", "status", "статус", "status_detail").strip()
            if "хши" in status_class.lower() or "яхши" in status_class.lower():
                status_detail = "Стандарт"
            elif "мон" in status_class.lower() or "ёмон" in status_class.lower():
                status_detail = "Ёмон"
            else:
                status_detail = status_class or "Стандарт"
            late_count = _safe_int_val(get(row, "неча_марта_кечикган", "late_count", "просрочек")) or 0
            last_pay = _date_str_val(get(row, "oxirgi_tulagan_sanasi", "last_payment_date", "oxirgi_tulagan"))
            product_amount = _safe_float_val(get(row, "summa", "jami_summa", "product_amount", "сумма"))
            debt_amount = _safe_float_val(get(row, "qoldiq", "qarz", "debt_amount", "долг"))
            paid_amount = _safe_float_val(get(row, "tolangan_summa", "paid_amount", "to'langan"))
            monthly_payment = _safe_float_val(get(row, "oylik_tolov", "monthly_payment"))
            now_iso = datetime.now().isoformat()
            db.execute(
                """INSERT INTO contracts (client_id, branch, contract_date, contract_end_date, contract_term, interest_rate, status_detail, late_count, total_late_days, last_payment_date, product_amount, debt_amount, paid_amount, monthly_payment, imported_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (client_id, branch, contract_date, contract_end, term, rate, status_detail, late_count, late_count * 30.0, last_pay, product_amount or 0, debt_amount or 0, paid_amount or 0, monthly_payment or 0, now_iso),
            )
            imported_contracts += 1
        except Exception:
            skipped += 1

    try:
        db.commit()
        try:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            try:
                db.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass
    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e), "imported_clients": imported_clients, "imported_contracts": imported_contracts, "skipped": skipped}
    finally:
        try:
            db.close()
        except Exception:
            pass
    return {"ok": True, "imported_clients": imported_clients, "imported_contracts": imported_contracts, "skipped": skipped}
