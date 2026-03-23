"""
CCC Intelligence Platform — Behavioral Scoring Engine
PART 1 of the Credit Intelligence Layer

Static scoring uses: age, income, region, employment.
Behavioral scoring uses: HOW the client actually behaves over time.

Behavioral dimensions:
  1. Payment Regularity Index     — consistency of on-time payments
  2. Micro-Delay Pattern          — 1-7 day delays (early warning signal)
  3. Payment Trajectory           — improving / stable / deteriorating
  4. Early Repayment Behavior     — signals strong capacity
  5. Repeat Purchase Behavior     — loyalty, relationship depth
  6. Contract Utilization Ratio   — debt-to-limit usage
  7. Time-Between-Contracts       — frequency of borrowing

Output:
  behavior_score          (0–100)
  behavior_risk_class     (A–E)
  behavior_pd             probability of default based on behavior alone
  behavior_signals        list of positive/negative signals
"""

import math
from datetime import datetime, timedelta
from core.database import get_db


# ═══════════════════════════════════════════════════════════════
#  BEHAVIORAL CONSTANTS
# ═══════════════════════════════════════════════════════════════

BEHAVIOR_CLASSES = {
    "A": {"min": 80, "max": 100, "label": "Отличное поведение",   "color": "#22c55e"},
    "B": {"min": 65, "max": 79,  "label": "Хорошее поведение",    "color": "#5cb85c"},
    "C": {"min": 45, "max": 64,  "label": "Стабильное поведение", "color": "#f0ad4e"},
    "D": {"min": 25, "max": 44,  "label": "Рискованное",          "color": "#ff9800"},
    "E": {"min": 0,  "max": 24,  "label": "Критическое",          "color": "#ef4444"},
}

# Weights for behavioral dimensions (sum to 1.0)
DIMENSION_WEIGHTS = {
    "payment_regularity":      0.30,
    "micro_delay_pattern":     0.20,
    "payment_trajectory":      0.15,
    "early_repayment":         0.10,
    "repeat_behavior":         0.10,
    "utilization_ratio":       0.10,
    "time_between_contracts":  0.05,
}


# ═══════════════════════════════════════════════════════════════
#  DIMENSION CALCULATORS
# ═══════════════════════════════════════════════════════════════

def _payment_regularity_score(contracts: list) -> tuple:
    """
    Measures how consistently the client pays on time.
    Uses late_count vs total contracts ratio.
    Score: 0–100
    """
    if not contracts:
        return 50, "Нет истории", 0.0

    total = len(contracts)
    contracts_with_lates = sum(1 for c in contracts if (c.get('late_count') or 0) > 0)
    total_late_days = sum(c.get('total_late_days') or 0 for c in contracts)
    avg_late_days = total_late_days / total if total > 0 else 0

    # Base score: fraction of contracts without any late
    clean_ratio = (total - contracts_with_lates) / total
    base = clean_ratio * 70  # max 70 from clean ratio

    # Bonus for low average late days
    if avg_late_days == 0:
        bonus = 30
    elif avg_late_days <= 3:
        bonus = 20
    elif avg_late_days <= 7:
        bonus = 10
    elif avg_late_days <= 14:
        bonus = 5
    else:
        bonus = 0

    score = min(100, base + bonus)
    desc = f"Чистых: {total - contracts_with_lates}/{total}, ср.просрочка: {avg_late_days:.0f}д"
    pd_contribution = max(0.0, (1 - score / 100) * 0.4)
    return round(score), desc, round(pd_contribution, 4)


def _micro_delay_pattern_score(contracts: list) -> tuple:
    """
    Micro-delays (1-7 days) are early warning signals.
    They often precede larger defaults.
    Score: 0–100 (100 = no micro-delays)
    """
    if not contracts:
        return 70, "Нет истории", 0.0

    total_late_days = sum(c.get('total_late_days') or 0 for c in contracts)
    total_contracts = len(contracts)

    # Estimate micro-delays: late_days in 1-7 range per contract
    # Approximation: if total_late_days/total_contracts is small → micro-delay
    avg_delay = total_late_days / total_contracts if total_contracts > 0 else 0

    if avg_delay == 0:
        score, desc = 100, "Нет микро-просрочек"
    elif avg_delay <= 3:
        score, desc = 85, f"Микро-задержки: ~{avg_delay:.1f}д ср."
    elif avg_delay <= 7:
        score, desc = 65, f"Микро-просрочки: {avg_delay:.1f}д ср."
    elif avg_delay <= 14:
        score, desc = 45, f"Систематические задержки: {avg_delay:.1f}д"
    elif avg_delay <= 30:
        score, desc = 25, f"Серьёзные задержки: {avg_delay:.1f}д"
    else:
        score, desc = 10, f"Хроническая просрочка: {avg_delay:.1f}д"

    pd_contribution = max(0.0, (1 - score / 100) * 0.35)
    return score, desc, round(pd_contribution, 4)


