"""
CCC Intelligence Platform — Feature Store Engine
PART 2: Centralized feature repository

The Feature Store is the single source of truth for all ML signals.
Every scoring engine reads features from here instead of computing inline.

Architecture:
  contracts + clients + behavioral_scores
                ↓
         feature_store_engine.py   ← this file
                ↓
         client_features (table)
                ↓
    rule scoring / ml scoring / behavioral scoring

Features computed:
  Static:     age, income, region, employment
  Behavioral: payment_behavior_index, early_repayment_score, micro_delay_ratio
  Portfolio:  repeat_customer_flag, utilization_ratio, contract_count
  Risk:       region_pd, branch_pd (from intelligence tables)
"""

import json
from datetime import datetime
from core.database import get_db


# ═══════════════════════════════════════════════════════════════
#  FEATURE DEFINITIONS
# ═══════════════════════════════════════════════════════════════

FEATURE_SCHEMA = {
    # Static client features
    "age":                      {"type": "int",   "source": "clients",   "description": "Возраст клиента"},
    "gender":                   {"type": "str",   "source": "clients",   "description": "Пол"},
    "income_source":            {"type": "str",   "source": "clients",   "description": "Источник дохода"},
    "monthly_income":           {"type": "float", "source": "clients",   "description": "Ежемесячный доход"},
    "region":                   {"type": "str",   "source": "clients",   "description": "Регион"},
    "has_car":                  {"type": "int",   "source": "clients",   "description": "Наличие авто"},
    "has_card":                 {"type": "int",   "source": "clients",   "description": "Наличие карты"},

    # Aggregated contract features
    "contract_count":           {"type": "int",   "source": "contracts", "description": "Всего договоров"},
    "active_contract_count":    {"type": "int",   "source": "contracts", "description": "Активных договоров"},
    "total_product_amount":     {"type": "float", "source": "contracts", "description": "Суммарная сумма товаров"},
    "total_debt_amount":        {"type": "float", "source": "contracts", "description": "Суммарный долг"},
    "total_paid_amount":        {"type": "float", "source": "contracts", "description": "Суммарно оплачено"},
    "avg_contract_term":        {"type": "float", "source": "contracts", "description": "Средний срок"},
    "avg_interest_rate":        {"type": "float", "source": "contracts", "description": "Средняя ставка"},

    # Behavioral features
    "payment_behavior_index":   {"type": "float", "source": "behavioral","description": "Индекс поведения 0–100"},
    "behavior_risk_class":      {"type": "str",   "source": "behavioral","description": "Поведенческий класс A–E"},
    "behavior_pd":              {"type": "float", "source": "behavioral","description": "Поведенческий PD"},
    "early_repayment_score":    {"type": "float", "source": "behavioral","description": "Досрочные погашения 0–100"},
    "micro_delay_ratio":        {"type": "float", "source": "behavioral","description": "Доля договоров с микро-просрочками"},
    "payment_trajectory":       {"type": "str",   "source": "behavioral","description": "improving/stable/deteriorating"},

    # Risk context features (from intelligence tables)
    "region_pd":                {"type": "float", "source": "risk_by_region", "description": "PD региона"},
    "branch_pd":                {"type": "float", "source": "risk_by_branch", "description": "PD филиала"},

    # Derived flags
    "repeat_customer_flag":     {"type": "int",   "source": "derived",   "description": "Повторный клиент (0/1)"},
    "has_npl_history":          {"type": "int",   "source": "derived",   "description": "Был НПЛ в истории"},
    "product_type":             {"type": "str",   "source": "derived",   "description": "Тип продукта (сумма)"},
    "utilization_ratio":        {"type": "float", "source": "derived",   "description": "Долг / товар"},
}


# ═══════════════════════════════════════════════════════════════
#  FEATURE COMPUTATION
# ═══════════════════════════════════════════════════════════════

def _classify_product_type(avg_amount: float) -> str:
    """Classify product by average amount."""
    if avg_amount <= 500_000:     return "micro"
    if avg_amount <= 2_000_000:   return "small"
    if avg_amount <= 8_000_000:   return "medium"
    if avg_amount <= 20_000_000:  return "large"
    return "premium"


