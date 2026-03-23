"""
CCC Intelligence Platform — Hybrid Scoring Engine
PART 3: Combines rule-based + ML + behavioral scores
PART 4: Portfolio feedback loop (region/branch/product risk adjustment)

Pipeline:
  1. Rule score       (existing scoring_engine → 0–100 scale)
  2. ML PD            (ml_engine → probability 0–1)
  3. Behavioral score (behavioral_scoring_engine → 0–100)
  4. Portfolio context (intelligence tables → risk multipliers)
  5. Final blend      → unified decision

Output:
  final_score
  risk_class
  probability_of_default (blended)
  recommended_interest_rate
  decision
  explanation
"""

import math
from datetime import datetime
from config.settings import SCORING_CLASSES, MIN_RATE, MAX_RATE, RATE_MAP
from core.database import get_db, get_client
from engines.behavioral_scoring_engine import compute_behavioral_score
from engines.feature_store_engine import get_client_features, compute_client_features


# ═══════════════════════════════════════════════════════════════
#  HYBRID WEIGHTS
# ═══════════════════════════════════════════════════════════════
# These weights define how much each component contributes.
# Tunable without changing logic.

HYBRID_WEIGHTS = {
    "rule_score":      0.50,   # Existing rule-based score
    "behavioral_score": 0.35,  # Behavioral dimension
    "ml_pd":           0.15,   # ML model (when available)
}

# Portfolio feedback: max adjustment to approval threshold
MAX_PORTFOLIO_ADJUSTMENT = 10  # points


# ═══════════════════════════════════════════════════════════════
#  PORTFOLIO FEEDBACK CONTEXT
# ═══════════════════════════════════════════════════════════════

def _get_portfolio_context(region: str, branch: str, product_type: str) -> dict:
    """
    Pull risk multipliers from intelligence tables.
    Returns adjustments to apply to scoring threshold.
    PART 4: Portfolio feedback loop.
    """
    db = get_db()
    context = {
        "region_pd":          0.08,
        "branch_pd":          0.08,
        "region_adjustment":  0,
        "branch_adjustment":  0,
        "product_adjustment": 0,
        "total_adjustment":   0,
        "messages":           [],
    }

    # Region risk
    if region:
        row = db.execute(
            "SELECT pd, total FROM risk_by_region WHERE region = ? ORDER BY id DESC LIMIT 1",
            (region,)
        ).fetchone()
        if row and row['total'] >= 20:
            context["region_pd"] = row['pd']
            if row['pd'] > 0.15:
                adj = -8
                context["region_adjustment"] = adj
                context["messages"].append(f"⚠️ Регион '{region}': PD={row['pd']*100:.1f}% → порог -{abs(adj)}пт")
            elif row['pd'] > 0.10:
                adj = -5
                context["region_adjustment"] = adj
                context["messages"].append(f"⚠️ Регион '{region}': PD={row['pd']*100:.1f}% → порог -{abs(adj)}пт")
            elif row['pd'] < 0.04:
                adj = +3
                context["region_adjustment"] = adj
                context["messages"].append(f"✅ Регион '{region}': низкий риск PD={row['pd']*100:.1f}%")

    # Branch risk
    if branch:
        row = db.execute(
            "SELECT pd, collection_rate FROM risk_by_branch WHERE branch = ? ORDER BY id DESC LIMIT 1",
            (branch,)
        ).fetchone()
        if row:
            context["branch_pd"] = row['pd']
            if row['pd'] > 0.12:
                adj = -5
                context["branch_adjustment"] = adj
                context["messages"].append(f"🔴 Филиал '{branch}': PD={row['pd']*100:.1f}%")
            elif row['pd'] < 0.04 and row['collection_rate'] > 85:
                adj = +2
                context["branch_adjustment"] = adj
                context["messages"].append(f"✅ Филиал '{branch}': низкий риск")

    db.close()

    context["total_adjustment"] = max(
        -MAX_PORTFOLIO_ADJUSTMENT,
        min(MAX_PORTFOLIO_ADJUSTMENT,
            context["region_adjustment"] + context["branch_adjustment"] + context["product_adjustment"])
    )
    return context


