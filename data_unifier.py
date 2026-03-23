"""
CCC — Унификатор данных: извлечение из разных форматов и стилей в единый канон без дублирования.

Система:
1. Маппинг колонок — любые имена (кириллица, латиница, разные стили) → канонические поля
2. Извлечение — из строки с любыми колонками получаем канонический dict
3. Сопоставление — по паспорту/ПИНФЛ/коду+ФИО находим существующую запись
4. Объединение — UPDATE при совпадении, INSERT только при новой записи (без дубликатов)
"""

from __future__ import annotations

import re
from datetime import datetime
from itertools import zip_longest
from typing import Any, Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════════════════
#  КАНОНИЧЕСКИЕ ПОЛЯ → ВОЗМОЖНЫЕ ИМЕНА КОЛОНОК (разные форматы и стили)
# ═══════════════════════════════════════════════════════════════════════════

CANONICAL_CLIENT_COLUMNS: Dict[str, List[str]] = {
    "kod_mijoz": [
        "код мижоз", "кодмижоз", "код_мижоз", "kod_mijoz", "kodmijoz",
        "client_id", "clientid", "id_клиент", "idklient", "код клиента",
    ],
    "full_name": [
        "фо", "фио", "ф.и.о.", "full_name", "fullname", "client_name",
        "фамилия", "имя", "фамилияисм", "фамилия исм шариф", "name",
    ],
    "gender": ["жинси", "jins", "gender", "пол", "sex"],
    "age": ["yoshi", "yosh", "age", "возраст", "узол"],
    "passport_series": ["серия", "seriya", "series", "паспорт серия"],
    "passport_number": [
        "паспорт_раками", "паспорт_рақами", "pasport_raqami",
        "номер паспорта", "passport_number", "number",
    ],
    "pinfl": ["jshshir", "жшшир", "пинфл", "pinfl", "jsheshir"],
    "region": ["regioni", "region", "регион", "город", "city"],
    "mfy": ["mfy", "мфй", "махалла", "mahalla"],
    "income_source": ["daromad_mj", "daromad", "источник дохода", "income_source"],
    "position": ["лавозими", "lavozimi", "position", "должность"],
    "workplace": ["иш жойи", "иш_жойи", "ish_joyi", "workplace", "место работы"],
    "has_car": ["avtomobili", "avtomobil", "has_car", "автомобиль"],
    "has_card": ["pl_kartasi", "pl_kartasi", "pl_karta", "has_card", "карта"],
    "phone": ["telefon_raqami", "телефон", "phone", "tel"],
    "status": ["статус", "status", "status_klient"],
}

CANONICAL_CONTRACT_COLUMNS: Dict[str, List[str]] = {
    "shartnoma_kodi": [
        "шартнома коди", "шартномакоди", "шартнома_коди", "shartnoma_kodi",
        "contract_id", "contractid", "номер договора",
    ],
    "contract_date": [
        "шартнома санаси", "шартномасанаси", "contract_date", "дата договора",
        "contractdate", "дата",
    ],
    "contract_end_date": [
        "шарт_тугаш_санаси", "шарттугашсанаси", "contract_end_date",
        "дата окончания", "окончание",
    ],
    "contract_term": ["шарт_муддати", "шартмуддати", "contract_term", "срок", "term"],
    "interest_rate": ["foizstav", "foizstavka", "interest_rate", "ставка", "rate"],
    "product_amount": [
        "товар суммаси", "товарсуммаси", "product_amount", "сумма товара",
        "productamount", "сумма",
    ],
    "advance_payment": [
        "олдиндан тулов суммаси", "олдиндантулов", "advance_payment",
        "первоначальный взнос", "взнос",
    ],
    "monthly_payment": [
        "ойлик тулови", "oylik_tulov", "monthly_payment", "ежемесячный платёж",
    ],
    "paid_amount": [
        "тулаган суммаси", "tulagan_summasi", "paid_amount", "оплачено",
    ],
    "debt_amount": [
        "карз суммаси", "qarz_summasi", "debt_amount", "долг", "остаток",
    ],
    "overdue_amount": [
        "кечикансумма", "кечиккансумма", "kechikkan_summa", "overdue_amount",
        "просрочка",
    ],
    "status_detail": [
        "статуси", "statusy", "status_detail", "статус", "status",
    ],
    "late_count": [
        "неча марта кечикган", "necha_marta_kechikkan", "late_count",
        "просрочек", "количество просрочек",
    ],
    "total_late_days": [
        "jami kechikgan kun", "jamikechikgankun", "total_late_days",
        "всего дней просрочки",
    ],
    "last_payment_date": [
        "oxirgi tulagan sanasi", "oxirgitulagansanasi", "last_payment_date",
        "дата последней оплаты",
    ],
    "responsible_person": [
        "масул_шахс", "masul_shaxs", "responsible_person", "ответственный",
    ],
}