def _payment_trajectory_label(contracts: list) -> str:
    """Simplified trajectory: improving / stable / deteriorating."""
    if len(contracts) < 2:
        return "stable"
    sorted_c = sorted(contracts, key=lambda c: c.get('contract_date') or '')
    mid = len(sorted_c) // 2
    early_avg = sum(c.get('total_late_days') or 0 for c in sorted_c[:mid]) / max(mid, 1)
    recent_avg = sum(c.get('total_late_days') or 0 for c in sorted_c[mid:]) / max(len(sorted_c) - mid, 1)
    if recent_avg < early_avg * 0.7:
        return "improving"
    if recent_avg > early_avg * 1.3:
        return "deteriorating"
    return "stable"


def compute_client_features(client_id: int) -> dict:
    """
    Compute and store all features for a client.
    Returns the feature dict (also persisted to client_features table).
    """
    db = get_db()

    # Load client
    client = db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not client:
        db.close()
        return {}
    client = dict(client)

    # Load contracts
    contracts = db.execute(
        "SELECT * FROM contracts WHERE client_id = ?", (client_id,)
    ).fetchall()
    contracts = [dict(c) for c in contracts]

    # Load latest behavioral score
    beh = db.execute(
        "SELECT * FROM behavioral_scores WHERE client_id = ? ORDER BY computed_at DESC LIMIT 1",
        (client_id,)
    ).fetchone()
    beh = dict(beh) if beh else {}

    # Load risk context
    region_row = db.execute(
        "SELECT pd FROM risk_by_region WHERE region = ? ORDER BY id DESC LIMIT 1",
        (client.get('region') or '',)
    ).fetchone()

    branch = contracts[0].get('branch') if contracts else None
    branch_row = db.execute(
        "SELECT pd FROM risk_by_branch WHERE branch = ? ORDER BY id DESC LIMIT 1",
        (branch or '',)
    ).fetchone() if branch else None

    db.close()

    # ─── Aggregate contract metrics ───
    n = len(contracts)
    total_product  = sum(c.get('product_amount') or 0 for c in contracts)
    total_debt     = sum(c.get('debt_amount') or 0 for c in contracts)
    total_paid     = sum(c.get('paid_amount') or 0 for c in contracts)
    npl_count      = sum(1 for c in contracts if c.get('status_detail') in ('Ёмон','МИБ','Судда'))
    active_count   = sum(1 for c in contracts if c.get('debt_amount', 0) > 0)

    avg_term       = sum(c.get('contract_term') or 12 for c in contracts) / n if n else 12
    avg_rate       = sum(c.get('interest_rate') or 3.3 for c in contracts) / n if n else 3.3
    avg_amount     = total_product / n if n else 0
    util_ratio     = round(total_debt / total_product, 4) if total_product > 0 else 0.0

    total_late_days = sum(c.get('total_late_days') or 0 for c in contracts)
    contracts_with_micro = sum(
        1 for c in contracts if 0 < (c.get('total_late_days') or 0) <= 7
    )
    micro_delay_ratio = round(contracts_with_micro / n, 4) if n > 0 else 0.0

    # ─── Build feature dict ───
    features = {
        "client_id":              client_id,
        # Static
        "age":                    client.get('age') or 0,
        "gender":                 client.get('gender') or '',
        "income_source":          client.get('income_source') or '',
        "monthly_income":         client.get('monthly_income') or 0.0,
        "region":                 client.get('region') or '',
        "has_car":                client.get('has_car') or 0,
        "has_card":               client.get('has_card') or 0,
        # Contracts
        "contract_count":         n,
        "active_contract_count":  active_count,
        "total_product_amount":   round(total_product, 2),
        "total_debt_amount":      round(total_debt, 2),
        "total_paid_amount":      round(total_paid, 2),
        "avg_contract_term":      round(avg_term, 1),
        "avg_interest_rate":      round(avg_rate, 3),
        # Behavioral
        "payment_behavior_index": beh.get('behavior_score') or 50,
        "behavior_risk_class":    beh.get('behavior_risk_class') or 'C',
        "behavior_pd":            beh.get('behavior_pd') or 0.15,
        "micro_delay_ratio":      micro_delay_ratio,
        "payment_trajectory":     _payment_trajectory_label(contracts),
        # Derived
        "repeat_customer_flag":   1 if n > 1 else 0,
        "has_npl_history":        1 if npl_count > 0 else 0,
        "product_type":           _classify_product_type(avg_amount),
        "utilization_ratio":      util_ratio,
        # Risk context
        "region_pd":              round(region_row[0] if region_row else 0.08, 4),
        "branch_pd":              round(branch_row[0] if branch_row else 0.08, 4),
        # Metadata
        "computed_at":            datetime.now().isoformat(),
    }

    # ─── Persist to feature store ───
    _upsert_features(features)
    return features