# ═══════════════════════════════════════════════════════════════
#  SCORE NORMALIZATION
# ═══════════════════════════════════════════════════════════════

def _normalize_rule_score(raw_score: int) -> float:
    """
    Normalize rule-based score (typically -30 to +80) → 0–100.
    Calibrated to existing SCORING_CLASSES.
    """
    # Existing: E < 15, D 15-29, C 30-41, B 42-54, A 55+
    # Map to 0-100: min=-40, max=80 range
    clamped = max(-40, min(80, raw_score))
    return round((clamped + 40) / 120 * 100, 1)


def _ml_pd_to_score(ml_pd: float) -> float:
    """Convert ML probability of default → 0–100 score (inverted)."""
    return round((1 - min(0.99, max(0, ml_pd))) * 100, 1)


# ═══════════════════════════════════════════════════════════════
#  HYBRID DECISION
# ═══════════════════════════════════════════════════════════════

def _classify_final(score: float, portfolio_context: dict) -> tuple:
    """Map blended score + portfolio adjustment → risk class."""
    adjusted = score + portfolio_context["total_adjustment"]
    adjusted = max(0, min(100, adjusted))

    if adjusted >= 80:   return "A", "Одобрено",         "#22c55e"
    if adjusted >= 65:   return "B", "Одобрено",         "#5cb85c"
    if adjusted >= 50:   return "C", "Доп. проверка",    "#f0ad4e"
    if adjusted >= 35:   return "D", "На рассмотрение",  "#ff9800"
    return                      "E", "Отказ",            "#ef4444"


def _blended_pd(rule_score_raw: int, ml_pd: float | None, behavior_pd: float) -> float:
    """
    Blend three PD estimates into final probability of default.
    If ML model unavailable, redistribute its weight.
    """
    # Convert rule score to PD: logistic function
    rule_pd = round(1 / (1 + math.exp(0.08 * (rule_score_raw - 40))) * 0.85, 4)

    if ml_pd is not None and 0 < ml_pd < 1:
        w_rule = HYBRID_WEIGHTS["rule_score"]
        w_beh  = HYBRID_WEIGHTS["behavioral_score"]
        w_ml   = HYBRID_WEIGHTS["ml_pd"]
    else:
        # No ML — redistribute ML weight to rules
        w_ml   = 0
        w_rule = HYBRID_WEIGHTS["rule_score"] + HYBRID_WEIGHTS["ml_pd"] * 0.7
        w_beh  = HYBRID_WEIGHTS["behavioral_score"] + HYBRID_WEIGHTS["ml_pd"] * 0.3
        ml_pd  = rule_pd  # placeholder

    blended = w_rule * rule_pd + w_ml * ml_pd + w_beh * behavior_pd
    return round(min(0.99, blended), 4)


def _recommend_rate(risk_class: str, is_pension: bool, blended_pd: float) -> tuple:
    """
    Recommend interest rate strictly within [MIN_RATE, MAX_RATE].
    Slight PD-based adjustment within the class band.
    """
    base_rate = RATE_MAP.get(risk_class, 3.3)

    # Micro-adjust based on PD (max ±0.1%)
    pd_adj = round((blended_pd - 0.08) * 0.5, 2)
    pd_adj = max(-0.1, min(0.1, pd_adj))

    rate = base_rate + pd_adj
    if is_pension and rate > MIN_RATE:
        rate = max(MIN_RATE, rate - 0.2)

    rate = round(max(MIN_RATE, min(MAX_RATE, rate)), 2)
    return rate, f"Класс {risk_class} + PD-корр. {pd_adj:+.2f}% → {rate}%"


