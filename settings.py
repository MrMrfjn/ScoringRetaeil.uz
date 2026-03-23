"""
CreditControlCenter FINAL — Конфигурация
Production-ready settings. Пути читаются из окружения (Docker/env) с fallback на in-repo.
"""
import os, hashlib

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Пути: из env для деплоя (Docker), иначе — относительно проекта
def _path_from_env(key: str, default: str) -> str:
    return os.environ.get(key, default).strip() or default

_default_dop = os.path.join(BASE_DIR, "data_raw", "Доп.база")
# Основная БД: при наличии data_raw/Full DB.db — используем её, иначе credit_control.db
_full_db = os.path.join(BASE_DIR, "data_raw", "Full DB.db")
_default_db = _full_db if os.path.isfile(_full_db) else os.path.join(BASE_DIR, "credit_control.db")

DB_PATH = _path_from_env("DB_PATH", _default_db)
DATA_RAW_DOP_BASE = _path_from_env("DATA_RAW_DOP_BASE", _default_dop)
UPLOAD_DIR = _path_from_env("UPLOAD_DIR", os.path.join(DATA_RAW_DOP_BASE, "uploads"))
EXPORT_DIR = _path_from_env("EXPORT_DIR", os.path.join(DATA_RAW_DOP_BASE, "exports"))

ANALYTICS_DATA_DIR = _path_from_env(
    "ANALYTICS_DATA_DIR",
    r"C:\Users\Baraka\Desktop\Аналитика\Умумий мижозлар анализи",
)

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)
os.makedirs(DATA_RAW_DOP_BASE, exist_ok=True)

# Режим: development | production (влияет на SECRET_KEY, cookie secure, debug)
FLASK_ENV = os.environ.get("FLASK_ENV", "development").strip().lower()
if FLASK_ENV not in ("development", "production"):
    FLASK_ENV = "development"

# Импорт: разрешённые расширения и макс. размер (МБ)
ALLOWED_IMPORT_EXTENSIONS = (".csv", ".xlsx", ".xls", ".xlsm", ".txt")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))

# ─── STRICT INTEREST RATE BOUNDS ───
MIN_RATE = 2.8
MAX_RATE = 4.0
RATE_MAP = {
    "A": 2.8,
    "B": 3.0,
    "C": 3.3,  # standard
    "D": 3.6,
    "E": 4.0,
}

# ─── BRANCHES ───
BRANCHES = {
    "Ф1": "Коканд А", "Ф2": "Коканд Б", "Ф3": "Фаргона",
    "Ф4": "Ташкент А", "Ф5": "Ташкент Б",
    "Ф6": "Андижон А", "Ф7": "Андижон Б",
}

# ─── ROLES: единый источник из core.rbac (OPERATIONAL_ROLES + алиасы) ───
try:
    from core.rbac import get_extended_roles_for_settings
    ROLES = get_extended_roles_for_settings()
except Exception:
    ROLES = {
        "executive": {"label": "Директор", "modules": ["dashboard","import","scoring","portfolio","alerts","employees","export","admin","intelligence","developer"]},
        "director": {"label": "Директор", "modules": ["dashboard","import","scoring","portfolio","alerts","employees","export","admin","intelligence","developer"]},
        "branch_manager": {"label": "Менеджер филиала", "modules": ["dashboard","import","scoring","portfolio","alerts","employees","export"]},
        "credit_officer": {"label": "Сотрудник скоринга", "modules": ["scoring","portfolio"]},
        "analyst": {"label": "Аналитик", "modules": ["dashboard","scoring","portfolio","alerts","export","intelligence"]},
        "branch_employee": {"label": "Сотрудник", "modules": ["scoring"]},
    }

# ─── SCORING CLASSES ───
SCORING_CLASSES = {
    "A": {"min": 55, "max": 999, "label": "Одобрено",          "rate": 2.8, "color": "#22c55e"},
    "B": {"min": 42, "max": 54,  "label": "Одобрено",          "rate": 3.0, "color": "#5cb85c"},
    "C": {"min": 30, "max": 41,  "label": "Доп. проверка",     "rate": 3.3, "color": "#f0ad4e"},
    "D": {"min": 15, "max": 29,  "label": "На рассмотрение",   "rate": 3.6, "color": "#ff9800"},
    "E": {"min": -999,"max": 14, "label": "Отказ",             "rate": 4.0, "color": "#ef4444"},
}

PENSION_BONUS = 8

# ─── INCOME/EMPLOYMENT LISTS ───
INCOME_SOURCES = [
    "Давлат ташкилоти", "Нафақачи (Пенсионер)", "МЧЖ (ООО)",
    "АЖ (Акциядорлик Жамияти)", "ЯТТ (Якка Тартибтаги Тадбиркор)",
    "Тижорат ташкилоти", "Хусусий даромад", "Қишлоқ хўжалиги",
    "Фермер", "Қуроллик кучлар (Ҳарбий)", "Стипендия", "Дивиденды",
    "Аренда (ижара)", "Ишсиз", "Бошқа",
]