def _upsert_features(f: dict):
    """Insert or replace features for client."""
    try:
        db = get_db()
        db.execute("""
            INSERT OR REPLACE INTO client_features (
                client_id, age, gender, income_source, monthly_income, region,
                has_car, has_card, contract_count, active_contract_count,
                total_product_amount, total_debt_amount, total_paid_amount,
                avg_contract_term, avg_interest_rate,
                payment_behavior_index, behavior_risk_class, behavior_pd,
                micro_delay_ratio, payment_trajectory,
                repeat_customer_flag, has_npl_history, product_type,
                utilization_ratio, region_pd, branch_pd, computed_at
            ) VALUES (
                :client_id, :age, :gender, :income_source, :monthly_income, :region,
                :has_car, :has_card, :contract_count, :active_contract_count,
                :total_product_amount, :total_debt_amount, :total_paid_amount,
                :avg_contract_term, :avg_interest_rate,
                :payment_behavior_index, :behavior_risk_class, :behavior_pd,
                :micro_delay_ratio, :payment_trajectory,
                :repeat_customer_flag, :has_npl_history, :product_type,
                :utilization_ratio, :region_pd, :branch_pd, :computed_at
            )
        """, f)
        db.commit()
        db.close()
    except Exception:
        pass


def get_client_features(client_id: int) -> dict | None:
    """Retrieve features from store (compute if missing)."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM client_features WHERE client_id = ?", (client_id,)
    ).fetchone()
    db.close()
    if row:
        return dict(row)
    # Not in store — compute on demand
    return compute_client_features(client_id)


def refresh_feature_store(limit: int = 1000) -> dict:
    """Nightly batch refresh of feature store."""
    db = get_db()
    ids = db.execute(
        "SELECT DISTINCT id FROM clients LIMIT ?", (limit,)
    ).fetchall()
    db.close()

    ok = 0
    for row in ids:
        try:
            compute_client_features(row[0])
            ok += 1
        except Exception:
            pass

    return {"status": "ok", "refreshed": ok}


def get_feature_distribution(feature_name: str) -> list:
    """Distribution of a numeric feature across all clients."""
    db = get_db()
    # Only allow known features (prevent SQL injection)
    if feature_name not in FEATURE_SCHEMA:
        db.close()
        return []
    rows = db.execute(
        f"SELECT {feature_name}, COUNT(*) as cnt FROM client_features GROUP BY {feature_name}"
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def init_feature_store_tables():
    """Create the feature store table."""
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS client_features (
            client_id               INTEGER PRIMARY KEY REFERENCES clients(id),
            -- Static
            age                     INTEGER,
            gender                  TEXT,
            income_source           TEXT,
            monthly_income          REAL DEFAULT 0,
            region                  TEXT,
            has_car                 INTEGER DEFAULT 0,
            has_card                INTEGER DEFAULT 0,
            -- Contracts aggregate
            contract_count          INTEGER DEFAULT 0,
            active_contract_count   INTEGER DEFAULT 0,
            total_product_amount    REAL DEFAULT 0,
            total_debt_amount       REAL DEFAULT 0,
            total_paid_amount       REAL DEFAULT 0,
            avg_contract_term       REAL DEFAULT 12,
            avg_interest_rate       REAL DEFAULT 3.3,
            -- Behavioral
            payment_behavior_index  REAL DEFAULT 50,
            behavior_risk_class     TEXT DEFAULT 'C',
            behavior_pd             REAL DEFAULT 0.15,
            micro_delay_ratio       REAL DEFAULT 0,
            payment_trajectory      TEXT DEFAULT 'stable',
            -- Derived flags
            repeat_customer_flag    INTEGER DEFAULT 0,
            has_npl_history         INTEGER DEFAULT 0,
            product_type            TEXT DEFAULT 'small',
            utilization_ratio       REAL DEFAULT 0,
            -- Risk context
            region_pd               REAL DEFAULT 0.08,
            branch_pd               REAL DEFAULT 0.08,
            -- Metadata
            computed_at             TEXT
        )
    """)
    db.commit()
    db.close()