# ═══════════════════════════════════════════════════════════════
#  MAIN HYBRID SCORING FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def hybrid_score_existing_client(client_id: int) -> dict:
    """
    Full hybrid scoring for an EXISTING client.
    Integrates all three engines + portfolio context.
    """
    # ─── Step 1: Run existing rule scoring ───
    from engines.scoring_engine import score_client_by_id
    try:
        rule_result = score_client_by_id(client_id)
    except Exception as e:
        return {"error": f"Rule scoring failed: {e}"}

    rule_score_raw   = rule_result["total_score"]
    rule_score_norm  = _normalize_rule_score(rule_score_raw)
    is_pension       = 'пенсион' in (rule_result['client'].get('income_source','') or '').lower()

    # ─── Step 2: Behavioral score ───
    beh_result  = compute_behavioral_score(client_id)
    beh_score   = beh_result.get("behavior_score", 50)
    beh_pd      = beh_result.get("behavior_pd", 0.15)

    # ─── Step 3: ML PD (optional — graceful fallback) ───
    ml_pd = _get_cached_ml_pd(client_id)

    # ─── Step 4: Portfolio context (feedback loop) ───
    features   = get_client_features(client_id) or {}
    region     = features.get('region') or rule_result['client'].get('region', '')
    branch     = rule_result['client'].get('branch') or (
        rule_result['client']['contracts'][0].get('branch') if rule_result['client'].get('contracts') else ''
    )
    product_type = features.get('product_type', 'small')
    portfolio_ctx = _get_portfolio_context(region, branch, product_type)

    # ─── Step 5: Blend scores ───
    blended_norm_score = (
        rule_score_norm  * HYBRID_WEIGHTS["rule_score"]     +
        beh_score        * HYBRID_WEIGHTS["behavioral_score"] +
        _ml_pd_to_score(ml_pd or 0.15) * HYBRID_WEIGHTS["ml_pd"]
    )
    blended_norm_score = round(blended_norm_score, 1)

    blended_pd = _blended_pd(rule_score_raw, ml_pd, beh_pd)

    # ─── Step 6: Final classification ───
    final_class, final_label, final_color = _classify_final(
        blended_norm_score, portfolio_ctx
    )
    recommended_rate, rate_desc = _recommend_rate(final_class, is_pension, blended_pd)

    # ─── Build output ───
    result = {
        # Client
        "client_id":             client_id,
        "client_name":           rule_result['client'].get('full_name'),

        # Component scores
        "rule_score_raw":        rule_score_raw,
        "rule_score_normalized": rule_score_norm,
        "behavioral_score":      beh_score,
        "ml_pd":                 ml_pd,

        # Blended
        "final_score":           blended_norm_score,
        "risk_class":            final_class,
        "risk_label":            final_label,
        "decision":              final_label,
        "color":                 final_color,

        # PD
        "probability_of_default": blended_pd,
        "pd_breakdown": {
            "rule_contribution":      round(rule_score_norm * HYBRID_WEIGHTS["rule_score"], 2),
            "behavioral_contribution": round(beh_score * HYBRID_WEIGHTS["behavioral_score"], 2),
            "ml_contribution":         round(_ml_pd_to_score(ml_pd or 0.15) * HYBRID_WEIGHTS["ml_pd"], 2),
        },

        # Rate
        "recommended_interest_rate": recommended_rate,
        "rate_description":          rate_desc,

        # Portfolio context
        "portfolio_context":     portfolio_ctx,

        # Behavioral detail
        "behavioral_result":     beh_result,

        # Rule detail (from existing engine)
        "rule_breakdown":        rule_result.get('breakdown', []),

        # Metadata
        "scoring_mode":  "hybrid",
        "weights_used":  HYBRID_WEIGHTS,
        "scored_at":     datetime.now().isoformat(),
    }

    _save_hybrid_score(result)
    return result