def _payment_trajectory_score(contracts: list) -> tuple:
    """
    Is the client's behavior improving, stable, or deteriorating?
    Compares first-half contracts to second-half contracts.
    Score: 0–100
    """
    if len(contracts) < 2:
        return 60, "Недостаточно истории", 0.0

    # Sort by contract date (oldest first)
    sorted_c = sorted(contracts, key=lambda c: c.get('contract_date') or '')
    mid = len(sorted_c) // 2
    early = sorted_c[:mid]
    recent = sorted_c[mid:]

    def avg_late(group):
        vals = [c.get('total_late_days') or 0 for c in group]
        return sum(vals) / len(vals) if vals else 0

    early_avg = avg_late(early)
    recent_avg = avg_late(recent)

    if recent_avg == 0 and early_avg == 0:
        return 80, "Стабильно чисто", 0.01
    elif recent_avg < early_avg * 0.7:
        score, desc = 90, f"📈 Улучшение: {early_avg:.0f}д → {recent_avg:.0f}д"
    elif recent_avg <= early_avg * 1.1:
        score, desc = 65, f"→ Стабильно: ~{recent_avg:.0f}д"
    elif recent_avg < early_avg * 1.5:
        score, desc = 40, f"📉 Ухудшение: {early_avg:.0f}д → {recent_avg:.0f}д"
    else:
        score, desc = 15, f"🔴 Резкое ухудшение: {early_avg:.0f}д → {recent_avg:.0f}д"

    pd_contribution = max(0.0, (1 - score / 100) * 0.25)
    return score, desc, round(pd_contribution, 4)


def _early_repayment_score(contracts: list) -> tuple:
    """
    Clients who repay early show strong capacity and intent.
    Detects: paid_amount significantly higher than scheduled.
    Score: 0–100
    """
    if not contracts:
        return 50, "Нет данных", 0.0

    early_repayments = 0
    for c in contracts:
        product = c.get('product_amount') or 0
        paid = c.get('paid_amount') or 0
        debt = c.get('debt_amount') or 0
        term = c.get('contract_term') or 12
        if product <= 0 or term <= 0:
            continue
        # Estimate expected paid by now based on contract term
        # If paid > 110% of expected → early repayment signal
        scheduled_monthly = product / term
        if paid > 0 and debt < product * 0.3 and paid > product * 0.7:
            early_repayments += 1

    ratio = early_repayments / len(contracts) if contracts else 0

    if ratio >= 0.5:
        score, desc = 95, f"Досрочные погашения: {early_repayments}/{len(contracts)}"
    elif ratio >= 0.3:
        score, desc = 80, f"Частичное досрочное: {early_repayments}/{len(contracts)}"
    elif ratio >= 0.1:
        score, desc = 65, f"Единичные досрочные: {early_repayments}/{len(contracts)}"
    else:
        score, desc = 50, "Нет досрочных погашений"

    pd_contribution = max(0.0, (1 - score / 100) * 0.15)
    return score, desc, round(pd_contribution, 4)


def _repeat_behavior_score(contracts_count: int, npl_count: int) -> tuple:
    """
    Repeat customers who never defaulted are the best risk segment.
    Score: 0–100
    """
    if contracts_count <= 0:
        return 40, "Новый клиент", 0.1

    if npl_count > 0:
        # Has NPL in history — penalize heavily
        npl_ratio = npl_count / contracts_count
        score = max(0, 30 - int(npl_ratio * 60))
        desc = f"НПЛ в истории: {npl_count}/{contracts_count}"
    else:
        # Clean repeat customer
        if contracts_count >= 5:
            score, desc = 95, f"Лояльный: {contracts_count} дог., 0 НПЛ"
        elif contracts_count >= 3:
            score, desc = 85, f"Постоянный: {contracts_count} дог., чисто"
        elif contracts_count >= 2:
            score, desc = 75, f"Повторный: {contracts_count} дог., чисто"
        else:
            score, desc = 60, "Первый договор завершён чисто"

    pd_contribution = max(0.0, (1 - score / 100) * 0.2)
    return score, desc, round(pd_contribution, 4)


