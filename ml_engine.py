"""
CCC — ML Engine
Hybrid scoring: rules + machine learning.
Early Default Prediction: identifies clients likely to default soon.
"""
import json
from datetime import datetime
from core.database import get_db

def train_ml_model():
    """
    Train a ML model on the training_dataset.
    Returns model metrics. Model predictions augment rule-based scoring.
    """
    db = get_db()
    rows = db.execute("""
        SELECT age, has_car, has_card, contract_term, interest_rate,
            product_amount, advance_payment, monthly_payment,
            late_count, total_late_days, debt_ratio, label
        FROM training_dataset
    """).fetchall()
    db.close()

    if len(rows) < 200:
        return {"status": "error", "message": f"Мало данных: {len(rows)} записей (нужно 200+)"}

    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import accuracy_score, roc_auc_score
    except ImportError:
        return {"status": "error", "message": "scikit-learn не установлен"}

    import numpy as np
    features = ['age','has_car','has_card','contract_term','interest_rate',
        'product_amount','advance_payment','monthly_payment',
        'late_count','total_late_days','debt_ratio']
    X = np.array([[r[f] or 0 for f in features] for r in rows])
    y = np.array([r['label'] for r in rows])

    # Проверка: должны быть как минимум два различных класса
    unique_labels = set(y.tolist()) if hasattr(y, "tolist") else set(y)
    if len(unique_labels) < 2:
        return {
            "status": "error",
            "message": "Невозможно обучить ML‑модель: все записи относятся к одному классу "
                       f"(label={next(iter(unique_labels), '?')}). Добавьте данные с другой меткой.",
        }

    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=42)
    model = GradientBoostingClassifier(n_estimators=100, max_depth=5, random_state=42)
    model.fit(X_tr, y_tr)

    y_pred = model.predict(X_te)
    y_prob = model.predict_proba(X_te)[:, 1]

    acc = round(accuracy_score(y_te, y_pred), 4)
    try: auc = round(roc_auc_score(y_te, y_prob), 4)
    except: auc = 0

    # Feature importance
    fi = {}
    names_ru = {'age':'Возраст','has_car':'Авто','has_card':'Карта',
        'contract_term':'Срок','interest_rate':'Ставка',
        'product_amount':'Сумма товара','advance_payment':'ПВ',
        'monthly_payment':'Ежемес. платёж','late_count':'Просрочки',
        'total_late_days':'Дней просрочки','debt_ratio':'Долг/Товар'}
    for feat, imp in zip(features, model.feature_importances_):
        fi[names_ru.get(feat, feat)] = round(float(imp), 4)
    fi = dict(sorted(fi.items(), key=lambda x: -x[1]))

    # Save model info to DB
    db2 = get_db()
    db2.execute("""CREATE TABLE IF NOT EXISTS ml_models (
        id INTEGER PRIMARY KEY, model_type TEXT, accuracy REAL, auc REAL,
        feature_importance TEXT, records_used INTEGER, trained_at TEXT)""")
    db2.execute("INSERT INTO ml_models VALUES (NULL,?,?,?,?,?,?)",
        ('gradient_boosting', acc, auc, json.dumps(fi, ensure_ascii=False),
         len(rows), datetime.now().isoformat()))
    db2.commit()
    db2.close()

    # Save model to disk for hybrid_scoring_engine._score_with_ml
    try:
        import os
        import pickle
        from config.settings import BASE_DIR
        models_dir = os.path.join(BASE_DIR, 'models')
        os.makedirs(models_dir, exist_ok=True)
        model_path = os.path.join(models_dir, 'scoring_model.pkl')
        with open(model_path, 'wb') as f:
            pickle.dump(model, f)
    except Exception:
        pass

    return {
        "status": "ok",
        "model": "Градиентный бустинг",
        "accuracy": acc, "auc": auc,
        "feature_importance": fi,
        "records_used": len(rows),
        "positive_rate": round(float(y.mean()) * 100, 2),
    }


def predict_early_defaults(days_threshold=90):
    """
    Early Default Prediction.
    Identifies active clients likely to default within N days
    based on: deteriorating payment patterns, increasing delays, high debt ratio.
    """
    db = get_db()

    # Clients with active contracts showing warning signs
    rows = db.execute("""
        SELECT c.id as client_id, c.full_name, c.age, c.income_source,
            ct.branch, ct.contract_term, ct.product_amount,
            ct.debt_amount, ct.paid_amount, ct.late_count,
            ct.total_late_days, ct.monthly_payment, ct.status_detail,
            CASE WHEN ct.product_amount > 0
                THEN ct.debt_amount * 1.0 / ct.product_amount ELSE 0 END as debt_ratio
        FROM contracts ct
        JOIN clients c ON c.id = ct.client_id
        WHERE ct.status_detail = 'Стандарт'
        AND ct.debt_amount > 0
        AND (ct.late_count > 0 OR ct.total_late_days > 15)
        ORDER BY ct.total_late_days DESC
    """).fetchall()

    predictions = []
    for r in rows:
        risk_score = 0
        reasons = []

        # Late payment count
        lc = r['late_count'] or 0
        if lc >= 3:
            risk_score += 40; reasons.append(f"Просрочек: {lc}")
        elif lc >= 2:
            risk_score += 25; reasons.append(f"Просрочек: {lc}")
        elif lc >= 1:
            risk_score += 15; reasons.append(f"Просрочка: {lc}")

        # Total late days
        tld = r['total_late_days'] or 0
        if tld > 60:
            risk_score += 30; reasons.append(f"Дней просрочки: {tld:.0f}")
        elif tld > 30:
            risk_score += 20; reasons.append(f"Дней просрочки: {tld:.0f}")
        elif tld > 15:
            risk_score += 10; reasons.append(f"Дней просрочки: {tld:.0f}")

        # Debt ratio
        dr = r['debt_ratio'] or 0
        if dr > 0.8:
            risk_score += 20; reasons.append(f"Долг/товар: {dr*100:.0f}%")
        elif dr > 0.6:
            risk_score += 10

        # Payment coverage
        if r['monthly_payment'] and r['monthly_payment'] > 0:
            coverage = (r['paid_amount'] or 0) / (r['product_amount'] or 1)
            if coverage < 0.2:
                risk_score += 15; reasons.append(f"Оплачено: {coverage*100:.0f}%")

        if risk_score >= 30:
            pred_label = "HIGH_RISK" if risk_score >= 60 else "MEDIUM_RISK"
            early_default_probability = round(min(0.99, risk_score / 100.0), 4)
            if pred_label == "HIGH_RISK":
                recommended_action = "Срочный контакт; предложить реструктуризацию; ограничить новые выдачи"
            else:
                recommended_action = "Мониторинг платежей; напоминание о сроках; при ухудшении — контакт"
            predictions.append({
                "client_id": r['client_id'],
                "full_name": r['full_name'],
                "branch": r['branch'],
                "risk_score": risk_score,
                "early_default_probability": early_default_probability,
                "recommended_action": recommended_action,
                "debt_amount": r['debt_amount'],
                "late_count": lc,
                "total_late_days": tld,
                "reasons": reasons,
                "prediction": pred_label,
            })

    predictions.sort(key=lambda x: -x['risk_score'])
    db.close()

    return {
        "total_analyzed": len(rows),
        "high_risk": sum(1 for p in predictions if p['prediction'] == 'HIGH_RISK'),
        "medium_risk": sum(1 for p in predictions if p['prediction'] == 'MEDIUM_RISK'),
        "predictions": predictions[:100],
    }