def hybrid_score_new_client(form_data: dict) -> dict:
    """
    Hybrid scoring for a NEW client (no behavioral history).
    Behavioral weight redistributed to rules.
    """
    from engines.scoring_engine import score_new_client
    rule_result = score_new_client(form_data)
    rule_score_raw  = rule_result["total_score"]
    rule_score_norm = _normalize_rule_score(rule_score_raw)

    # No behavioral history for new client
    beh_score = 50
    beh_pd    = 0.15

    region     = form_data.get('region', '')
    branch     = form_data.get('branch', '')
    portfolio_ctx = _get_portfolio_context(region, branch, 'small')

    # New client: heavier weight on rules
    blended_norm_score = round(
        rule_score_norm * 0.80 + beh_score * 0.10 + 50 * 0.10,
        1
    )

    blended_pd = _blended_pd(rule_score_raw, None, beh_pd)
    is_pension = 'пенсион' in (form_data.get('income_source','') or '').lower()
    final_class, final_label, final_color = _classify_final(blended_norm_score, portfolio_ctx)
    recommended_rate, rate_desc = _recommend_rate(final_class, is_pension, blended_pd)

    return {
        "client_id":              None,
        "client_name":            form_data.get('full_name', 'Новый клиент'),
        "rule_score_raw":         rule_score_raw,
        "rule_score_normalized":  rule_score_norm,
        "behavioral_score":       beh_score,
        "ml_pd":                  None,
        "final_score":            blended_norm_score,
        "risk_class":             final_class,
        "risk_label":             final_label,
        "decision":               final_label,
        "color":                  final_color,
        "probability_of_default": blended_pd,
        "recommended_interest_rate": recommended_rate,
        "rate_description":       rate_desc,
        "portfolio_context":      portfolio_ctx,
        "behavioral_result":      {"behavior_score": 50, "note": "Новый клиент — нет истории"},
        "rule_breakdown":         rule_result.get('breakdown', []),
        "scoring_mode":           "hybrid_new",
        "scored_at":              datetime.now().isoformat(),
    }


def _get_cached_ml_pd(client_id: int) -> float | None:
    """Read latest ML prediction for client from training_dataset."""
    try:
        db = get_db()
        # Check if ML model was trained — use its stored feature importance as proxy
        model_exists = db.execute(
            "SELECT COUNT(*) FROM ml_models"
        ).fetchone()[0]
        db.close()

        if model_exists == 0:
            return None

        # Get client features for ML prediction
        features = get_client_features(client_id)
        if not features:
            return None

        return _score_with_ml(features)
    except Exception:
        return None


def _score_with_ml(features: dict) -> float | None:
    """Apply trained ML model to feature vector."""
    try:
        import numpy as np
        from sklearn.ensemble import GradientBoostingClassifier
        import pickle, os
        from config.settings import BASE_DIR

        model_path = os.path.join(BASE_DIR, 'models', 'scoring_model.pkl')
        if not os.path.exists(model_path):
            return None

        with open(model_path, 'rb') as f:
            model = pickle.load(f)

        feature_vector = np.array([[
            features.get('age', 30),
            features.get('has_car', 0),
            features.get('has_card', 0),
            features.get('avg_contract_term', 12),
            features.get('avg_interest_rate', 3.3),
            features.get('total_product_amount', 0),
            0,   # advance_payment — not in feature store
            features.get('total_product_amount', 0) / max(features.get('contract_count', 1), 1) / 12,
            0,   # late_count — use behavior proxy
            features.get('micro_delay_ratio', 0) * 30,
            features.get('utilization_ratio', 0),
        ]])

        pd_proba = model.predict_proba(feature_vector)[0][1]
        return round(float(pd_proba), 4)
    except Exception:
        return None


def _save_hybrid_score(result: dict):
    """Log hybrid scoring result."""
    try:
        import json
        db = get_db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS hybrid_scoring_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER,
                rule_score_raw INTEGER,
                rule_score_norm REAL,
                behavioral_score REAL,
                ml_pd REAL,
                final_score REAL,
                risk_class TEXT,
                blended_pd REAL,
                recommended_rate REAL,
                decision TEXT,
                portfolio_context TEXT,
                scored_at TEXT
            )
        """)
        db.execute("""
            INSERT INTO hybrid_scoring_log
            (client_id, rule_score_raw, rule_score_norm, behavioral_score, ml_pd,
             final_score, risk_class, blended_pd, recommended_rate, decision,
             portfolio_context, scored_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            result.get('client_id'),
            result.get('rule_score_raw'),
            result.get('rule_score_normalized'),
            result.get('behavioral_score'),
            result.get('ml_pd'),
            result.get('final_score'),
            result.get('risk_class'),
            result.get('probability_of_default'),
            result.get('recommended_interest_rate'),
            result.get('decision'),
            json.dumps(result.get('portfolio_context', {}), ensure_ascii=False),
            result.get('scored_at'),
        ))
        db.commit()
        db.close()
    except Exception:
        pass