def _utilization_ratio_score(contracts: list) -> tuple:
    """
    Contract utilization: debt_amount / product_amount.
    High utilization with no payments = risk.
    Low utilization = capacity to pay.
    Score: 0–100
    """
    if not contracts:
        return 60, "Нет данных", 0.0

    active = [c for c in contracts if (c.get('debt_amount') or 0) > 0]
    if not active:
        return 80, "Нет активной задолженности", 0.0

    ratios = []
    for c in active:
        product = c.get('product_amount') or 1
        debt = c.get('debt_amount') or 0
        ratios.append(debt / product)

    avg_util = sum(ratios) / len(ratios)

    if avg_util <= 0.2:
        score, desc = 95, f"Утилизация: {avg_util*100:.0f}% (очень низкая)"
    elif avg_util <= 0.4:
        score, desc = 80, f"Утилизация: {avg_util*100:.0f}% (умеренная)"
    elif avg_util <= 0.6:
        score, desc = 60, f"Утилизация: {avg_util*100:.0f}% (средняя)"
    elif avg_util <= 0.8:
        score, desc = 35, f"Утилизация: {avg_util*100:.0f}% (высокая)"
    else:
        score, desc = 15, f"Утилизация: {avg_util*100:.0f}% (критическая)"

    pd_contribution = avg_util * 0.3
    return score, desc, round(pd_contribution, 4)


def _time_between_contracts_score(contracts: list) -> tuple:
    """
    Time between contracts: very short intervals may indicate dependency.
    Score: 0–100
    """
    if len(contracts) < 2:
        return 60, "Один договор", 0.0

    dates = []
    for c in contracts:
        d = c.get('contract_date')
        if d:
            try:
                dates.append(datetime.strptime(d[:10], '%Y-%m-%d'))
            except Exception:
                pass

    if len(dates) < 2:
        return 60, "Недостаточно дат", 0.0

    dates.sort()
    gaps = [(dates[i+1] - dates[i]).days for i in range(len(dates)-1)]
    avg_gap = sum(gaps) / len(gaps)

    if avg_gap >= 365:
        score, desc = 90, f"Интервал: ~{int(avg_gap/30)} мес. (здоровый)"
    elif avg_gap >= 180:
        score, desc = 80, f"Интервал: ~{int(avg_gap/30)} мес."
    elif avg_gap >= 90:
        score, desc = 65, f"Интервал: ~{int(avg_gap/30)} мес."
    elif avg_gap >= 30:
        score, desc = 45, f"Частое кредитование: ~{int(avg_gap)} дн."
    else:
        score, desc = 25, f"⚠️ Очень короткий интервал: ~{int(avg_gap)} дн."

    pd_contribution = max(0.0, (1 - score / 100) * 0.1)
    return score, desc, round(pd_contribution, 4)


# ═══════════════════════════════════════════════════════════════
#  MAIN BEHAVIORAL SCORING FUNCTION
# ═══════════════════════════════════════════════════════════════