def _norm_col(name: str) -> str:
    """Нормализация имени колонки для сопоставления."""
    if not name:
        return ""
    s = str(name).lower().strip().replace(" ", "").replace("\u00a0", "").replace("_", "-")
    return re.sub(r"[^\w\u0400-\u04ff]", "", s)


def build_column_map(
    source_columns: List[str],
    canonical_spec: Dict[str, List[str]],
) -> Dict[str, str]:
    """
    Строит маппинг: каноническое_поле → исходное_имя_колонки.
    Исходные колонки могут быть в любом порядке и стиле.
    """
    norm_to_orig: Dict[str, str] = {}
    for col in source_columns:
        n = _norm_col(col)
        if n:
            norm_to_orig[n] = col

    result: Dict[str, str] = {}
    for canonical, variants in canonical_spec.items():
        for v in variants:
            nv = _norm_col(v)
            if nv in norm_to_orig:
                result[canonical] = norm_to_orig[nv]
                break
            for norm_key in norm_to_orig:
                if nv == norm_key or (len(nv) >= 3 and (nv in norm_key or norm_key in nv)):
                    result[canonical] = norm_to_orig[norm_key]
                    break
            if canonical in result:
                break
    return result


def row_to_dict(headers: List[str], values: List[Any]) -> Dict[str, Any]:
    """Преобразует строку (список значений) + заголовки в dict для извлечения."""
    if not headers:
        return {}
    return dict(zip_longest(headers, values[:len(headers)], fillvalue=None))


def _safe_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none"):
        return None
    if s.endswith(".0") and s[:-2].replace(".", "").isdigit():
        try:
            return str(int(float(s)))
        except Exception:
            pass
    return s