EMPLOYMENT_TYPES = [
    "Пенсионер", "Тадбиркор", "Укитувчи", "Ишчи", "Хамшира", "Хайдовчи",
    "Уй бекаси", "Тикувчи", "Тарбиячи", "Савдогар", "Уста", "Сотувчи",
    "Бухгалтер", "Фермер", "Мухандис (инженер)", "Директор", "Менеджер",
    "Врач / Шифокор", "Юрист", "Дастурчи (IT)", "Ҳарбий хизматчи",
    "Талаба", "Бошқа",
]

NPL_STATUSES = ["Ёмон", "МИБ", "Судда"]

# ─── ПОРОГИ АЛЕРТОВ (смягчены на ~30% по системе) ───
# Используются в main.generate_alerts и engines.analytics_engine.run_alerts
ALERT_SOFTEN = 1.3
ALERT_NEAR_MIB_MIN = 2
ALERT_NEW_NPL_DAYS = 7
ALERT_NEW_NPL_MIN = 2
ALERT_EMPLOYEE_NPL_PCT = 19.5
ALERT_EMPLOYEE_MIN_CONTRACTS = 26
ALERT_EARLY_DEFAULT_MIN = 4
ALERT_BRANCH_NPL_PCT = 10.4
ALERT_BRANCH_MIN_CONTRACTS = 130
ALERT_EARLY_DELAY_MIN = 65
ALERT_BEST_MONTH_MIN_CONTRACTS = 65
# analytics_engine.run_alerts
ALERT_NPL_BRANCH_CRITICAL_PCT = 26
ALERT_NPL_BRANCH_WARNING_PCT = 13
ALERT_RISKY_EMPLOYEE_NPL_PCT = 13
ALERT_LOW_COLLECTION_PCT = 50
ALERT_LOW_COLLECTION_MIN_CONTRACTS = 100

# ─── DATA QUALITY (nightly + staged passport merge) ───
DATA_QUALITY_NIGHTLY_ENABLED = os.environ.get("DATA_QUALITY_NIGHTLY_ENABLED", "1").strip() in ("1", "true", "yes")
DATA_QUALITY_NIGHTLY_HOUR = int(os.environ.get("DATA_QUALITY_NIGHTLY_HOUR", "2"))
# off | dry_run | apply
DATA_QUALITY_PASSPORT_MERGE_MODE = os.environ.get("DATA_QUALITY_PASSPORT_MERGE_MODE", "dry_run").strip().lower()

# ─── PORTFOLIO: фильтр по сумме долга (ТЗ 11) ───
# Бэнды: (label, min, max) — max=None означает "и выше"
DEBT_BANDS = [
    ("До 1 млн", 0, 999_999),
    ("1–2 млн", 1_000_000, 1_999_999),
    ("2–3.9 млн", 2_000_000, 3_999_999),
    ("4 млн+", 4_000_000, None),
    ("6 млн+", 6_000_000, None),
    ("8 млн+", 8_000_000, None),
    ("10 млн+", 10_000_000, None),
    ("20 млн+", 20_000_000, None),
    ("25 млн+", 25_000_000, None),
    ("30 млн+", 30_000_000, None),
]
# Маржа для оценки утечки прибыли (просрочка * маржа)
PROFIT_MARGIN_PCT = float(os.environ.get("PROFIT_MARGIN_PCT", "20"))

# ─── OPEX / FINANCE ───
OPEX_CATEGORIES = [
    "Аренда", "ФОТ", "Маркетинг", "IT и связь",
    "Транспорт", "Коммунальные", "Канцелярия", "Прочее",
]

def _hash(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

DEFAULT_USERS = {
    "director": {"password_hash": _hash("Director@2026!"), "role": "executive", "branch": None, "name": "Директор"},
    "analyst": {"password_hash": _hash("Analyst@2026!"), "role": "head_analyst", "branch": None, "name": "Аналитик"},
}
for i in range(1, 8):
    DEFAULT_USERS[f"manager_f{i}"] = {"password_hash": _hash(f"ManagerF{i}@26!"), "role": "branch_manager", "branch": f"Ф{i}", "name": f"Менеджер {BRANCHES[f'Ф{i}']}"}
    DEFAULT_USERS[f"employee_f{i}"] = {"password_hash": _hash(f"EmpF{i}@2026"), "role": "branch_employee", "branch": f"Ф{i}", "name": f"Сотрудник {BRANCHES[f'Ф{i}']}"}