def compute_behavioral_score(client_id: int) -> dict:
    """
    Compute full behavioral score for an existing client.
    Returns behavior_score, behavior_risk_class, behavior_pd, signals.
    """
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not client:
        db.close()
        return {"error": "Клиент не найден", "behavior_score": 50, "behavior_risk_class": "C"}

    contracts = db.execute(
        "SELECT * FROM contracts WHERE client_id = ? ORDER BY contract_date",
        (client_id,)
    ).fetchall()
    db.close()

    contracts = [dict(c) for c in contracts]
    client = dict(client)
    contracts_count = len(contracts)
    npl_count = sum(1 for c in contracts if c.get('status_detail') in ('Ёмон', 'МИБ', 'Судда'))

    # ─── Calculate each dimension ───
    reg_score,   reg_desc,   reg_pd   = _payment_regularity_score(contracts)
    micro_score, micro_desc, micro_pd = _micro_delay_pattern_score(contracts)
    traj_score,  traj_desc,  traj_pd  = _payment_trajectory_score(contracts)
    early_score, early_desc, early_pd = _early_repayment_score(contracts)
    rep_score,   rep_desc,   rep_pd   = _repeat_behavior_score(contracts_count, npl_count)
    util_score,  util_desc,  util_pd  = _utilization_ratio_score(contracts)
    tbc_score,   tbc_desc,   tbc_pd   = _time_between_contracts_score(contracts)

    # ─── Weighted composite score ───
    w = DIMENSION_WEIGHTS
    behavior_score = (
        reg_score   * w["payment_regularity"]     +
        micro_score * w["micro_delay_pattern"]    +
        traj_score  * w["payment_trajectory"]     +
        early_score * w["early_repayment"]        +
        rep_score   * w["repeat_behavior"]        +
        util_score  * w["utilization_ratio"]      +
        tbc_score   * w["time_between_contracts"]
    )
    behavior_score = round(behavior_score)

    # ─── Behavioral PD (blended) ───
    behavior_pd = (
        reg_pd   * w["payment_regularity"]     +
        micro_pd * w["micro_delay_pattern"]    +
        traj_pd  * w["payment_trajectory"]     +
        early_pd * w["early_repayment"]        +
        rep_pd   * w["repeat_behavior"]        +
        util_pd  * w["utilization_ratio"]      +
        tbc_pd   * w["time_between_contracts"]
    )
    behavior_pd = round(min(0.99, behavior_pd), 4)

    # ─── Risk class ───
    behavior_risk_class = "E"
    for cls, cfg in BEHAVIOR_CLASSES.items():
        if cfg["min"] <= behavior_score <= cfg["max"]:
            behavior_risk_class = cls
            break

    # ─── Breakdown ───
    breakdown = [
        {"dimension": "Регулярность платежей",   "score": reg_score,   "weight": w["payment_regularity"],    "desc": reg_desc},
        {"dimension": "Микро-задержки",           "score": micro_score, "weight": w["micro_delay_pattern"],   "desc": micro_desc},
        {"dimension": "Динамика поведения",       "score": traj_score,  "weight": w["payment_trajectory"],    "desc": traj_desc},
        {"dimension": "Досрочное погашение",      "score": early_score, "weight": w["early_repayment"],       "desc": early_desc},
        {"dimension": "Повторная активность",     "score": rep_score,   "weight": w["repeat_behavior"],       "desc": rep_desc},
        {"dimension": "Утилизация лимита",        "score": util_score,  "weight": w["utilization_ratio"],     "desc": util_desc},
        {"dimension": "Интервал между дог.",      "score": tbc_score,   "weight": w["time_between_contracts"],"desc": tbc_desc},
    ]

    # ─── Positive / negative signals ───
    signals = []
    for item in breakdown:
        if item["score"] >= 80:
            signals.append({"type": "positive", "icon": "✅",
                "text": f"{item['dimension']}: {item['desc']}"})
        elif item["score"] <= 35:
            signals.append({"type": "negative", "icon": "⚠️",
                "text": f"{item['dimension']}: {item['desc']}"})

    # ─── Save to behavioral_scores table ───
    _save_behavioral_score(client_id, behavior_score, behavior_risk_class, behavior_pd, breakdown)

    return {
        "client_id":           client_id,
        "behavior_score":      behavior_score,
        "behavior_risk_class": behavior_risk_class,
        "behavior_risk_label": BEHAVIOR_CLASSES[behavior_risk_class]["label"],
        "behavior_pd":         behavior_pd,
        "behavior_color":      BEHAVIOR_CLASSES[behavior_risk_class]["color"],
        "breakdown":           breakdown,
        "signals":             signals,
        "contracts_analyzed":  contracts_count,
        "computed_at":         datetime.now().isoformat(),
    }


def compute_behavioral_score_batch(limit: int = 500) -> dict:
    """
    Batch compute behavioral scores for all clients with contracts.
    Used by scheduler for nightly refresh.
    """
    db = get_db()
    client_ids = db.execute(
        "SELECT DISTINCT client_id FROM contracts LIMIT ?", (limit,)
    ).fetchall()
    db.close()

    computed = 0
    errors = 0
    for row in client_ids:
        try:
            compute_behavioral_score(row[0])
            computed += 1
        except Exception:
            errors += 1

    return {"status": "ok", "computed": computed, "errors": errors}


def get_behavioral_score(client_id: int) -> dict | None:
    """Read latest cached behavioral score from DB."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM behavioral_scores WHERE client_id = ? ORDER BY computed_at DESC LIMIT 1",
        (client_id,)
    ).fetchone()
    db.close()
    return dict(row) if row else None


def _save_behavioral_score(client_id, score, risk_class, pd, breakdown):
    """Persist behavioral score to DB."""
    try:
        import json
        db = get_db()
        db.execute("""
            INSERT INTO behavioral_scores
            (client_id, behavior_score, behavior_risk_class, behavior_pd, breakdown, computed_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (client_id, score, risk_class, pd,
              json.dumps(breakdown, ensure_ascii=False, default=str),
              datetime.now().isoformat()))
        db.commit()
        db.close()
    except Exception:
        pass


def init_behavioral_tables():
    """Create tables needed for behavioral scoring."""
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS behavioral_scores (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id        INTEGER NOT NULL REFERENCES clients(id),
            behavior_score   INTEGER NOT NULL,
            behavior_risk_class TEXT,
            behavior_pd      REAL,
            breakdown        TEXT,
            computed_at      TEXT,
            INDEX_hint       TEXT
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS ix_behavioral_client
        ON behavioral_scores(client_id, computed_at DESC)
    """)
    db.commit()
    db.close()