def _safe_float(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        s = str(val).replace(",", ".").replace("\xa0", "").replace(" ", "")
        return float(re.sub(r"[^\d.\-]", "", s) or 0)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        f = _safe_float(val)
        return int(f) if f == f else None
    except (ValueError, TypeError):
        return None


def _bool_val(val: Any) -> int:
    if isinstance(val, (int, float)):
        return 1 if val else 0
    s = str(val).strip().upper()
    return 1 if s in ("1", "TRUE", "ИСТИНА", "ДА", "YES") else 0


def _date_str(val: Any, datemode: int = 0) -> Optional[str]:
    if val is None:
        return None
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    try:
        import xlrd
        f = float(val)
        if f > 1000:
            dt = xlrd.xldate.xldate_as_datetime(f, datemode)
            return dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return None
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return s


def extract_canonical_client(
    row: Dict[str, Any],
    col_map: Dict[str, str],
    datemode: int = 0,
) -> Dict[str, Any]:
    """
    Извлекает канонический dict клиента из строки с любыми колонками.
    """
    def g(*keys: str) -> Any:
        for k in keys:
            if k in col_map and col_map[k] in row:
                return row.get(col_map[k])
        return None

    series = _safe_str(g("passport_series"))
    num = _safe_str(g("passport_number"))
    passport = None
    if series or num:
        from core.database import compose_passport
        passport = compose_passport(series=series, number=num)

    pinfl_raw = _safe_str(g("pinfl"))
    pinfl_int = None
    if pinfl_raw:
        digits = re.sub(r"[^\d]", "", pinfl_raw)
        if len(digits) >= 13:
            pinfl_int = int(digits)

    kod = _safe_int(g("kod_mijoz"))

    return {
        "kod_mijoz": kod,
        "full_name": _safe_str(g("full_name")),
        "gender": _safe_str(g("gender")),
        "age": _safe_int(g("age")),
        "passport": passport,
        "pinfl": pinfl_int,
        "region": _safe_str(g("region")),
        "mfy": _safe_str(g("mfy")),
        "income_source": _safe_str(g("income_source")),
        "position": _safe_str(g("position")),
        "workplace": _safe_str(g("workplace")),
        "has_car": _bool_val(g("has_car")),
        "has_card": _bool_val(g("has_card")),
        "phone": _safe_str(g("phone")),
        "status": _safe_str(g("status")),
    }


def extract_canonical_contract(
    row: Dict[str, Any],
    col_map: Dict[str, str],
    datemode: int = 0,
) -> Dict[str, Any]:
    """Извлекает канонический dict договора из строки."""
    def g(*keys: str) -> Any:
        for k in keys:
            if k in col_map and col_map[k] in row:
                return row.get(col_map[k])
        return None

    return {
        "shartnoma_kodi": _safe_int(g("shartnoma_kodi")),
        "contract_date": _date_str(g("contract_date"), datemode),
        "contract_end_date": _date_str(g("contract_end_date"), datemode),
        "contract_term": _safe_int(g("contract_term")),
        "interest_rate": _safe_float(g("interest_rate")),
        "product_amount": _safe_float(g("product_amount")),
        "advance_payment": _safe_float(g("advance_payment")),
        "monthly_payment": _safe_float(g("monthly_payment")),
        "paid_amount": _safe_float(g("paid_amount")),
        "debt_amount": _safe_float(g("debt_amount")),
        "overdue_amount": _safe_float(g("overdue_amount")),
        "status_detail": _safe_str(g("status_detail")),
        "late_count": _safe_int(g("late_count")) or 0,
        "total_late_days": _safe_float(g("total_late_days")),
        "last_payment_date": _date_str(g("last_payment_date"), datemode),
        "responsible_person": _safe_str(g("responsible_person")),
    }


def resolve_client_id(
    db,
    canonical: Dict[str, Any],
) -> Optional[int]:
    """
    Находит id существующего клиента по паспорту, ПИНФЛ или (код+ФИО).
    Возвращает client_id или None.
    """
    cur = db.cursor()

    if canonical.get("passport"):
        from core.database import normalize_passport
        norm = normalize_passport(canonical["passport"])
        if len(norm) >= 5:
            row = cur.execute(
                "SELECT id FROM clients WHERE REPLACE(REPLACE(UPPER(COALESCE(passport,'')),' ',''),'-','') = ? LIMIT 1",
                (norm,),
            ).fetchone()
            if row:
                return row[0]

    if canonical.get("pinfl") and canonical["pinfl"] > 100000:
        row = cur.execute(
            "SELECT id FROM clients WHERE external_id = ? LIMIT 1",
            (canonical["pinfl"],),
        ).fetchone()
        if row:
            return row[0]

    if canonical.get("kod_mijoz"):
        row = cur.execute(
            "SELECT id FROM clients WHERE external_id = ? LIMIT 1",
            (canonical["kod_mijoz"],),
        ).fetchone()
        if row:
            return row[0]

    fio = (canonical.get("full_name") or "").strip()
    if fio:
        row = cur.execute(
            "SELECT id FROM clients WHERE TRIM(full_name) = ? LIMIT 1",
            (fio,),
        ).fetchone()
        if row:
            return row[0]

    return None


def resolve_contract_id(
    db,
    client_id: int,
    shartnoma_kodi: Optional[int],
    branch: str,
) -> Optional[int]:
    """Находит id существующего договора по client_id + external_id + branch."""
    if not shartnoma_kodi:
        return None
    row = db.execute(
        "SELECT id FROM contracts WHERE client_id = ? AND external_id = ? AND branch = ? LIMIT 1",
        (client_id, shartnoma_kodi, branch),
    ).fetchone()
    return row[0] if row else None


def upsert_client(
    db,
    canonical: Dict[str, Any],
    branch: Optional[str] = None,
) -> Tuple[int, str]:
    """
    Вставляет или обновляет клиента. Без дубликатов.
    Возвращает (client_id, "inserted"|"updated").
    """
    now = datetime.now().isoformat()
    existing_id = resolve_client_id(db, canonical)

    external_id = canonical.get("pinfl") or canonical.get("kod_mijoz")

    if existing_id:
        db.execute(
            """
            UPDATE clients SET
                full_name = COALESCE(?, full_name),
                gender = COALESCE(?, gender),
                age = COALESCE(?, age),
                passport = COALESCE(?, passport),
                external_id = COALESCE(?, external_id),
                region = COALESCE(?, region),
                mfy = COALESCE(?, mfy),
                income_source = COALESCE(?, income_source),
                position = COALESCE(?, position),
                workplace = COALESCE(?, workplace),
                has_car = COALESCE(?, has_car),
                has_card = COALESCE(?, has_card),
                phone = COALESCE(?, phone),
                updated_at = ?
            WHERE id = ?
            """,
            (
                canonical.get("full_name"),
                canonical.get("gender"),
                canonical.get("age"),
                canonical.get("passport"),
                external_id,
                canonical.get("region"),
                canonical.get("mfy"),
                canonical.get("income_source"),
                canonical.get("position"),
                canonical.get("workplace"),
                canonical.get("has_car"),
                canonical.get("has_card"),
                canonical.get("phone"),
                now,
                existing_id,
            ),
        )
        return existing_id, "updated"

    db.execute(
        """
        INSERT INTO clients (
            external_id, full_name, gender, age, passport,
            region, mfy, income_source, position, workplace,
            has_car, has_card, phone, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            external_id,
            canonical.get("full_name") or "",
            canonical.get("gender"),
            canonical.get("age"),
            canonical.get("passport"),
            canonical.get("region"),
            canonical.get("mfy"),
            canonical.get("income_source"),
            canonical.get("position"),
            canonical.get("workplace"),
            canonical.get("has_car", 0),
            canonical.get("has_card", 0),
            canonical.get("phone"),
            now,
            now,
        ),
    )
    return db.execute("SELECT last_insert_rowid()").fetchone()[0], "inserted"


def upsert_contract(
    db,
    client_id: int,
    canonical: Dict[str, Any],
    branch: str,
) -> Tuple[int, str]:
    """
    Вставляет или обновляет договор. Без дубликатов.
    Возвращает (contract_id, "inserted"|"updated").
    """
    now = datetime.now().isoformat()
    shart = canonical.get("shartnoma_kodi")
    existing_id = resolve_contract_id(db, client_id, shart, branch)

    rate = canonical.get("interest_rate") or 0
    if rate <= 0:
        rate = 3.0

    if existing_id:
        db.execute(
            """
            UPDATE contracts SET
                product_amount = COALESCE(?, product_amount),
                advance_payment = COALESCE(?, advance_payment),
                monthly_payment = COALESCE(?, monthly_payment),
                paid_amount = COALESCE(?, paid_amount),
                debt_amount = COALESCE(?, debt_amount),
                overdue_amount = COALESCE(?, overdue_amount),
                status_detail = COALESCE(?, status_detail),
                late_count = COALESCE(?, late_count),
                total_late_days = COALESCE(?, total_late_days),
                last_payment_date = ?,
                responsible_person = ?,
                contract_date = COALESCE(?, contract_date),
                contract_end_date = COALESCE(?, contract_end_date),
                contract_term = COALESCE(?, contract_term),
                interest_rate = ?,
                imported_at = ?
            WHERE id = ?
            """,
            (
                canonical.get("product_amount"),
                canonical.get("advance_payment"),
                canonical.get("monthly_payment"),
                canonical.get("paid_amount"),
                canonical.get("debt_amount"),
                canonical.get("overdue_amount"),
                canonical.get("status_detail"),
                canonical.get("late_count"),
                canonical.get("total_late_days"),
                canonical.get("last_payment_date"),
                canonical.get("responsible_person"),
                canonical.get("contract_date"),
                canonical.get("contract_end_date"),
                canonical.get("contract_term"),
                rate,
                now,
                existing_id,
            ),
        )
        return existing_id, "updated"

    db.execute(
        """
        INSERT INTO contracts (
            client_id, external_id, branch,
            contract_date, contract_end_date, contract_term, interest_rate,
            product_amount, advance_payment, monthly_payment,
            paid_amount, debt_amount, overdue_amount,
            status_detail, late_count, total_late_days, last_payment_date,
            responsible_person, imported_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            client_id,
            shart,
            branch,
            canonical.get("contract_date"),
            canonical.get("contract_end_date"),
            canonical.get("contract_term"),
            rate,
            canonical.get("product_amount") or 0,
            canonical.get("advance_payment") or 0,
            canonical.get("monthly_payment") or 0,
            canonical.get("paid_amount") or 0,
            canonical.get("debt_amount") or 0,
            canonical.get("overdue_amount") or 0,
            canonical.get("status_detail"),
            canonical.get("late_count") or 0,
            canonical.get("total_late_days") or 0,
            canonical.get("last_payment_date"),
            canonical.get("responsible_person"),
            now,
        ),
    )
    return db.execute("SELECT last_insert_rowid()").fetchone()[0], "inserted"


def unify_and_import_row(
    db,
    row: Dict[str, Any],
    client_col_map: Dict[str, str],
    contract_col_map: Dict[str, str],
    branch: str,
    datemode: int = 0,
) -> Tuple[Optional[int], Optional[int], str, str]:
    """
    Унифицирует строку (клиент+договор), сопоставляет с БД, вставляет/обновляет без дубликатов.
    Возвращает (client_id, contract_id, client_action, contract_action).
    """
    client_canon = extract_canonical_client(row, client_col_map, datemode)
    contract_canon = extract_canonical_contract(row, contract_col_map, datemode)

    if not client_canon.get("full_name"):
        return None, None, "skipped", "skipped"

    client_id, client_action = upsert_client(db, client_canon, branch)
    if not contract_canon.get("shartnoma_kodi"):
        return client_id, None, client_action, "skipped"

    contract_id, contract_action = upsert_contract(db, client_id, contract_canon, branch)
    return client_id, contract_id, client_action, contract_action
