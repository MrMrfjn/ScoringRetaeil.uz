"""
CreditControlCenter FINAL — Production Flask Application
Architecture: client_id → contracts → scoring → analytics
"""
import os, sys, hashlib, json, logging
import threading
from collections import defaultdict
from datetime import datetime
from functools import wraps
from time import time
from flask import (Flask, render_template, render_template_string, request, redirect,
                   url_for, session, jsonify, send_file, Response)
from werkzeug.utils import secure_filename

# Загрузка .env до импорта config (единый механизм конфигурации)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# In-app rate limiting (per key or IP)
_rate_buckets = defaultdict(list)
_rate_lock = threading.Lock()
def _rate_limit_allow(key: str, limit: int = 60, window_sec: int = 60) -> bool:
    with _rate_lock:
        now = time()
        bucket_key = (key, int(now // window_sec))
        _rate_buckets[bucket_key].append(now)
        _rate_buckets[bucket_key] = [t for t in _rate_buckets[bucket_key] if now - t < window_sec]
        return len(_rate_buckets[bucket_key]) <= limit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# Proactive: if project dir is not writable, use temp for DB before config loads
if not os.environ.get('DB_PATH'):
    _proj_dir = BASE_DIR
    try:
        _test_file = os.path.join(_proj_dir, '.ccc_write_test')
        with open(_test_file, 'w') as _f:
            _f.write('1')
        os.remove(_test_file)
    except (OSError, IOError):
        _fallback_db = os.path.join(os.environ.get('TEMP', os.environ.get('TMP', '.')), 'ccc_credit_control.db')
        os.environ['DB_PATH'] = _fallback_db

# Централизованное логирование (уровень, формат, опционально файл)
def _setup_logging():
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%Y-%m-%d %H:%M:%S")
    log_path = os.environ.get("LOG_PATH", "").strip()
    if log_path:
        try:
            from logging.handlers import RotatingFileHandler
            h = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
            h.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
            logging.getLogger().addHandler(h)
        except Exception:
            pass

_setup_logging()
logger = logging.getLogger("ccc")

# #region agent log
def _dbg745(hypothesis_id, location, message, data, run_id="pre-fix"):
    try:
        with open(os.path.join(BASE_DIR, "..", "debug-74510d.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "sessionId": "74510d",
                "runId": run_id,
                "hypothesisId": hypothesis_id,
                "location": location,
                "message": message,
                "data": data or {},
                "timestamp": int(datetime.now().timestamp() * 1000),
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass
# #endregion

from config.settings import *
from core.database import *
from engines.scoring_engine import score_client_by_id, score_new_client, ScoringError


from engines.analytics_engine import (compute_daily_snapshot, run_alerts,
                                       get_latest_snapshot)
from engines.backup_engine import create_backup, list_backups
from engines.integrity_engine import run_integrity_checks
from engines.intelligence_engine import (compute_intelligence_tables,
                                          prepare_training_dataset, get_portfolio_feedback)
from engines.ml_engine import train_ml_model, predict_early_defaults
from engines.scheduler_engine import start_scheduler, get_scheduler_status

app = Flask(__name__)
_secret = os.environ.get("SECRET_KEY", "").strip()
# FLASK_ENV из config.settings (development | production)
_FLASK_ENV = os.environ.get("FLASK_ENV", "development").strip().lower()
if _FLASK_ENV == "production" and not _secret:
    raise RuntimeError(
        "В production необходимо задать SECRET_KEY в окружении (например в .env). "
        "Пример: SECRET_KEY=случайная_строка_не_менее_32_символов"
    )
app.secret_key = _secret or "CCC-FINAL-DEV-KEY-NOT-FOR-PRODUCTION"
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024
app.config['SESSION_COOKIE_SECURE']   = _FLASK_ENV == 'production'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = int(os.environ.get('SESSION_LIFETIME_MINUTES', '480')) * 60

# ═══ INTELLIGENCE LAYER ═══
# Behavioral scoring · Feature store · Hybrid scoring · Dashboards

try:
    from engines.behavioral_scoring_engine import init_behavioral_tables
    from engines.feature_store_engine import init_feature_store_tables
    from engines.saas_engine import init_saas_tables
    from engines.white_label_engine import init_white_label_tables
    init_behavioral_tables()
    init_feature_store_tables()
    init_saas_tables()
    init_white_label_tables()
except Exception as _e:
    logger.warning("Extended tables: %s", _e)

try:
    from routes.dashboards import register_dashboard_routes
    register_dashboard_routes(app)
except Exception as _e:
    logger.warning("Dashboard routes: %s", _e)

try:
    from routes.api_routes import register_api_routes
    register_api_routes(app, _rate_limit_allow)
except Exception as _e:
    logger.warning("API routes: %s", _e)

# Fallback POST /api/score if api_routes did not register it (e.g. import error)
if not any(r.rule == '/api/score' and 'POST' in (r.methods or set()) for r in app.url_map.iter_rules()):
    @app.route("/api/score", methods=["POST"])
    def api_score_fallback():
        if not _rate_limit_allow("score_%s" % (request.remote_addr or "unknown"), limit=30, window_sec=60):
            return jsonify({"error": "Too many requests"}), 429
        from engines.scoring_engine import score_new_client, ScoringError
        data = request.get_json(silent=True) or {}
        try:
            if data.get("client_id"):
                from engines.scoring_engine import score_client_by_id
                result = score_client_by_id(int(data["client_id"]))
            else:
                result = score_new_client(data)
            breakdown = [{"criterion": n, "points": s, "description": d} for n, s, d in result["breakdown"]]
            return jsonify({
                "score": result["total_score"],
                "probability_of_default": result.get("pd"),
                "risk_class": result["risk_class"],
                "recommended_interest_rate": result.get("interest_rate"),
                "decision": result.get("decision"),
                "rate_desc": result.get("rate_desc", ""),
                "breakdown": breakdown,
                "calculator": result.get("calculator", {}),
            })
        except ScoringError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500


# ═══ CSRF (минимальная защита форм) ═══
def _csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = hashlib.sha256(os.urandom(32)).hexdigest()
    return session['csrf_token']

@app.before_request
def _csrf_validate():
    if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
        _csrf_token()  # обеспечить токен к следующему POST
        return
    if request.path.startswith('/api/') or request.path == '/health' or request.path.startswith('/system/'):
        return  # API и системные эндпоинты без CSRF (своя авторизация)
    token = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token')
    if not token or token != session.get('csrf_token'):
        _csrf_token()
        if request.path.startswith('/api/') or request.accept_mimetypes.best_match(['application/json']) == 'application/json':
            return jsonify({"error": "forbidden", "message": "Неверный или отсутствующий CSRF-токен"}), 403
        back_url = request.path or '/'
        return (f"""
        <!DOCTYPE html><html><head><meta charset="UTF-8"><title>Обновите страницу</title>
        <style>body{{background:#0f172a;color:#e2e8f0;font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;}}
        .box{{text-align:center;padding:40px;background:#1e293b;border-radius:16px;max-width:420px;}} a{{color:#3b82f6;}} .btn{{display:inline-block;margin-top:16px;padding:10px 24px;background:#3b82f6;color:#fff;border-radius:8px;text-decoration:none;font-weight:600}}</style></head><body>
        <div class="box"><h2>Страница устарела</h2><p>Сервер перезагрузился. Нажмите кнопку ниже — форма откроется заново.</p><a class="btn" href="{back_url}">Обновить и продолжить</a></div></body></html>
        """, 403)

@app.context_processor
def _inject_csrf():
    return dict(csrf_token=_csrf_token())

# ═══ SECURITY HEADERS ═══

@app.after_request
def _security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if _FLASK_ENV == 'production':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

@app.before_request
def _make_session_permanent():
    session.permanent = True

# ═══ MIDDLEWARE ═══

def login_required(f):
    @wraps(f)
    def d(*a, **k):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*a, **k)
    return d


def role_required(mod):
    def dec(f):
        @wraps(f)
        def d(*a, **k):
            if 'user' not in session:
                return redirect(url_for('login'))
            u = session['user']
            mods = u.get('modules') or []
            if mod not in mods and not (u.get('role') in ('director', 'executive') and mod in ('export', 'admin', 'import')):
                return "Нет доступа", 403
            return f(*a, **k)
        return d
    return dec


def user_branch():
    return session.get('user', {}).get('branch')


@app.route('/api/db-check')
@login_required
def api_db_check():
    """Диагностика базы данных: список таблиц и количество записей."""
    db = get_db()
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    counts = {}
    for t in tables:
        name = t['name'] if isinstance(t, dict) else t[0]
        try:
            cnt = db.execute(f"SELECT COUNT(*) as c FROM [{name}]").fetchone()
            counts[name] = cnt['c'] if isinstance(cnt, dict) else cnt[0]
        except Exception:
            counts[name] = '?'
    db.close()
    return jsonify(counts)


@app.route('/health')
def health_check():
    """Docker / nginx health check endpoint."""
    status = {"status": "ok", "timestamp": datetime.now().isoformat(),
              "platform": "scoringretail.uz"}
    try:
        _db = get_db(); _db.execute("SELECT 1").fetchone(); _db.close()
        status["database"] = "ok"
    except Exception as _e:
        status["database"] = f"error: {_e}"; status["status"] = "degraded"
    try:
        import shutil
        usage = shutil.disk_usage(BASE_DIR)
        status["disk_free_gb"] = round(usage.free / 1024**3, 1)
    except Exception:
        pass
    return jsonify(status), (200 if status["status"] == "ok" else 503)


# /api/* routes registered in routes/api_routes.py (hybrid, behavioral, features, score, marketplace, developer, etc.)


@app.route('/api/user/me')
@login_required
def api_user_me():
    """Return current user info for SPA frontend."""
    u = session.get('user')
    if not u:
        return jsonify({'error': 'not authenticated'}), 401
    return jsonify({
        'username': u.get('username', ''),
        'name': u.get('name', ''),
        'role': u.get('role', ''),
        'role_label': u.get('role_label', ''),
        'branch': u.get('branch'),
        'modules': u.get('modules', []),
    })


# ═══ AUTH ═══

@app.route('/login', methods=['GET', 'POST'])
def login():
    err = None
    if request.method == 'POST':
        u = authenticate(request.form.get('username', '').strip(),
                         request.form.get('password', '').strip())
        if u:
            rc = ROLES.get(u['role'], {})
            session['user'] = {
                'username': u['username'], 'name': u['name'],
                'role': u['role'], 'role_label': rc.get('label', ''),
                'branch': u.get('branch'),
                'modules': rc.get('modules', []),
            }
            log_audit(u['username'], 'login')
            return redirect(url_for('dashboard'))
        err = "Неверный логин или пароль"
    return render_template('login.html', error=err, css=CSS)

@app.route('/logout')
def logout():
    try:
        un = (session.get('user') or {}).get('username') or (session.get('user') or {}).get('name') or 'anonymous'
        log_audit(un, 'logout')
    except Exception:
        pass
    session.clear()
    return redirect(url_for('login'))

# ═══ DASHBOARD ═══

@app.route('/')
@login_required
def dashboard():
    u = session['user']
    year_filter = request.args.get('year', '').strip()
    branch_filter = request.args.get('branch', '').strip()
    if u.get('branch'):
        b = u['branch']
    elif branch_filter:
        b = branch_filter
    else:
        b = None
    flt = "AND branch=?" if b else ""
    year_flt = "AND strftime('%Y', contract_date)=?" if year_filter else ""
    p = []
    if year_filter:
        p.append(year_filter)
    if b:
        p.append(b)
    # Основные цифры дашборда — из снимка (картинка: Портфел, Прри, Хатар)
    from core.database import get_dashboard_snapshot
    snapshot = get_dashboard_snapshot(branch_filter=b)
    if snapshot:
        # Live counters from contracts (snapshot table stores only portfolio/prri/npl% picture)
        npl_case = "SUM(CASE WHEN TRIM(COALESCE(status_detail,'')) IN ('Ёмон','Емон','МИБ','Судда') OR CAST(COALESCE(total_late_days,0) AS REAL) >= 90 THEN 1 ELSE 0 END)"
        db_live = get_db()
        live_row = db_live.execute(f"""
            SELECT
                COUNT(*) AS total_contracts,
                COUNT(DISTINCT client_id) AS total_clients,
                {npl_case} AS npl_count
            FROM contracts
            WHERE 1=1 {year_flt} {flt}
        """, p).fetchone()
        db_live.close()
        live_total = int(live_row["total_contracts"] or 0) if live_row else 0
        live_clients = int(live_row["total_clients"] or 0) if live_row else 0
        live_npl = int(live_row["npl_count"] or 0) if live_row else 0

        kpi = {
            "total": int(live_total),
            "portfolio": snapshot["kpi"]["portfolio"],
            "npl_rate": snapshot["kpi"]["npl_rate"],
            "collection_rate": snapshot["kpi"]["collection_rate"],
            "clients": int(live_clients),
            "npl": int(live_npl),
        }
        by_branch = [
            {
                "branch": r["branch"],
                "portfolio": r["portfolio"],
                "npl_rate": r["npl_rate_pct"],
                "prri": r["prri"],
                "total": None,
                "npl_count": None,
                "collection_rate": None,
            }
            for r in snapshot["by_branch"]
        ]
        dashboard_snapshot_date = snapshot["snapshot_date"]
    else:
        dashboard_snapshot_date = None
        db = get_db()
        npl_case = "SUM(CASE WHEN TRIM(COALESCE(status_detail,'')) IN ('Ёмон','Емон','МИБ','Судда') OR CAST(COALESCE(total_late_days,0) AS REAL) >= 90 THEN 1 ELSE 0 END)"
        npl_rate_expr = f"ROUND(100.0*{npl_case}/COUNT(*),2)"
        kpi_row = db.execute(f"""
            SELECT COUNT(*) as total,
                   COUNT(DISTINCT client_id) as clients,
                   {npl_case} as npl,
                   {npl_rate_expr} as npl_rate,
                   CASE
                     WHEN COALESCE(SUM(product_amount),0) > 0
                     THEN ROUND(100.0 * COALESCE(SUM(paid_amount),0) / SUM(product_amount), 1)
                     ELSE 0
                   END as collection_rate,
                   COALESCE(SUM(debt_amount),0) as portfolio
            FROM contracts WHERE 1=1 {year_flt} {flt}
        """, p).fetchone()
        kpi = dict(kpi_row) if kpi_row else {}
        by_branch = []
        for r in db.execute(f"""
            SELECT branch,
                   COUNT(*) as total,
                   {npl_case} as npl_count,
                   ROUND(100.0*{npl_case}/COUNT(*),1) as npl_rate,
                   CASE
                     WHEN COALESCE(SUM(product_amount),0) > 0
                     THEN ROUND(100.0 * COALESCE(SUM(paid_amount),0) / SUM(product_amount), 1)
                     ELSE 0
                   END as collection_rate,
                   COALESCE(SUM(debt_amount),0) as portfolio
            FROM contracts WHERE 1=1 {year_flt} {flt}
            GROUP BY branch ORDER BY branch
        """, p).fetchall():
            by_branch.append(dict(r))

    # Дробление и срезы — всегда из Full DB.db (npl_case нужен при любом источнике KPI)
    npl_case = "SUM(CASE WHEN TRIM(COALESCE(status_detail,'')) IN ('Ёмон','Емон','МИБ','Судда') OR CAST(COALESCE(total_late_days,0) AS REAL) >= 90 THEN 1 ELSE 0 END)"
    db = get_db()
    yearly_rows = db.execute(f"""
        SELECT strftime('%Y', contract_date) as year,
               COUNT(*) as total,
               {npl_case} as npl,
               ROUND(100.0*{npl_case}/COUNT(*),1) as npl_rate,
               CASE
                 WHEN COALESCE(SUM(product_amount),0) > 0
                 THEN ROUND(100.0 * COALESCE(SUM(paid_amount),0) / SUM(product_amount), 1)
                 ELSE 0
               END as collection_rate,
               COALESCE(SUM(debt_amount),0) as portfolio
        FROM contracts WHERE 1=1 {year_flt} {flt} AND contract_date IS NOT NULL
        GROUP BY year ORDER BY year DESC
    """, p).fetchall()
    yearly = [dict(r) for r in yearly_rows]
    # Список годов для выпадающего списка — всегда полный (без применения текущего фильтра)
    years_all_rows = db.execute(
        "SELECT DISTINCT strftime('%Y', contract_date) as year FROM contracts WHERE contract_date IS NOT NULL ORDER BY year DESC"
    ).fetchall()
    years = [r['year'] for r in years_all_rows]
    cur_year = year_filter if year_filter else ''

    aging_row = db.execute(f"""
        SELECT
          SUM(CASE WHEN COALESCE(total_late_days,0)=0 THEN 1 ELSE 0 END) as d0,
          SUM(CASE WHEN total_late_days BETWEEN 1 AND 30 THEN 1 ELSE 0 END) as d30,
          SUM(CASE WHEN total_late_days BETWEEN 31 AND 60 THEN 1 ELSE 0 END) as d60,
          SUM(CASE WHEN total_late_days BETWEEN 61 AND 90 THEN 1 ELSE 0 END) as d90,
          SUM(CASE WHEN total_late_days > 90 THEN 1 ELSE 0 END) as d90p
        FROM contracts WHERE 1=1 {year_flt} {flt}
    """, p).fetchone()
    aging = dict(aging_row) if aging_row else {}

    top_employees = []
    for r in db.execute(f"""
        SELECT responsible_person as employee_name,
               COUNT(*) as total,
               SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90 THEN 1 ELSE 0 END) as npl,
               ROUND(100.0*SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90
                     THEN 1 ELSE 0 END)/COUNT(*),1) as npl_rate
        FROM contracts
        WHERE contract_date >= date('now','-365 days')
        AND responsible_person IS NOT NULL AND TRIM(responsible_person) != ''
        AND responsible_person NOT IN ('-','—','Пусто') {year_flt} {flt}
        GROUP BY responsible_person HAVING total > 10
        ORDER BY npl_rate DESC LIMIT 5
    """, p).fetchall():
        top_employees.append(dict(r))

    db.close()
    return render_template_string(
        DASHBOARD_T,
        user=u,
        active="dashboard",
        kpi=kpi,
        by_branch=by_branch,
        dashboard_snapshot_date=dashboard_snapshot_date,
        yearly=yearly,
        years=years,
        cur_year=cur_year,
        aging=aging,
        top_employees=top_employees,
        current_branch=b,
        BRANCHES=BRANCHES,
        NAV=NAV,
        CSS=CSS,
    )

# ═══ SCORING (client_id centric) ═══

def _normalize_score_result(engine_result):
    """Привести результат движка к виду шаблона: score, breakdown как list of dicts."""
    if not engine_result or engine_result.get('error'):
        return engine_result
    out = dict(engine_result)
    out['score'] = engine_result.get('total_score', 0)
    bd = engine_result.get('breakdown') or []
    if not bd:
        out['breakdown'] = []
    elif isinstance(bd[0], (list, tuple)) and len(bd[0]) >= 3:
        out['breakdown'] = [{'criterion': n, 'points': s, 'description': d} for n, s, d in bd]
    else:
        out['breakdown'] = [{'criterion': x.get('criterion', x.get('name', '')), 'points': x.get('points', x.get('score', 0)), 'description': x.get('description', x.get('desc', ''))} for x in bd]
    return out

@app.route('/scoring', methods=['GET', 'POST'])
@login_required
@role_required('scoring')
def scoring():
    u = session['user']
    action = request.form.get('action', '') if request.method == 'POST' else ''
    results = []
    score_result = None
    search_error = None
    query = ''

    if action == 'search':
        from core.database import search_scoring_candidates
        query = request.form.get('query', '').strip()
        if query:
            try:
                results = search_scoring_candidates(query)
                # #region agent log
                _dbg745(
                    "H-search-miss",
                    "main.py:scoring.search",
                    "search_results",
                    {
                        "query": query[:64],
                        "rows": len(results),
                        "sample": [
                            {
                                "client_id": r.get("client_id"),
                                "branch": r.get("branch"),
                                "passport": r.get("passport"),
                                "phone": r.get("phone"),
                            } for r in results[:6]
                        ],
                    },
                )
                # #endregion
                try:
                    log_audit(u['username'], 'search', 'client', details=f'{query} → {len(results)} рез.')
                except Exception:
                    pass
            except Exception as e:
                search_error = str(e)

    elif action == 'score_existing':
        client_id = request.form.get('client_id')
        if client_id and client_id.isdigit():
            try:
                score_result = score_client_by_id(int(client_id))
                score_result = _normalize_score_result(score_result)
                try:
                    log_audit(u['username'], 'score', 'client', entity_id=int(client_id), details=f"class={score_result.get('risk_class')}")
                except Exception:
                    pass
            except Exception as e:
                score_result = {'error': str(e)}
        else:
            score_result = {'error': 'Не указан client_id'}

    elif action == 'score_new':
        try:
            product = float(request.form.get('product_amount', 0) or 0)
            advance_pct = float(request.form.get('advance_pct', 0) or 0)
            term_months = int(request.form.get('term_months', 12) or 12)
            advance_payment = product * advance_pct / 100 if product else 0
            form = {
                'age': int(request.form.get('age', 30) or 30),
                'gender': request.form.get('gender', 'М'),
                'income_source': request.form.get('income_source', ''),
                'monthly_income': float(request.form.get('monthly_income', 0) or 0),
                'extra_income': float(request.form.get('extra_income', 0) or 0),
                'extra_income_type': request.form.get('extra_income_type', ''),
                'position': request.form.get('position', ''),
                'product_amount': product,
                'advance_payment': advance_payment,
                'advance_pct': advance_pct,
                'term_months': term_months,
                'contract_term': term_months,
                'region': request.form.get('region', ''),
                'mfy': request.form.get('mfy', ''),
                'external_delay': int(request.form.get('external_delay', 0) or 0),
                'overdue_months': int(request.form.get('external_delay', 0) or 0),
                'has_guarantor': request.form.get('has_guarantor') == '1',
                'has_kafeel': request.form.get('has_guarantor') == '1',
                'has_mib_debt': request.form.get('has_mib_debt') == '1',
                'has_mib_court': request.form.get('has_mib_debt') == '1',
                'has_fine': request.form.get('has_fine') == '1',
                'admin_fine': 500000 if request.form.get('has_fine') == '1' else 0,
                'mib_debt': 200000 if request.form.get('has_mib_debt') == '1' else 0,
                'branch': request.form.get('branch', u.get('branch', 'Ф1') or 'Ф1'),
                'full_name': request.form.get('full_name', 'Новый клиент'),
                'has_card': '1' if request.form.get('has_card') == '1' else '0',
                'has_car': '1' if request.form.get('has_car') == '1' else '0',
                'interest_rate': 3.3,
                'passport': request.form.get('passport', '').strip(),
                'pinfl': request.form.get('pinfl', '').strip(),
            }
            score_result = score_new_client(form)
            score_result = _normalize_score_result(score_result)
            score_result['form_data'] = {
                'product_amount': product, 'advance_pct': advance_pct,
                'term_months': term_months,
            }
            try:
                log_audit(u['username'], 'score_new', 'client', details=f"class={score_result.get('risk_class')}")
            except Exception:
                pass
        except Exception as e:
            score_result = {'error': str(e)}

    return render_template_string(SCORING_T, user=u, active='scoring',
        results=results, score_result=score_result,
        search_error=search_error, query=query,
        BRANCHES=BRANCHES, NAV=NAV, CSS=CSS,
        INCOME_SOURCES=INCOME_SOURCES,
        POSITIONS=EMPLOYMENT_TYPES)

# ═══ SCORING PRINT — маршруты /scoring/print и /developer/score-pdf определены ниже (одна view scoring_print)

# ═══ ALERTS ═══

def _collection_stage_meta(overdue_days: float | int | None):
    d = int(overdue_days or 0)
    if d > 90:
        return ("legal", "HIGH", 5)
    if d >= 43:
        return ("collection", "MEDIUM", 3)
    if d >= 1:
        return ("call_center", "LOW", 2)
    return (None, None, None)


def _normalize_workflow_overdue_days(overdue_days: float | int | None) -> int:
    """Normalize legacy outliers for workflow display/ordering."""
    d = int(overdue_days or 0)
    if d < 0:
        return 0
    # Business workflow uses stage ranges up to legal 90+; legacy data can contain
    # extreme values (thousands of days) that break UX without adding decision value.
    return min(d, 180)


def _sync_collection_alerts(db, branch_filter=None):
    """Auto-classify contracts into collection workflow alerts."""
    today = datetime.now().date()
    flt = "AND c.branch=?" if branch_filter else ""
    params = ([branch_filter] if branch_filter else [])
    contracts = db.execute(f"""
        SELECT c.id AS contract_id, c.client_id, c.branch, c.responsible_person,
               CAST(COALESCE(c.total_late_days,0) AS INTEGER) AS overdue_days,
               c.debt_amount, cl.full_name
        FROM contracts c
        JOIN clients cl ON cl.id = c.client_id
        WHERE CAST(COALESCE(c.total_late_days,0) AS INTEGER) >= 1
          AND COALESCE(c.debt_amount,0) > 0
          AND (c.contract_end_date IS NULL OR TRIM(c.contract_end_date)='' OR date(c.contract_end_date) >= date('now'))
          {flt}
    """, params).fetchall()
    # #region agent log
    try:
        ods = [int((dict(x).get("overdue_days") or 0)) for x in contracts[:5000]]
        _dbg745(
            "H-alerts-overdue-source",
            "main.py:_sync_collection_alerts",
            "overdue_source_stats",
            {
                "branch_filter": branch_filter,
                "contracts_source": len(contracts),
                "min_overdue": min(ods) if ods else 0,
                "max_overdue": max(ods) if ods else 0,
                "p95_overdue": sorted(ods)[int(len(ods) * 0.95)] if ods else 0,
            },
        )
    except Exception:
        pass
    # #endregion

    active_keys = set()
    created_cnt = 0
    updated_cnt = 0
    now_iso = datetime.now().isoformat()
    for r in contracts:
        rr = dict(r)
        raw_overdue_days = int(rr.get("overdue_days") or 0)
        norm_overdue_days = _normalize_workflow_overdue_days(raw_overdue_days)
        stage, priority, days_limit = _collection_stage_meta(raw_overdue_days)
        if not stage:
            continue
        key = (rr["client_id"], rr.get("branch") or "", stage)
        active_keys.add(key)
        existing = db.execute(
            """
            SELECT id, status, created_at, details
            FROM alerts
            WHERE alert_type='collection_workflow'
              AND client_id=?
              AND branch_id=?
              AND stage=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (rr["client_id"], rr.get("branch") or "", stage),
        ).fetchone()
        if existing:
            ex = dict(existing)
            created_at = ex.get("created_at") or now_iso
            try:
                created_date = datetime.fromisoformat(created_at).date()
            except Exception:
                created_date = today
            deadline_date = (created_date + timedelta(days=days_limit)).isoformat()
            status = ex.get("status") or "pending"
            if status != "done" and today > (created_date + timedelta(days=days_limit)):
                status = "overdue"
            db.execute(
                """
                UPDATE alerts
                SET overdue_days=?, assigned_to=?, status=?, deadline_date=?, priority=?, updated_at=?, branch=?, entity_id=?, message=?, severity=?
                WHERE id=?
                """,
                (
                    norm_overdue_days,
                    rr.get("responsible_person"),
                    status,
                    deadline_date,
                    priority,
                    now_iso,
                    rr.get("branch"),
                    rr.get("contract_id"),
                    f"{rr.get('full_name','Клиент')} | stage={stage} | overdue={norm_overdue_days}",
                    "critical" if priority == "HIGH" else ("warning" if priority == "MEDIUM" else "info"),
                    ex["id"],
                ),
            )
            updated_cnt += 1
        else:
            created_date = today
            deadline_date = (created_date + timedelta(days=days_limit)).isoformat()
            db.execute(
                """
                INSERT INTO alerts (
                    alert_type, severity, message, entity_type, entity_id, branch, details, created_at, updated_at,
                    client_id, branch_id, stage, overdue_days, assigned_to, status, deadline_date, priority
                )
                VALUES ('collection_workflow',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "critical" if priority == "HIGH" else ("warning" if priority == "MEDIUM" else "info"),
                    f"{rr.get('full_name','Клиент')} | stage={stage} | overdue={norm_overdue_days}",
                    "contract",
                    rr.get("contract_id"),
                    rr.get("branch"),
                    json.dumps({
                        "legal_steps": {
                            "demand_letter": False,
                            "ssp_claim_created": False,
                            "ssp_claim_approved": False,
                            "court_fee_paid": False
                        }
                    }, ensure_ascii=False),
                    now_iso,
                    now_iso,
                    rr.get("client_id"),
                    rr.get("branch"),
                    stage,
                    norm_overdue_days,
                    rr.get("responsible_person"),
                    "pending",
                    deadline_date,
                    priority,
                ),
            )
            created_cnt += 1

    stale = db.execute(
        "SELECT id, client_id, COALESCE(branch_id,'') AS branch_id_eff, stage, status FROM alerts WHERE alert_type='collection_workflow'"
    ).fetchall()
    for s in stale:
        sid, cid, br, st, status = s["id"], s["client_id"], s["branch_id_eff"], s["stage"], s["status"]
        if (cid, br, st) not in active_keys and status != "done":
            db.execute("UPDATE alerts SET status='done', updated_at=? WHERE id=?", (now_iso, sid))
    # #region agent log
    _dbg745(
        "H-alert-workflow",
        "main.py:_sync_collection_alerts",
        "workflow_sync",
        {
            "branch_filter": branch_filter,
            "contracts_source": len(contracts),
            "created": created_cnt,
            "updated": updated_cnt,
            "active_keys": len(active_keys),
        },
    )
    _dbg745(
        "H-alerts-overdue-normalized",
        "main.py:_sync_collection_alerts",
        "overdue_normalized_sample",
        {
            "sample": [
                {
                    "contract_id": dict(x).get("contract_id"),
                    "raw": int(dict(x).get("overdue_days") or 0),
                    "normalized": _normalize_workflow_overdue_days(dict(x).get("overdue_days")),
                }
                for x in contracts[:12]
            ],
            "normalized_max_expected": 180,
        },
    )
    # #endregion


def _get_collection_alert_rows(db, branch_filter=None, stage_filter="", status_filter="", overdue_filter=""):
    flt = ["alert_type='collection_workflow'"]
    p = []
    if branch_filter:
        flt.append("branch_id=?")
        p.append(branch_filter)
    if stage_filter:
        flt.append("stage=?")
        p.append(stage_filter)
    if status_filter:
        flt.append("status=?")
        p.append(status_filter)
    if overdue_filter == "1_43":
        flt.append("overdue_days BETWEEN 1 AND 43")
    elif overdue_filter == "43_90":
        flt.append("overdue_days BETWEEN 43 AND 90")
    elif overdue_filter == "90p":
        flt.append("overdue_days > 90")
    where = " AND ".join(flt)
    rows = db.execute(f"""
        SELECT a.id, a.client_id, a.branch_id, a.stage, a.overdue_days, a.assigned_to,
               a.status, a.deadline_date, a.priority, c.full_name
        FROM alerts a
        LEFT JOIN clients c ON c.id = a.client_id
        WHERE {where}
        ORDER BY
            CASE a.priority WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,
            CASE a.status WHEN 'overdue' THEN 0 WHEN 'in_progress' THEN 1 WHEN 'pending' THEN 2 ELSE 3 END,
            a.overdue_days DESC, a.id DESC
        LIMIT 500
    """, p).fetchall()
    out = [dict(r) for r in rows]
    stage_labels = {
        "call_center": "Ундирув оператор",
        "collection": "Ундирувчи",
        "legal": "Юрист",
    }
    status_labels = {
        "pending": "Ожидает",
        "in_progress": "В работе",
        "done": "Выполнено",
        "overdue": "Просрочено",
    }
    priority_labels = {
        "HIGH": "Высокий",
        "MEDIUM": "Средний",
        "LOW": "Низкий",
    }
    for r in out:
        r["stage_label"] = stage_labels.get(r.get("stage"), r.get("stage") or "")
        r["status_label"] = status_labels.get(r.get("status"), r.get("status") or "")
        r["priority_label"] = priority_labels.get(r.get("priority"), r.get("priority") or "")
    # #region agent log
    _dbg745(
        "H-workflow-filters",
        "main.py:_get_collection_alert_rows",
        "workflow_rows",
        {
            "branch_filter": branch_filter,
            "stage_filter": stage_filter,
            "status_filter": status_filter,
            "overdue_filter": overdue_filter,
            "rows": len(out),
            "sample": [{"id": r.get("id"), "stage": r.get("stage"), "status": r.get("status"), "overdue_days": r.get("overdue_days")} for r in out[:6]],
        },
    )
    # #endregion
    return out

def generate_alerts(db, branch_filter=None, days_ago=0):
    """Генерирует список актуальных алертов из БД. Пороги из config.settings (смягчены ~30%). days_ago: 0=всё время, 7/30/90=только договоры за период."""
    flt = "AND branch=?" if branch_filter else ""
    date_flt = "AND contract_date >= date('now', '-' || ? || ' days')" if days_ago and days_ago > 0 else ""
    p = ([days_ago] if days_ago and days_ago > 0 else []) + ([branch_filter] if branch_filter else [])
    alerts = []

    debt_ok = " AND debt_amount > 0"
    # 1. Клиенты с просрочкой 25–30 дней (почти МИБ)
    near_mib = db.execute(f"""
        SELECT COUNT(*) as cnt FROM contracts
        WHERE total_late_days BETWEEN 25 AND 30 {debt_ok} {date_flt} {flt}
    """, p).fetchone()['cnt']
    if near_mib >= ALERT_NEAR_MIB_MIN:
        alerts.append({
            'id': 'near_mib', 'type': 'critical', 'icon': '🔴',
            'title': f'{near_mib} договоров в 5 днях от МИБ',
            'text': 'Договоры с просрочкой 25–30 дней. Если не погасят — перейдут в МИБ. Немедленно связаться с клиентами.',
            'action_url': '/alerts/detail/near_mib',
            'branch': branch_filter
        })

    # 2. Новые дефолты за последние N дней
    new_npl = db.execute(f"""
        SELECT COUNT(*) as cnt FROM contracts
        WHERE status_detail IN ('Ёмон','МИБ','Судда')
        AND contract_date >= date('now','-{ALERT_NEW_NPL_DAYS} days') {debt_ok} {flt}
    """, ([branch_filter] if branch_filter else [])).fetchone()['cnt']
    if new_npl >= ALERT_NEW_NPL_MIN:
        alerts.append({
            'id': 'new_npl_7d', 'type': 'critical', 'icon': '🔴',
            'title': f'{new_npl} новых дефолтов за {ALERT_NEW_NPL_DAYS} дней',
            'text': 'Новые договоры перешли в статус НПЛ. Требуется анализ причин.',
            'action_url': '/alerts/detail/new_npl_7d',
            'branch': branch_filter
        })

    # 3. Сотрудник с НПЛ выше порога (смягчённый)
    bad_emp = db.execute(f"""
        SELECT responsible_person AS employee_name,
               ROUND(100.0*SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') THEN 1 ELSE 0 END)/COUNT(*),1) AS npl_rate,
               COUNT(*) AS total
        FROM contracts WHERE responsible_person IS NOT NULL AND TRIM(responsible_person) != '' {date_flt} {flt}
        GROUP BY responsible_person HAVING total > ? AND npl_rate > ?
        ORDER BY npl_rate DESC LIMIT 3
    """, p + [ALERT_EMPLOYEE_MIN_CONTRACTS - 1, ALERT_EMPLOYEE_NPL_PCT]).fetchall()
    for i, emp in enumerate(bad_emp):
        alerts.append({
            'id': f"emp_{i}", 'type': 'warning', 'icon': '🟡',
            'title': f"Высокий НПЛ: {emp['employee_name']} — {emp['npl_rate']}%",
            'text': f"Сотрудник имеет НПЛ {emp['npl_rate']}% при {emp['total']} договорах. Рекомендуется проверка и временное ограничение лимита выдач.",
            'action_url': None, 'branch': branch_filter
        })

    # 4. Ранний дефолт (< 90 дней с выдачи)
    early_def = db.execute(f"""
        SELECT COUNT(*) as cnt FROM contracts
        WHERE (
            CAST(COALESCE(total_late_days,0) AS REAL) >= 90
            OR TRIM(COALESCE(status_detail,'')) IN ('МИБ','Судда')
        )
        AND TRIM(COALESCE(status_detail,'')) IN ('Ёмон','Емон','МИБ','Судда')
        AND (contract_end_date IS NULL OR date(contract_end_date) >= date('now'))
        AND julianday('now') - julianday(contract_date) < 90 {debt_ok} {date_flt} {flt}
    """, p).fetchone()['cnt']
    if early_def >= ALERT_EARLY_DEFAULT_MIN:
        alerts.append({
            'id': 'early_default', 'type': 'critical', 'icon': '🔴',
            'title': f'Ранний дефолт: {early_def} договоров до 90 дней',
            'text': 'Клиенты дефолтируют в первые 3 месяца — признак ошибки скоринга или мошенничества.',
            'action_url': '/alerts/detail/early_default',
            'branch': branch_filter
        })

    # 5. Филиал с НПЛ выше порога (смягчённый)
    high_branch = db.execute(f"""
        SELECT branch,
               ROUND(100.0*SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') THEN 1 ELSE 0 END)/COUNT(*),1) AS npl_rate
        FROM contracts WHERE 1=1 {date_flt} {flt}
        GROUP BY branch
        HAVING npl_rate > ? AND COUNT(*) > ?
    """, p + [ALERT_BRANCH_NPL_PCT, ALERT_BRANCH_MIN_CONTRACTS - 1]).fetchall()
    for br in high_branch:
        alerts.append({
            'id': f"branch_{br['branch']}".replace(' ', '_'), 'type': 'warning', 'icon': '🟡',
            'title': f"НПЛ {br['branch']} превысил {ALERT_BRANCH_NPL_PCT}%: {br['npl_rate']}%",
            'text': f"Филиал {br['branch']} показывает НПЛ выше нормы. Рекомендуется ограничить лимиты выдачи.",
            'action_url': None, 'branch': br['branch']
        })

    # 6. Просрочка 1–24 дней (предупреждение, без пересечения с near_mib 25–30)
    early_delay = db.execute(f"""
        SELECT COUNT(*) as cnt FROM contracts
        WHERE total_late_days BETWEEN 1 AND 24
        AND status_detail NOT IN ('Ёмон','МИБ','Судда') {debt_ok} {date_flt} {flt}
    """, p).fetchone()['cnt']
    if early_delay >= ALERT_EARLY_DELAY_MIN:
        alerts.append({
            'id': 'early_delay', 'type': 'info', 'icon': '🔵',
            'title': f'{early_delay} договоров с ранней просрочкой (1–24 дней)',
            'text': 'Договоры ещё не в НПЛ, но требуют внимания. Рекомендуется превентивный звонок клиентам.',
            'action_url': '/alerts/detail/early_delay',
            'branch': branch_filter
        })

    # 7. Позитив: лучший месяц (смягчённый мин. договоров)
    best_month = db.execute(f"""
        SELECT strftime('%Y-%m', contract_date) AS month,
               ROUND(100.0*SUM(CASE WHEN status_detail='Стандарт' THEN 1 ELSE 0 END)/COUNT(*),1) AS collect_rate
        FROM contracts WHERE contract_date >= date('now','-6 months') {flt}
        GROUP BY month HAVING COUNT(*) > ?
        ORDER BY collect_rate DESC LIMIT 1
    """, ([branch_filter] if branch_filter else []) + [ALERT_BEST_MONTH_MIN_CONTRACTS - 1]).fetchone()
    if best_month:
        alerts.append({
            'id': 'best_month', 'type': 'positive', 'icon': '🟢',
            'title': f"Лучший сбор за {best_month['month']}: {best_month['collect_rate']}%",
            'text': "Рекордный показатель сбора за последние 6 месяцев. Сохраняйте темп!",
            'action_url': None, 'branch': branch_filter
        })

    order = {'critical': 0, 'warning': 1, 'info': 2, 'positive': 3}
    alerts.sort(key=lambda x: order.get(x['type'], 9))
    return alerts


# /api/alerts-count — зарегистрирован в routes/api_routes.py


@app.route('/alerts/detail/<alert_id>')
@login_required
@role_required('alerts')
def alert_detail(alert_id):
    u = session['user']
    b_user = u.get('branch')
    b_arg = (request.args.get('branch') or '').strip()
    b_eff = b_user or (b_arg if b_arg else None)
    flt = "AND c.branch=?" if b_eff else ""
    p = ([b_eff] if b_eff else [])
    db = get_db()
    rows = []
    title = alert_id

    debt_positive = " AND c.debt_amount > 0"
    if alert_id == 'near_mib':
        title = "Договоры в 5 днях от МИБ (25–30 дней просрочки)"
        rows = db.execute(f"""
            SELECT cl.full_name, c.branch, c.client_id,
                   c.responsible_person AS employee_name,
                   c.total_late_days AS delay_days,
                   c.debt_amount AS total_debt,
                   (SELECT COALESCE(SUM(debt_amount),0) FROM contracts WHERE client_id=c.client_id) AS client_total_debt,
                   c.status_detail AS status, c.id AS cid
            FROM contracts c JOIN clients cl ON cl.id=c.client_id
            WHERE c.total_late_days BETWEEN 25 AND 30{debt_positive} {flt}
            ORDER BY c.total_late_days DESC, c.branch ASC, c.id ASC
        """, p).fetchall()

    elif alert_id == 'new_npl_7d':
        title = "Новые дефолты за 7 дней"
        rows = db.execute(f"""
            SELECT cl.full_name, c.branch, c.client_id,
                   c.responsible_person AS employee_name,
                   c.total_late_days AS delay_days,
                   c.debt_amount AS total_debt,
                   (SELECT COALESCE(SUM(debt_amount),0) FROM contracts WHERE client_id=c.client_id) AS client_total_debt,
                   c.status_detail AS status, c.id AS cid
            FROM contracts c JOIN clients cl ON cl.id=c.client_id
            WHERE c.status_detail IN ('Ёмон','МИБ','Судда')
            AND c.contract_date >= date('now','-7 days'){debt_positive} {flt}
            ORDER BY c.debt_amount DESC, c.branch ASC, c.id ASC
        """, p).fetchall()

    elif alert_id == 'early_default':
        title = "Ранний дефолт (до 90 дней с выдачи)"
        rows = db.execute(f"""
            SELECT cl.full_name, c.branch, c.client_id,
                   c.responsible_person AS employee_name,
                   c.total_late_days AS delay_days,
                   c.debt_amount AS total_debt,
                   (SELECT COALESCE(SUM(debt_amount),0) FROM contracts WHERE client_id=c.client_id) AS client_total_debt,
                   c.status_detail AS status, c.id AS cid, c.contract_date,
                   c.contract_end_date AS contract_end_date,
                   CAST(julianday('now') - julianday(c.contract_date) AS INTEGER) AS age_days
            FROM contracts c JOIN clients cl ON cl.id=c.client_id
            WHERE (
                CAST(COALESCE(c.total_late_days,0) AS REAL) >= 90
                OR TRIM(COALESCE(c.status_detail,'')) IN ('МИБ','Судда')
            )
            AND TRIM(COALESCE(c.status_detail,'')) IN ('Ёмон','Емон','МИБ','Судда')
            AND (c.contract_end_date IS NULL OR date(c.contract_end_date) >= date('now'))
            AND julianday('now') - julianday(c.contract_date) < 90{debt_positive} {flt}
            ORDER BY age_days ASC, c.branch ASC, c.id ASC
        """, p).fetchall()

    elif alert_id == 'early_delay':
        title = "Ранняя просрочка 1–24 дней"
        rows = db.execute(f"""
            SELECT cl.full_name, c.branch, c.client_id,
                   c.responsible_person AS employee_name,
                   c.total_late_days AS delay_days,
                   c.debt_amount AS total_debt,
                   (SELECT COALESCE(SUM(debt_amount),0) FROM contracts WHERE client_id=c.client_id) AS client_total_debt,
                   c.status_detail AS status, c.id AS cid
            FROM contracts c JOIN clients cl ON cl.id=c.client_id
            WHERE c.total_late_days BETWEEN 1 AND 24
            AND c.status_detail NOT IN ('Ёмон','МИБ','Судда'){debt_positive} {flt}
            ORDER BY c.total_late_days DESC, c.branch ASC, c.id ASC LIMIT 100
        """, p).fetchall()

    db.close()
    return render_template_string(ALERT_DETAIL_T, user=u, active='alerts',
        title=title, rows=[dict(r) for r in rows],
        alert_id=alert_id,
        back_qs=("?branch=" + b_arg) if (b_arg and not b_user) else "",
        NAV=NAV, CSS=CSS)


@app.route('/alerts')
@login_required
@role_required('alerts')
def alerts():
    u = session['user']
    b = u.get('branch')
    flt_type = request.args.get('type', '')
    flt_branch = request.args.get('branch', b or '')
    flt_days = request.args.get('days', '')
    flt_from = request.args.get('from', '')
    flt_to = request.args.get('to', '')
    c_stage = (request.args.get('c_stage') or '').strip()
    c_status = (request.args.get('c_status') or '').strip()
    c_overdue = (request.args.get('c_overdue') or '').strip()
    days_ago = int(flt_days) if flt_days and flt_days.isdigit() else 0
    db = get_db()
    _sync_collection_alerts(db, branch_filter=flt_branch or b or None)
    all_alerts = generate_alerts(db, branch_filter=flt_branch or b or None, days_ago=days_ago)
    collection_rows = _get_collection_alert_rows(
        db,
        branch_filter=flt_branch or b or None,
        stage_filter=c_stage,
        status_filter=c_status,
        overdue_filter=c_overdue,
    )
    if flt_from:
        all_alerts = [a for a in all_alerts if not a.get('created_at') or a.get('created_at', '') >= flt_from]
    if flt_to:
        all_alerts = [a for a in all_alerts if not a.get('created_at') or a.get('created_at', '') <= flt_to]
    db.close()
    if flt_type:
        all_alerts = [a for a in all_alerts if a['type'] == flt_type]
    # carry filters into detail links (branch + days)
    try:
        from urllib.parse import urlencode
        q = {}
        if flt_branch:
            q["branch"] = flt_branch
        if flt_days:
            q["days"] = flt_days
        qs = ("?" + urlencode(q)) if q else ""
        for a in all_alerts:
            if a.get("action_url"):
                a["action_url"] = a["action_url"] + qs
    except Exception:
        pass
    critical = sum(1 for a in all_alerts if a['type'] == 'critical')
    warning = sum(1 for a in all_alerts if a['type'] == 'warning')
    return render_template_string(ALERTS_T, user=u, active='alerts',
        alerts=all_alerts, critical=critical, warning=warning,
        collection_rows=collection_rows,
        c_stage=c_stage, c_status=c_status, c_overdue=c_overdue,
        flt_type=flt_type, flt_branch=flt_branch, flt_days=flt_days,
        flt_from=flt_from, flt_to=flt_to,
        BRANCHES=BRANCHES, NAV=NAV, CSS=CSS)


@app.route('/api/alerts/workflow/update', methods=['POST'])
@login_required
@role_required('alerts')
def api_alerts_workflow_update():
    data = request.get_json(silent=True) or {}
    alert_id = data.get('id')
    new_status = (data.get('status') or '').strip()
    legal_steps = data.get('legal_steps')
    if not alert_id:
        return jsonify({"ok": False, "error": "id is required"}), 400
    if new_status and new_status not in ('pending', 'in_progress', 'done', 'overdue'):
        return jsonify({"ok": False, "error": "invalid status"}), 400
    db = get_db()
    row = db.execute("SELECT id, details FROM alerts WHERE id=? AND alert_type='collection_workflow'", (alert_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({"ok": False, "error": "alert not found"}), 404
    details = {}
    try:
        details = json.loads(row["details"] or "{}")
    except Exception:
        details = {}
    if isinstance(legal_steps, dict):
        details["legal_steps"] = {
            "demand_letter": bool(legal_steps.get("demand_letter")),
            "ssp_claim_created": bool(legal_steps.get("ssp_claim_created")),
            "ssp_claim_approved": bool(legal_steps.get("ssp_claim_approved")),
            "court_fee_paid": bool(legal_steps.get("court_fee_paid")),
        }
    if not new_status:
        new_status = db.execute("SELECT status FROM alerts WHERE id=?", (alert_id,)).fetchone()["status"]
    db.execute(
        "UPDATE alerts SET status=?, details=?, updated_at=? WHERE id=?",
        (new_status, json.dumps(details, ensure_ascii=False), datetime.now().isoformat(), alert_id),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True})

# Geo API: /api/geo/regions, /api/geo/mfy/<id> — in routes/api_routes.py

# ═══ PORTFOLIO ═══

@app.route('/portfolio')
@login_required
@role_required('portfolio')
def portfolio():
    from core.kpi import _build_debt_filter_sql
    u = session['user']
    b = u.get('branch')
    flt = "AND branch=?" if b else ""
    p = ([b] if b else [])

    year = request.args.get('year', '').strip()
    branch_f = request.args.get('branch', b or '').strip()
    product_f = request.args.get('product', '').strip()
    debt_filters = request.args.getlist('debt_filter')

    dyn_flt = flt
    dyn_p = list(p)
    if year:
        dyn_flt += " AND strftime('%Y',contract_date)=?"
        dyn_p.append(year)
    if product_f:
        dyn_flt += " AND COALESCE(NULLIF(TRIM(product_type),''),'Не указан')=?"
        dyn_p.append(product_f)
    if branch_f and not b:
        dyn_flt += " AND branch=?"
        dyn_p.append(branch_f)
    debt_sql = _build_debt_filter_sql(debt_filters, dyn_p, col="debt_amount")
    dyn_flt += debt_sql

    db = get_db()

    kpi_row = db.execute(f"""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90 THEN 1 ELSE 0 END) as npl,
               ROUND(100.0*SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90
                     THEN 1 ELSE 0 END)/COUNT(*),2) as npl_rate,
               ROUND(100.0*SUM(CASE WHEN status_detail='Стандарт'
                     THEN 1 ELSE 0 END)/COUNT(*),1) as collection_rate,
               COALESCE(SUM(debt_amount),0) as portfolio,
               COALESCE(SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90
                     THEN debt_amount ELSE 0 END),0) as npl_debt
        FROM contracts WHERE 1=1 {dyn_flt}
    """, dyn_p).fetchone()
    kpi = dict(kpi_row) if kpi_row else {}

    by_branch = [dict(r) for r in db.execute(f"""
        SELECT branch,
               COUNT(*) as total,
               SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90 THEN 1 ELSE 0 END) as npl_count,
               ROUND(100.0*SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90
                     THEN 1 ELSE 0 END)/COUNT(*),1) as npl_rate,
               ROUND(100.0*SUM(CASE WHEN status_detail='Стандарт'
                     THEN 1 ELSE 0 END)/COUNT(*),1) as collection_rate,
               COALESCE(SUM(debt_amount),0) as portfolio
        FROM contracts WHERE 1=1 {dyn_flt}
        GROUP BY branch ORDER BY branch
    """, dyn_p).fetchall()]

    by_product = [dict(r) for r in db.execute(f"""
        SELECT COALESCE(product_type,'Не указан') as product_type,
               COUNT(*) as total,
               SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90 THEN 1 ELSE 0 END) as npl,
               ROUND(100.0*SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90
                     THEN 1 ELSE 0 END)/COUNT(*),1) as npl_rate,
               COALESCE(SUM(debt_amount),0) as portfolio
        FROM contracts WHERE 1=1 {dyn_flt}
        GROUP BY product_type ORDER BY total DESC LIMIT 15
    """, dyn_p).fetchall()]

    aging_row = db.execute(f"""
        SELECT
          SUM(CASE WHEN COALESCE(total_late_days,0)=0 THEN 1 ELSE 0 END) as d0,
          SUM(CASE WHEN total_late_days BETWEEN 1 AND 30 THEN 1 ELSE 0 END) as d30,
          SUM(CASE WHEN total_late_days BETWEEN 31 AND 60 THEN 1 ELSE 0 END) as d60,
          SUM(CASE WHEN total_late_days BETWEEN 61 AND 90 THEN 1 ELSE 0 END) as d90,
          SUM(CASE WHEN total_late_days > 90 THEN 1 ELSE 0 END) as d90p,
          SUM(CASE WHEN COALESCE(total_late_days,0)=0 THEN debt_amount ELSE 0 END) as d0_sum,
          SUM(CASE WHEN total_late_days BETWEEN 1 AND 30 THEN debt_amount ELSE 0 END) as d30_sum,
          SUM(CASE WHEN total_late_days BETWEEN 31 AND 60 THEN debt_amount ELSE 0 END) as d60_sum,
          SUM(CASE WHEN total_late_days BETWEEN 61 AND 90 THEN debt_amount ELSE 0 END) as d90_sum,
          SUM(CASE WHEN total_late_days > 90 THEN debt_amount ELSE 0 END) as d90p_sum
        FROM contracts WHERE 1=1 {dyn_flt}
    """, dyn_p).fetchone()
    aging = dict(aging_row) if aging_row else {}

    # Полный список годов и продуктов для выпадающих списков (не зависит от текущих фильтров)
    years = [r['year'] for r in db.execute(
        "SELECT DISTINCT strftime('%Y',contract_date) as year "
        "FROM contracts WHERE contract_date IS NOT NULL ORDER BY year DESC"
    ).fetchall()]
    products = [r['pt'] for r in db.execute(
        "SELECT DISTINCT COALESCE(NULLIF(TRIM(product_type),''),'Не указан') as pt "
        "FROM contracts ORDER BY pt LIMIT 30"
    ).fetchall()]

    db.close()

    # ТЗ 11: сегментация (продукт×сегмент, регион×МФЙ)
    from core.kpi import get_portfolio_segments
    seg = get_portfolio_segments(
        branch=branch_f if branch_f and not b else b,
        year=year or None,
        product_type=product_f or None,
        debt_bands=debt_filters or None,
    )
    # #region agent log
    _dbg745(
        "H-portfolio-empty-data",
        "main.py:portfolio",
        "portfolio_segments_page",
        {
            "year": year,
            "branch_f": branch_f,
            "product_f": product_f,
            "debt_filters": debt_filters,
            "products_segments": len(seg.get("products_segments", []) or []),
            "regions_mfy": len(seg.get("regions_mfy", []) or []),
            "kpi_total": int((kpi or {}).get("total") or 0),
        },
    )
    # #endregion

    return render_template_string(PORTFOLIO_T, user=u, active='portfolio',
        kpi=kpi, by_branch=by_branch, by_product=by_product,
        aging=aging, years=years, products=products,
        year=year, branch_f=branch_f, product_f=product_f,
        debt_filters=debt_filters, DEBT_BANDS=DEBT_BANDS,
        products_segments=seg.get('products_segments', []),
        regions_mfy=seg.get('regions_mfy', []),
        debt_threshold=seg.get('threshold', 4000000),
        BRANCHES=BRANCHES, NAV=NAV, CSS=CSS)


@app.route('/export/portfolio')
@login_required
@role_required('portfolio')
def export_portfolio():
    import io
    from core.kpi import _build_debt_filter_sql
    u = session['user']
    b = u.get('branch')
    year = request.args.get('year', '')
    branch_f = request.args.get('branch', b or '')
    product_f = request.args.get('product', '')
    debt_filters = request.args.getlist('debt_filter')

    flt = "WHERE 1=1"
    p = []
    if b:
        flt += " AND c.branch=?"
        p.append(b)
    if year:
        flt += " AND strftime('%Y',c.contract_date)=?"
        p.append(year)
    if branch_f and not b:
        flt += " AND c.branch=?"
        p.append(branch_f)
    if product_f:
        flt += " AND COALESCE(NULLIF(TRIM(c.product_type),''),'Не указан')=?"
        p.append(product_f)
    flt += _build_debt_filter_sql(debt_filters, p, col="c.debt_amount")

    db = get_db()
    rows = db.execute(f"""
        SELECT c.id, cl.full_name, c.branch,
               c.responsible_person as employee_name,
               c.product_type, c.contract_date,
               c.debt_amount as total_debt, c.total_late_days as delay_days,
               c.status_detail as status
        FROM contracts c JOIN clients cl ON cl.id=c.client_id
        {flt} ORDER BY c.branch, c.status_detail
    """, p).fetchall()
    db.close()

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        from flask import make_response
        return make_response("openpyxl не установлен. pip install openpyxl", 500)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Портфель"
    headers = ['ID', 'ФИО', 'Филиал', 'Сотрудник', 'Продукт', 'Дата', 'Долг', 'Дн.проср.', 'Статус']
    hdr_fill = PatternFill("solid", fgColor="1E293B")
    hdr_font = Font(color="FFFFFF", bold=True)
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(1, ci, h)
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = Alignment(horizontal='center')
    status_colors = {'Ёмон': 'FCA5A5', 'МИБ': 'FCA5A5', 'МИБда': 'FCA5A5', 'Судда': 'FCA5A5', 'Стандарт': 'BBF7D0'}
    for ri, row in enumerate(rows, 2):
        r = dict(row)
        vals = [r.get('id'), r.get('full_name'), r.get('branch'), r.get('employee_name'),
                r.get('product_type'), r.get('contract_date'),
                r.get('total_debt'), r.get('delay_days'), r.get('status')]
        for ci, v in enumerate(vals, 1):
            ws.cell(ri, ci, v)
        sc = status_colors.get(r.get('status'), '')
        if sc:
            for ci in range(1, 10):
                ws.cell(ri, ci).fill = PatternFill("solid", fgColor=sc)
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = 18

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    try:
        from datetime import datetime as dt
        export_name = 'portfolio_%s_%s.xlsx' % (year or 'all', dt.now().strftime('%Y%m%d_%H%M'))
        export_path = os.path.join(EXPORT_DIR, export_name)
        with open(export_path, 'wb') as f:
            f.write(buf.getvalue())
        buf.seek(0)
    except Exception:
        pass
    try:
        log_audit(u.get('username') or u.get('name', ''), 'export', details='portfolio')
    except Exception:
        pass
    return send_file(buf, as_attachment=True,
                     download_name=f'portfolio_{year or "all"}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

# ═══ ADMIN ═══

@app.route('/admin', methods=['GET', 'POST'])
@login_required
def admin():
    u = session['user']
    if u.get('role') not in ('director', 'executive') and 'admin' not in (u.get('modules') or []):
        return redirect('/')
    msg = None
    error = None
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'create_user':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '').strip()
            role = request.form.get('role', 'credit_officer')
            branch = request.form.get('branch', '')
            if not username or not password:
                error = "Логин и пароль обязательны"
            else:
                try:
                    db = get_db()
                    exists = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
                    if exists:
                        error = f"Пользователь «{username}» уже существует"
                        db.close()
                    else:
                        from core.database import _hash_password
                        pw_hash = _hash_password(password)
                        db.execute(
                            "INSERT INTO users (username, password_hash, name, role, branch, is_active, created_at) "
                            "VALUES (?,?,?,?,?,1,?)",
                            (username, pw_hash, username, role, branch or None, datetime.now().isoformat())
                        )
                        db.commit()
                        log_audit(u['username'], 'create_user', 'user', details=f'{username}/{role}')
                        msg = f"Пользователь «{username}» создан"
                        db.close()
                except Exception as e:
                    error = str(e)
        elif action == 'toggle_user':
            uid = request.form.get('uid')
            active = int(request.form.get('active', 1))
            try:
                db = get_db()
                db.execute("UPDATE users SET is_active=? WHERE id=?", (active, uid))
                db.commit()
                log_audit(u['username'], 'toggle_user', 'user', entity_id=int(uid) if uid and str(uid).isdigit() else None, details='active=' + str(active))
                msg = "Статус обновлён"
                db.close()
            except Exception as e:
                error = str(e)
        elif action == 'change_password':
            uid = request.form.get('uid')
            new_pass = request.form.get('new_password', '').strip()
            if len(new_pass) < 6:
                error = "Пароль минимум 6 символов"
            else:
                try:
                    from core.database import _hash_password
                    pw_hash = _hash_password(new_pass)
                    db = get_db()
                    db.execute("UPDATE users SET password_hash=? WHERE id=?", (pw_hash, uid))
                    db.commit()
                    log_audit(u['username'], 'change_password', 'user', entity_id=int(uid) if uid and str(uid).isdigit() else None)
                    msg = "Пароль изменён"
                    db.close()
                except Exception as e:
                    error = str(e)
        elif action == 'change_role':
            uid = request.form.get('uid')
            new_role = request.form.get('new_role')
            new_branch = request.form.get('new_branch', '')
            try:
                db = get_db()
                db.execute("UPDATE users SET role=?, branch=? WHERE id=?", (new_role, new_branch or None, uid))
                db.commit()
                log_audit(u['username'], 'change_role', 'user', entity_id=int(uid) if uid and str(uid).isdigit() else None, details=f'role={new_role}')
                msg = "Роль обновлена"
                db.close()
            except Exception as e:
                error = str(e)
    db = get_db()
    # Директор/executive видят всех; остальные с модулем admin — только свой филиал
    user_branch = u.get('branch')
    if user_branch and u.get('role') not in ('director', 'executive'):
        users = [dict(r) for r in db.execute(
            "SELECT id, username, name, role, branch, is_active, COALESCE(is_active,1) as active FROM users WHERE branch = ? OR branch IS NULL OR branch = '' ORDER BY role, username",
            (user_branch,)
        ).fetchall()]
    else:
        users = [dict(r) for r in db.execute(
            "SELECT id, username, name, role, branch, is_active, COALESCE(is_active,1) as active FROM users ORDER BY role, username"
        ).fetchall()]
    audit_query = request.args.get('audit_q', '')
    audit_sql = "SELECT * FROM audit_log WHERE 1=1"
    audit_p = []
    if audit_query:
        audit_sql += " AND (user LIKE ? OR action LIKE ? OR details LIKE ?)"
        audit_p.extend([f'%{audit_query}%'] * 3)
    audit_sql += " ORDER BY created_at DESC LIMIT 100"
    audit_rows = []
    if _table_exists(db, 'audit_log'):
        audit_rows = [dict(r) for r in db.execute(audit_sql, audit_p).fetchall()]
    from config.settings import DB_PATH
    db_path = DB_PATH
    db_size_mb = round(os.path.getsize(db_path) / 1024 / 1024, 2) if os.path.exists(db_path) else 0
    table_counts = {}
    for tbl, _ in [('clients', 'Клиентов'), ('contracts', 'Договоров'), ('scoring_log', 'Скорингов'), ('audit_log', 'Аудит записей'), ('portfolio_snapshots', 'Снимков')]:
        try:
            if _table_exists(db, tbl):
                table_counts[tbl] = db.execute("SELECT COUNT(*) as c FROM " + tbl).fetchone()['c']
            else:
                table_counts[tbl] = 0
        except Exception:
            table_counts[tbl] = '—'
    db.close()
    rendered_nav = render_template_string(NAV, user=u, active='admin', BRANCHES=BRANCHES)
    return render_template_string(
        ADMIN_T, user=u, active='admin', css=CSS, nav=rendered_nav,
        users=users, audit_rows=audit_rows, audit_query=audit_query,
        msg=msg, error=error, db_size_mb=db_size_mb, table_counts=table_counts,
        ROLES=ROLES, BRANCHES=BRANCHES
    )


# /api/refresh-analytics и /api/backup — зарегистрированы в routes/api_routes.py


@app.route('/api/product-detail/<product>')
@login_required
def api_product_detail(product):
    db = get_db()
    u = session['user']
    b = u.get('branch')
    flt = " AND branch=?" if b else ""
    p_base = [b] if b else []
    by_branch = db.execute(f"""
        SELECT branch, COUNT(*) as total,
            SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days>=90 THEN 1 ELSE 0 END) as npl,
            COALESCE(SUM(product_amount),0)/1000000.0 as portfolio_mln
        FROM contracts WHERE COALESCE(product_type,'Не указан')=? {flt}
        GROUP BY branch ORDER BY total DESC
    """, [product] + p_base).fetchall()
    by_status = db.execute(f"""
        SELECT COALESCE(status_detail,'—') as status_detail, COUNT(*) as total
        FROM contracts WHERE COALESCE(product_type,'Не указан')=? {flt}
        GROUP BY status_detail ORDER BY total DESC
    """, [product] + p_base).fetchall()
    db.close()
    return jsonify({
        'by_branch': [dict(r) for r in by_branch],
        'by_status': [dict(r) for r in by_status],
    })


@app.route('/api/integrity-check')
@login_required
def api_integrity_check():
    u = session['user']
    if u.get('role') not in ('director', 'executive', 'head_analyst') and 'admin' not in (u.get('modules') or []):
        return jsonify({'error': 'Нет прав'}), 403
    try:
        result = run_integrity_checks()
        summary = result.get('summary') or f"Проверка: {result.get('status','')}, замечаний: {result.get('issues_count',0)}"
        return jsonify({'summary': summary, **result})
    except Exception as e:
        db = get_db()
        orphan_contracts = db.execute(
            "SELECT COUNT(*) as c FROM contracts WHERE client_id NOT IN (SELECT id FROM clients)"
        ).fetchone()['c']
        null_status = db.execute(
            "SELECT COUNT(*) as c FROM contracts WHERE status_detail IS NULL OR status_detail=''"
        ).fetchone()['c']
        db.close()
        return jsonify({
            'summary': f"Сирот: {orphan_contracts}, статус пустой: {null_status}",
            'orphan_contracts': orphan_contracts,
            'null_status': null_status
        })


@app.route('/api/train-ml', methods=['POST'])
@login_required
def api_train_ml():
    u = session['user']
    if u.get('role') not in ('director', 'executive', 'head_analyst') and 'admin' not in (u.get('modules') or []):
        return jsonify({'error': 'Нет прав'}), 403
    try:
        result = train_ml_model()
        log_audit(u['username'], 'train_ml', 'system')
        return jsonify({'ok': True, 'message': str(result)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══ INTELLIGENCE HELPERS (in main.py per spec) ═══

def _table_exists(db, name):
    try:
        db.execute("SELECT 1 FROM " + name + " LIMIT 1")
        return True
    except Exception:
        return False


def safe_read_file(filepath, filename):
    """Читает Excel или CSV с автоподбором кодировки и разделителя."""
    import os
    ext = os.path.splitext(filename)[1].lower()

    try:
        import pandas as pd
    except ImportError:
        return None, "Установите pandas: pip install pandas openpyxl xlrd"

    if ext in ('.xls', '.xlsx', '.xlsm'):
        try:
            frames = []
            xl = pd.ExcelFile(filepath)
            for sheet in xl.sheet_names:
                try:
                    df = pd.read_excel(filepath, sheet_name=sheet, dtype=str)
                    df.columns = [str(c).strip() for c in df.columns]
                    df = df.dropna(how='all')
                    if len(df) > 0:
                        df['_sheet'] = sheet
                        frames.append(df)
                except Exception:
                    pass
            if frames:
                return pd.concat(frames, ignore_index=True), None
            return None, "Не удалось прочитать ни один лист Excel"
        except Exception as e:
            return None, "Ошибка Excel: %s" % str(e)

    if ext in ('.csv', '.txt'):
        for enc in ['utf-8-sig', 'utf-8', 'cp1251', 'cp1252', 'latin-1']:
            for sep in [',', ';', '\t', '|']:
                try:
                    df = pd.read_csv(
                        filepath, encoding=enc, sep=sep,
                        on_bad_lines='skip', engine='python',
                        quoting=3, dtype=str
                    )
                    df.columns = [str(c).strip() for c in df.columns]
                    df = df.dropna(how='all')
                    if len(df.columns) >= 2 and len(df) >= 1:
                        return df, None
                except TypeError:
                    try:
                        df = pd.read_csv(
                            filepath, encoding=enc, sep=sep,
                            on_bad_lines='skip', engine='python', dtype=str
                        )
                        df.columns = [str(c).strip() for c in df.columns]
                        df = df.dropna(how='all')
                        if len(df.columns) >= 2 and len(df) >= 1:
                            return df, None
                    except Exception:
                        pass
                except Exception:
                    pass
        return None, "Не удалось прочитать CSV. Попробуйте сохранить как .xlsx"

    try:
        df = pd.read_excel(filepath, dtype=str)
        df.columns = [str(c).strip() for c in df.columns]
        return df.dropna(how='all'), None
    except Exception as e:
        return None, "Неподдерживаемый формат: %s. Ошибка: %s" % (ext, str(e))


def detect_file_type(df):
    """Определяет тип данных по названиям колонок."""
    cols_lower = [str(c).lower() for c in df.columns]
    score = {'clients': 0, 'contracts': 0, 'payments': 0}
    client_kw = ['фио', 'имя', 'full_name', 'паспорт', 'passport', 'телефон', 'phone', 'возраст', 'age', 'клиент']
    contract_kw = ['договор', 'contract', 'сумма', 'amount', 'продукт', 'product', 'филиал', 'branch', 'статус', 'status', 'долг', 'debt', 'клиент', 'client']
    payment_kw = ['платёж', 'payment', 'оплата', 'дата_оплаты', 'сумма_оплаты']
    for kw in client_kw:
        if any(kw in c for c in cols_lower):
            score['clients'] += 1
    for kw in contract_kw:
        if any(kw in c for c in cols_lower):
            score['contracts'] += 1
    for kw in payment_kw:
        if any(kw in c for c in cols_lower):
            score['payments'] += 1
    return max(score, key=score.get)


def do_import(df, import_type, username):
    """Импортирует данные из DataFrame в БД. Схема: status_detail, debt_amount, responsible_person."""
    import pandas as pd
    db = get_db()
    imported = 0
    skipped = 0
    col_map = {c: str(c).lower().strip().replace(' ', '_') for c in df.columns}
    df = df.rename(columns=col_map)

    if import_type == 'clients':
        for _, row in df.iterrows():
            try:
                name = str(row.get('full_name') or row.get('фио') or row.get('имя') or row.get('ф.и.о.') or '').strip()
                if not name or name == 'nan':
                    skipped += 1
                    continue
                # Паспорт: одна колонка или серия + номер / паспорт_номер
                passport_val = str(row.get('passport') or row.get('паспорт') or '').strip()
                if not passport_val or passport_val == 'nan':
                    series = str(row.get('серия') or row.get('series') or '').strip().replace(' ', '')
                    num = str(row.get('номер') or row.get('number') or row.get('паспорт_номер') or row.get('passport_number') or '').strip().replace(' ', '')
                    if series or num:
                        passport_val = (series or '') + (num or '')
                passport = (passport_val or None) if (passport_val and passport_val != 'nan') else None
                # ПИНФЛ → external_id для поиска по ПИНФЛ
                pinfl_raw = str(row.get('пинфл') or row.get('pinfl') or row.get('external_id') or row.get('инн') or '').strip()
                pinfl_clean = re.sub(r'[^\d]', '', pinfl_raw) if pinfl_raw else ''
                external_id = int(pinfl_clean) if len(pinfl_clean) >= 9 else None
                phone = str(row.get('phone') or row.get('телефон') or '').strip()
                phone = (phone or None) if (phone and phone != 'nan') else None
                age = row.get('age') or row.get('возраст') or row.get('узол')
                try:
                    age = int(float(str(age))) if age and str(age) != 'nan' else None
                except Exception:
                    age = None
                db.execute(
                    "INSERT INTO clients (full_name, passport, phone, age, external_id) VALUES (?,?,?,?,?)",
                    (name, passport, phone, age, external_id)
                )
                imported += 1
            except Exception:
                skipped += 1

    elif import_type == 'contracts':
        for _, row in df.iterrows():
            try:
                client_name = str(row.get('client_name') or row.get('фио') or row.get('клиент') or row.get('full_name') or '').strip()
                if not client_name or client_name == 'nan':
                    skipped += 1
                    continue
                cl = db.execute("SELECT id FROM clients WHERE UPPER(TRIM(full_name))=UPPER(TRIM(?)) LIMIT 1", (client_name,)).fetchone()
                if not cl:
                    db.execute("INSERT INTO clients (full_name) VALUES (?)", (client_name,))
                    cl = db.execute("SELECT last_insert_rowid() AS id").fetchone()
                client_id = cl['id']
                branch = str(row.get('branch') or row.get('филиал') or 'Ф1').strip()
                product_type = str(row.get('product_type') or row.get('продукт') or '').strip()
                debt_val = row.get('debt_amount') or row.get('total_debt') or row.get('долг') or row.get('сумма') or 0
                try:
                    debt_amount = float(str(debt_val).replace(' ', '').replace(',', '.').replace('\xa0', '') or 0)
                except Exception:
                    debt_amount = 0
                status_detail = str(row.get('status_detail') or row.get('status') or row.get('статус') or 'Стандарт').strip()
                contract_date = str(row.get('contract_date') or row.get('дата') or row.get('contract_date') or '').strip()
                responsible_person = str(row.get('responsible_person') or row.get('employee_name') or row.get('сотрудник') or '').strip() or None
                db.execute("""
                    INSERT INTO contracts (client_id, branch, product_type, debt_amount, status_detail, contract_date, responsible_person)
                    VALUES (?,?,?,?,?,?,?)
                """, (client_id, branch, product_type, debt_amount, status_detail, contract_date if contract_date and contract_date != 'nan' else None, responsible_person))
                imported += 1
            except Exception:
                skipped += 1

    db.commit()
    db.close()
    return imported, skipped


def generate_insights(db, branch_filter=None):
    """AI-инсайты из реальных данных (всплеск НПЛ, ранний дефолт, худший/лучший филиал, сотрудник-лидер по НПЛ)."""
    flt = "AND branch=?" if branch_filter else ""
    p = ([branch_filter] if branch_filter else [])
    insights = []
    try:
        # 1. Всплеск НПЛ (90 дней vs предыдущие 90)
        row_cur = db.execute(f"""
            SELECT COUNT(*) as c FROM contracts
            WHERE status_detail IN ('Ёмон','МИБ','Судда') AND contract_date >= date('now','-90 days') {flt}
        """, p).fetchone()
        cur = (dict(row_cur).get('c', 0) if row_cur is not None else 0) or 0
        row_prev = db.execute(f"""
            SELECT COUNT(*) as c FROM contracts
            WHERE status_detail IN ('Ёмон','МИБ','Судда') AND contract_date BETWEEN date('now','-180 days') AND date('now','-90 days') {flt}
        """, p).fetchone()
        prev = (dict(row_prev).get('c', 0) if row_prev is not None else 0) or 0
        if prev > 0:
            delta = round((cur - prev) / prev * 100, 1)
            if delta > 10:
                insights.append({'type': 'critical', 'icon': '🔴',
                    'title': f'НПЛ вырос на {delta}% за квартал',
                    'text': f'Новых дефолтов: {cur} vs {prev} в предыдущем периоде. Рекомендуется проверка сотрудников с ростом просрочки.'})
            elif delta < -10:
                insights.append({'type': 'positive', 'icon': '🟢',
                    'title': f'НПЛ снизился на {abs(delta)}% — портфель улучшается',
                    'text': 'Положительная динамика. Эффективность работы с просрочкой растёт.'})

        # 2. Ранний дефолт
        row_early = db.execute(f"""
            SELECT COUNT(*) as c FROM contracts
            WHERE status_detail IN ('Ёмон','МИБ','Судда') AND julianday('now')-julianday(contract_date)<90 {flt}
        """, p).fetchone()
        early = (dict(row_early).get('c', 0) if row_early is not None else 0) or 0
        if early > 3:
            insights.append({'type': 'critical', 'icon': '🔴',
                'title': f'Ранний дефолт: {early} договоров до 90 дней',
                'text': 'Клиенты дефолтируют в первые 3 месяца — признак ошибки скоринга или мошенничества. Требуется ручная проверка.'})

        # 3. Худший и лучший филиал
        bstats = db.execute(f"""
            SELECT branch,
                   COUNT(*) as t,
                   ROUND(100.0*SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') THEN 1 ELSE 0 END)/COUNT(*),1) as npl
            FROM contracts WHERE 1=1 {flt} GROUP BY branch HAVING t > 100
        """, p).fetchall()
        if bstats:
            bstats = [dict(r) for r in bstats]
            worst = max(bstats, key=lambda x: x['npl'] or 0)
            best = min(bstats, key=lambda x: x['npl'] or 999)
            if (worst.get('npl') or 0) > 5:
                insights.append({'type': 'warning', 'icon': '🟡',
                    'title': f"Высокий НПЛ в {worst['branch']}: {worst['npl']}%",
                    'text': f"Рекомендуется ограничить лимиты выдачи в {worst['branch']} до стабилизации показателей."})
            insights.append({'type': 'positive', 'icon': '🟢',
                'title': f"Лучший результат: {best['branch']} — НПЛ {best['npl']}%",
                'text': f"Филиал {best['branch']} показывает наименьший НПЛ в сети. Рекомендуется тиражировать практику выдач."})

        # 4. Сотрудник-лидер по НПЛ
        emp = db.execute(f"""
            SELECT responsible_person AS employee_name,
                   COUNT(*) as t,
                   ROUND(100.0*SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') THEN 1 ELSE 0 END)/COUNT(*),1) as npl
            FROM contracts WHERE responsible_person IS NOT NULL AND TRIM(responsible_person) != '' {flt}
            GROUP BY responsible_person HAVING t > 20
            ORDER BY npl DESC LIMIT 1
        """, p).fetchone()
        if emp and (emp['npl'] or 0) > 8:
            insights.append({'type': 'warning', 'icon': '🟡',
                'title': f"Риск у сотрудника: {emp['employee_name']} — НПЛ {emp['npl']}%",
                'text': f"{emp['t']} договоров, НПЛ в {round((emp['npl'] or 0)/3, 1)}x выше среднего. Рекомендуется временное ограничение лимита."})
    except Exception as e:
        insights = [{'type': 'warning', 'icon': '⚠️', 'title': 'Ошибка генерации инсайтов', 'text': str(e)}]
    return insights


def generate_recommendations(db, branch_filter=None):
    """Рекомендации: высокий НПЛ по филиалу, лучший сегмент по доходу, молодёжь vs среднее."""
    flt = "AND branch=?" if branch_filter else ""
    p = ([branch_filter] if branch_filter else [])
    recs = []
    try:
        avg_row = db.execute(f"""
            SELECT ROUND(100.0*SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') THEN 1 ELSE 0 END)/COUNT(*),1) as r
            FROM contracts WHERE 1=1 {flt}
        """, p).fetchone()
        avg = (avg_row['r'] if avg_row else None) or 0

        for br in db.execute(f"""
            SELECT branch, ROUND(100.0*SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') THEN 1 ELSE 0 END)/COUNT(*),1) as npl
            FROM contracts WHERE 1=1 {flt} GROUP BY branch HAVING npl > ? AND COUNT(*) > 100
        """, p + [avg * 1.3]).fetchall():
            br = dict(br)
            recs.append({'priority': 'high', 'text': f"📉 Снизить лимит выдачи в {br['branch']} до 5 млн сум. НПЛ {br['npl']}% превышает среднее на {round((br['npl'] or 0) - avg, 1)}%."})

        best = db.execute(f"""
            SELECT cl.income_source,
                   ROUND(100.0*SUM(CASE WHEN c.status_detail IN ('Ёмон','МИБ','Судда') THEN 1 ELSE 0 END)/COUNT(*),1) as npl,
                   COUNT(*) as cnt
            FROM contracts c JOIN clients cl ON cl.id=c.client_id
            WHERE cl.income_source IS NOT NULL AND cl.income_source != '' {flt}
            GROUP BY cl.income_source HAVING cnt > 200
            ORDER BY npl ASC LIMIT 1
        """, p).fetchone()
        if best:
            best = dict(best)
            recs.append({'priority': 'low', 'text': f"📈 Увеличить лимиты для «{best['income_source']}»: НПЛ {best['npl']}% при {best['cnt']} договорах. Низкорисковый сегмент."})

        youth = db.execute(f"""
            SELECT ROUND(100.0*SUM(CASE WHEN c.status_detail IN ('Ёмон','МИБ','Судда') THEN 1 ELSE 0 END)/COUNT(*),1) as npl
            FROM contracts c JOIN clients cl ON cl.id=c.client_id WHERE cl.age BETWEEN 18 AND 22 {flt}
        """, p).fetchone()
        youth = (youth['npl'] if youth else None) or 0
        if youth and avg > 0 and youth > avg * 1.5:
            recs.append({'priority': 'medium', 'text': f"Ужесточить скоринг для клиентов 18-22 лет: НПЛ {youth}% vs среднее {avg}%. Требовать поручителя."})

        # Risky employees
        risky_emp = db.execute(f"""
            SELECT COALESCE(NULLIF(TRIM(responsible_person),''),'—') AS emp,
                   COUNT(*) AS cnt,
                   ROUND(100.0*SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days>=90 THEN 1 ELSE 0 END)/COUNT(*),1) AS npl
            FROM contracts WHERE 1=1 {flt}
            GROUP BY emp HAVING cnt>=20 AND npl>? ORDER BY npl DESC LIMIT 3
        """, p + [avg * 1.5]).fetchall()
        for re in risky_emp:
            re = dict(re)
            recs.append({'priority': 'high', 'text': f"Сотрудник {re['emp']}: NPL {re['npl']}% при {re['cnt']} договорах. Рассмотреть ограничение выдач."})

        # Low collection branches
        low_coll = db.execute(f"""
            SELECT branch,
                   CASE WHEN COALESCE(SUM(product_amount),0)>0
                        THEN ROUND(100.0*SUM(paid_amount)/SUM(product_amount),1) ELSE 0 END AS coll
            FROM contracts WHERE 1=1 {flt}
            GROUP BY branch HAVING COUNT(*)>50 AND coll<60 ORDER BY coll LIMIT 2
        """, p).fetchall()
        for lc in low_coll:
            lc = dict(lc)
            recs.append({'priority': 'medium', 'text': f"Низкий сбор в {lc['branch']}: {lc['coll']}%. Усилить контроль взыскания."})

        # Scale successful segments
        best_seg = db.execute(f"""
            SELECT cl.income_source AS seg,
                   COUNT(*) AS cnt,
                   ROUND(100.0*SUM(CASE WHEN c.status_detail IN ('Ёмон','МИБ','Судда') THEN 1 ELSE 0 END)/COUNT(*),1) AS npl
            FROM contracts c JOIN clients cl ON cl.id=c.client_id
            WHERE cl.income_source IS NOT NULL AND cl.income_source!='' {flt}
            GROUP BY seg HAVING cnt>100 AND npl<?
            ORDER BY cnt DESC LIMIT 1
        """, p + [avg * 0.5]).fetchall()
        for bs in best_seg:
            bs = dict(bs)
            recs.append({'priority': 'low', 'text': f"Масштабировать сегмент «{bs['seg']}»: {bs['cnt']} договоров, NPL всего {bs['npl']}%."})

        if not recs:
            recs.append({'priority': 'low', 'text': 'Портфель в норме. Критических отклонений не выявлено.'})
    except Exception:
        pass
    return recs


def make_sparkline(values, width=280, height=55, color='#3b82f6'):
    """Строит SVG-спарклайн из списка float. Подсвечивает аномалии (рост > 2% за шаг)."""
    vals = [float(v or 0) for v in (values or [])]
    if len(vals) < 2:
        return f'<svg width="{width}" height="{height}"></svg>'
    mn, mx = min(vals), max(vals)
    rng = mx - mn or 1
    pts = []
    for i, v in enumerate(vals):
        x = round(i / (len(vals) - 1) * width, 1)
        y = round(height - (v - mn) / rng * (height - 4) - 2, 1)
        pts.append(f'{x},{y}')
    path = ' '.join(pts)
    dots = ''
    for i in range(1, len(vals)):
        if vals[i] - vals[i - 1] > 2:
            x = round(i / (len(vals) - 1) * width, 1)
            y = round(height - (vals[i] - mn) / rng * (height - 4) - 2, 1)
            dots += f'<circle cx="{x}" cy="{y}" r="4" fill="#ef4444" opacity="0.8"/>'
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<polyline points="{path}" fill="none" stroke="{color}" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        f'{dots}</svg>'
    )


# ═══ INTELLIGENCE PAGE ═══

@app.route('/intelligence')
@login_required
@role_required('dashboard')
def intelligence_page():
    u = session['user']
    b = u.get('branch')
    branch_arg = request.args.get('branch', '').strip()
    year_arg = request.args.get('year', '').strip()
    month_arg = request.args.get('month', '').strip()
    if branch_arg == 'all':
        b = None
    elif branch_arg:
        b = branch_arg
    flt = "AND c.branch=?" if b else ""
    flt_c = "AND branch=?" if b else ""
    p = ([b] if b else [])
    db = get_db()

    # Риск-матрица по возрасту (НПЛ = статус Ёмон/МИБ/Судда ИЛИ total_late_days >= 90)
    year_flt = "AND strftime('%Y', c.contract_date)=?" if year_arg else ""
    month_flt = "AND strftime('%m', c.contract_date)=?" if month_arg else ""
    p_rm = p + ([year_arg] if year_arg else []) + ([month_arg] if month_arg else [])
    risk_matrix = []
    risk_matrix_error = None
    try:
        age_groups = [('18–25', 18, 25), ('26–35', 26, 35), ('36–45', 36, 45),
                      ('46–55', 46, 55), ('56–65', 56, 65), ('66–75', 66, 75), ('76+', 76, 120)]
        npl_sql = "SUM(CASE WHEN c.status_detail IN ('Ёмон','МИБ','Судда') OR c.total_late_days >= 90 THEN 1 ELSE 0 END)"
        for label, age_min, age_max in age_groups:
            row = db.execute(f"""
                SELECT COUNT(*) as total, {npl_sql} as npl
                FROM contracts c JOIN clients cl ON cl.id=c.client_id
                WHERE CAST(COALESCE(cl.age,0) AS INTEGER) BETWEEN ? AND ? {flt} {year_flt} {month_flt}
            """, [age_min, age_max] + p_rm).fetchone()
            total = row['total'] or 0
            npl = row['npl'] or 0
            risk_matrix.append({
                'segment': label,
                'total': total,
                'npl': npl,
                'pd': round(npl / total * 100, 1) if total > 0 else 0
            })
        # Сегмент «Не указан» для клиентов без возраста
        row_unknown = db.execute(f"""
            SELECT COUNT(*) as total, {npl_sql} as npl
            FROM contracts c JOIN clients cl ON cl.id=c.client_id
            WHERE (cl.age IS NULL OR CAST(COALESCE(cl.age,0) AS INTEGER) < 18 OR CAST(COALESCE(cl.age,0) AS INTEGER) > 120) {flt} {year_flt} {month_flt}
        """, p_rm).fetchone()
        total_u = row_unknown['total'] or 0
        npl_u = row_unknown['npl'] or 0
        risk_matrix.append({
            'segment': 'Не указан',
            'total': total_u,
            'npl': npl_u,
            'pd': round(npl_u / total_u * 100, 1) if total_u > 0 else 0
        })
    except Exception as e:
        risk_matrix = []
        risk_matrix_error = str(e) or 'Ошибка расчёта'
        import logging
        logging.getLogger(__name__).exception("Risk matrix by age failed")

    year_flt = "AND strftime('%Y', contract_date)=?" if year_arg else ""
    month_flt = "AND strftime('%m', contract_date)=?" if month_arg else ""
    p_cohort = p + ([year_arg] if year_arg else []) + ([month_arg] if month_arg else [])

    cohorts = db.execute(f"""
        SELECT strftime('%Y-%m', contract_date) as month,
               ROUND(100.0*SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90
                     THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),1) as npl_rate
        FROM contracts WHERE 1=1 {flt_c} {year_flt} {month_flt}
        GROUP BY month ORDER BY month DESC LIMIT 12
    """, p_cohort).fetchall()
    npl_values = [dict(c).get('npl_rate', 0) for c in reversed(list(cohorts))]
    sparkline = make_sparkline(npl_values, color='#ef4444') if npl_values else ''

    vintage = db.execute(f"""
        SELECT strftime('%Y-%m', contract_date) as cohort,
               COUNT(*) as total,
               ROUND(100.0*SUM(CASE WHEN total_late_days > 30 THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),1) as dpd30,
               ROUND(100.0*SUM(CASE WHEN total_late_days > 60 THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),1) as dpd60,
               ROUND(100.0*SUM(CASE WHEN total_late_days > 90 THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),1) as dpd90,
               ROUND(100.0*SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days >= 90
                     THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),1) as npl_pct
        FROM contracts WHERE 1=1 {flt_c} {year_flt} {month_flt}
        GROUP BY cohort ORDER BY cohort DESC LIMIT 24
    """, p_cohort).fetchall()

    at_risk = db.execute(f"""
        SELECT cl.full_name, c.branch, c.responsible_person AS employee_name,
               c.total_late_days AS delay_days, c.debt_amount AS total_debt, cl.id AS client_id
        FROM contracts c JOIN clients cl ON cl.id=c.client_id
        WHERE c.total_late_days BETWEEN 1 AND 24
        AND c.debt_amount > 0
        AND (c.status_detail IS NULL OR c.status_detail NOT IN ('Ёмон','МИБ','Судда')) {flt}
        ORDER BY c.total_late_days DESC, c.debt_amount DESC LIMIT 30
    """, p).fetchall()

    hitlist = db.execute(f"""
        SELECT cl.full_name, c.branch, c.responsible_person AS employee_name,
               c.total_late_days AS delay_days, c.debt_amount AS total_debt,
               c.status_detail AS status, cl.id AS client_id
        FROM contracts c JOIN clients cl ON cl.id=c.client_id
        WHERE c.total_late_days BETWEEN 15 AND 24
        AND c.debt_amount > 0
        AND (c.status_detail IS NULL OR c.status_detail NOT IN ('Ёмон','МИБ','Судда')) {flt}
        ORDER BY c.total_late_days DESC, c.debt_amount DESC LIMIT 20
    """, p).fetchall()

    insights = generate_insights(db, b)
    recommendations = generate_recommendations(db, b)
    branches_for_filter = db.execute("SELECT DISTINCT branch FROM contracts WHERE branch IS NOT NULL ORDER BY branch").fetchall()
    years_for_filter = db.execute("SELECT DISTINCT strftime('%Y', contract_date) AS y FROM contracts WHERE contract_date IS NOT NULL ORDER BY y DESC LIMIT 10").fetchall()

    # Channel analytics (top responsible persons as surrogate channels)
    channel_data = []
    try:
        ch_rows = db.execute(f"""
            SELECT COALESCE(NULLIF(TRIM(responsible_person),''), 'Прочее') AS channel,
                   COUNT(*) AS total,
                   ROUND(SUM(product_amount)/1e6,1) AS portfolio_mln,
                   ROUND(100.0*SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days>=90 THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),1) AS npl_rate,
                   ROUND(AVG(product_amount),0) AS avg_ticket
            FROM contracts WHERE 1=1 {flt_c}
            GROUP BY channel ORDER BY total DESC LIMIT 15
        """, p).fetchall()
        channel_data = [dict(r) for r in ch_rows]
    except Exception:
        pass

    # Repeat clients
    repeat_data = None
    try:
        dist_rows = db.execute(f"""
            SELECT
                CASE WHEN cnt=1 THEN '1 договор' WHEN cnt=2 THEN '2 договора' ELSE '3+ договоров' END AS bucket,
                COUNT(*) AS count,
                ROUND(100.0*COUNT(*)/NULLIF((SELECT COUNT(DISTINCT client_id) FROM contracts WHERE 1=1 {flt_c}),0),1) AS pct
            FROM (SELECT client_id, COUNT(*) AS cnt FROM contracts WHERE 1=1 {flt_c} GROUP BY client_id) sub
            GROUP BY bucket ORDER BY MIN(cnt)
        """, p + p).fetchall()
        branch_repeat = db.execute(f"""
            SELECT branch,
                   COUNT(DISTINCT client_id) AS total_clients,
                   SUM(CASE WHEN cc>1 THEN 1 ELSE 0 END) AS repeat_clients,
                   ROUND(100.0*SUM(CASE WHEN cc>1 THEN 1 ELSE 0 END)/NULLIF(COUNT(DISTINCT client_id),0),1) AS repeat_pct
            FROM (SELECT client_id, branch, COUNT(*) AS cc FROM contracts WHERE 1=1 {flt_c} GROUP BY client_id, branch) sub
            GROUP BY branch ORDER BY branch
        """, p).fetchall()
        repeat_data = {
            'distribution': [dict(r) for r in dist_rows],
            'by_branch': [dict(r) for r in branch_repeat],
        }
    except Exception:
        pass

    # Aging buckets
    aging_data = []
    try:
        aging_rows = db.execute(f"""
            SELECT
                CASE WHEN total_late_days<=0 THEN '0 дней'
                     WHEN total_late_days BETWEEN 1 AND 30 THEN '1-30'
                     WHEN total_late_days BETWEEN 31 AND 60 THEN '31-60'
                     WHEN total_late_days BETWEEN 61 AND 90 THEN '61-90'
                     WHEN total_late_days BETWEEN 91 AND 180 THEN '91-180'
                     WHEN total_late_days BETWEEN 181 AND 365 THEN '181-365'
                     ELSE '365+' END AS bucket,
                COUNT(*) AS cnt,
                COALESCE(SUM(debt_amount),0) AS debt_sum,
                ROUND(100.0*COUNT(*)/NULLIF((SELECT COUNT(*) FROM contracts WHERE 1=1 {flt_c}),0),1) AS pct
            FROM contracts WHERE 1=1 {flt_c}
            GROUP BY bucket ORDER BY MIN(COALESCE(total_late_days,0))
        """, p + p).fetchall()
        aging_data = [dict(r) for r in aging_rows]
    except Exception:
        pass

    # Employee top-10 by risk + top-10 best
    employee_risk = []
    employee_best = []
    try:
        emp_rows = db.execute(f"""
            SELECT COALESCE(NULLIF(TRIM(responsible_person),''),'Прочее') AS emp,
                   COUNT(*) AS total,
                   ROUND(SUM(product_amount)/1e6,1) AS portfolio_mln,
                   SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days>=90 THEN 1 ELSE 0 END) AS npl_cnt,
                   ROUND(100.0*SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') OR total_late_days>=90 THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),1) AS npl_rate,
                   CASE WHEN COALESCE(SUM(product_amount),0)>0 THEN ROUND(100.0*SUM(paid_amount)/SUM(product_amount),1) ELSE 0 END AS collection_rate
            FROM contracts WHERE 1=1 {flt_c}
            GROUP BY emp HAVING COUNT(*)>=5
            ORDER BY npl_rate DESC
        """, p).fetchall()
        all_emps = [dict(r) for r in emp_rows]
        employee_risk = all_emps[:10]
        employee_best = sorted(all_emps, key=lambda x: x.get('npl_rate', 100))[:10]
    except Exception:
        pass

    # Income source segments
    income_segments = []
    try:
        inc_rows = db.execute(f"""
            SELECT COALESCE(NULLIF(TRIM(cl.income_source),''),'Не указан') AS segment,
                   COUNT(*) AS total,
                   SUM(CASE WHEN c.status_detail IN ('Ёмон','МИБ','Судда') OR c.total_late_days>=90 THEN 1 ELSE 0 END) AS npl_cnt,
                   ROUND(100.0*SUM(CASE WHEN c.status_detail IN ('Ёмон','МИБ','Судда') OR c.total_late_days>=90 THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),1) AS pd
            FROM contracts c JOIN clients cl ON cl.id=c.client_id
            WHERE 1=1 {flt}
            GROUP BY segment ORDER BY total DESC
        """, p).fetchall()
        income_segments = [dict(r) for r in inc_rows]
    except Exception:
        pass

    # Sales channel data (online vs offline from sales table)
    sales_channel = []
    try:
        sc_rows = db.execute(f"""
            SELECT CASE WHEN is_online=1 THEN 'Онлайн' ELSE 'Офлайн' END AS channel,
                   COUNT(*) AS cnt, ROUND(SUM(total)/1e6,1) AS total_mln,
                   ROUND(AVG(total),0) AS avg_ticket
            FROM sales WHERE 1=1 {"AND branch=?" if b else ""}
            GROUP BY channel
        """, ([b] if b else [])).fetchall()
        sales_channel = [dict(r) for r in sc_rows]
    except Exception:
        pass

    # Sales by product group
    sales_products = []
    try:
        sp_rows = db.execute(f"""
            SELECT COALESCE(product_group,'Прочее') AS pg, COUNT(*) AS cnt,
                   ROUND(SUM(total)/1e6,1) AS total_mln, ROUND(AVG(total),0) AS avg_price
            FROM sales WHERE 1=1 {"AND branch=?" if b else ""}
            GROUP BY pg ORDER BY cnt DESC LIMIT 10
        """, ([b] if b else [])).fetchall()
        sales_products = [dict(r) for r in sp_rows]
    except Exception:
        pass

    db.close()

    return render_template_string(
        INTELLIGENCE_T,
        user=u, active='intelligence',
        insights=insights, recommendations=recommendations,
        risk_matrix=risk_matrix, risk_matrix_error=risk_matrix_error,
        hitlist=[dict(r) for r in hitlist],
        sparkline=sparkline, npl_values=npl_values,
        vintage=[dict(v) for v in vintage],
        at_risk=[dict(r) for r in at_risk],
        channel_data=channel_data, repeat_data=repeat_data,
        aging_data=aging_data,
        employee_risk=employee_risk, employee_best=employee_best,
        income_segments=income_segments,
        sales_channel=sales_channel, sales_products=sales_products,
        filter_branch=branch_arg or (b or 'all'),
        filter_year=year_arg, filter_month=month_arg,
        branches_for_filter=[r['branch'] for r in branches_for_filter],
        years_for_filter=[r['y'] for r in years_for_filter],
        BRANCHES=BRANCHES, NAV=NAV, CSS=CSS
    )


# ═══ FINANCE / OPEX PAGE ═══

@app.route('/finance')
@login_required
@role_required('dashboard')
def finance_page():
    u = session['user']
    if u.get('role') not in ('director', 'executive', 'head_analyst') and 'admin' not in (u.get('modules') or []):
        return redirect('/')
    from core.database import get_db, seed_opex_demo_data
    try:
        seed_opex_demo_data()
    except Exception:
        pass
    db = get_db()
    try:
        periods = [r[0] for r in db.execute("SELECT DISTINCT period FROM opex_entries ORDER BY period DESC").fetchall()]
    except Exception:
        periods = []
    db.close()
    cur_branch = request.args.get('branch', '').strip()
    cur_period = request.args.get('period', '').strip()
    return render_template_string(FINANCE_T, user=u, active='finance',
        BRANCHES=BRANCHES, NAV=NAV, CSS=CSS,
        periods=periods, cur_branch=cur_branch, cur_period=cur_period)


# ═══ RETAIL ANALYTICS (TZ12) ═══

@app.route('/retail-analytics')
@login_required
@role_required('dashboard')
def retail_analytics_page():
    u = session['user']
    db = get_db()
    branch_opts = [r[0] for r in db.execute("SELECT DISTINCT branch FROM contracts WHERE branch IS NOT NULL AND TRIM(branch)<>'' ORDER BY branch").fetchall()]
    year_opts = [r[0] for r in db.execute("SELECT DISTINCT strftime('%Y', contract_date) y FROM contracts WHERE contract_date IS NOT NULL ORDER BY y DESC").fetchall()]
    month_opts = [r[0] for r in db.execute("SELECT DISTINCT strftime('%m', contract_date) m FROM contracts WHERE contract_date IS NOT NULL ORDER BY m").fetchall()]
    category_opts = [r[0] for r in db.execute(
        "SELECT DISTINCT COALESCE(NULLIF(TRIM(product_type),''),'Не указан') c FROM contracts ORDER BY c"
    ).fetchall()]
    # supplier column may be absent in current schema
    has_supplier = False
    try:
        cols = db.execute("PRAGMA table_info(contracts)").fetchall()
        has_supplier = any((c["name"] == "supplier") for c in cols)
    except Exception:
        pass
    supplier_opts = []
    if has_supplier:
        supplier_opts = [r[0] for r in db.execute(
            "SELECT DISTINCT COALESCE(NULLIF(TRIM(supplier),''),'N/A') s FROM contracts ORDER BY s"
        ).fetchall()]
    # #region agent log
    _dbg745(
        "H-retail-supplier-filter",
        "main.py:retail_analytics_page",
        "supplier_filter_source",
        {
            "has_supplier_column": bool(has_supplier),
            "supplier_opts_count": len(supplier_opts),
            "sample_suppliers": supplier_opts[:10],
        },
    )
    # #endregion
    db.close()
    return render_template_string(
        H('Retail Analytics') + RETAIL_ANALYTICS_T + F,
        user=u,
        active='retail_analytics',
        NAV=NAV,
        CSS=CSS,
        branches=branch_opts,
        years=year_opts,
        months=month_opts,
        categories=category_opts,
        suppliers=supplier_opts,
    )


# ═══ DEVELOPER PORTAL PAGE ═══

RETAIL_ANALYTICS_T = """
<div class="ctr">
  <div class="page-header">
    <h1>🛍️ Product & Category Retail Analytics</h1>
    <p style="font-size:13px;color:var(--muted);margin-top:4px">TAB1–TAB4: продажи, маржа, стратегия роста, ассортимент, поставщики</p>
  </div>

  <div class="fl" style="gap:8px;flex-wrap:wrap;margin-bottom:14px">
    <select id="raBranch"><option value="">Все филиалы</option>{% for b in branches %}<option value="{{b}}">{{b}}</option>{% endfor %}</select>
    <select id="raYear"><option value="">Все годы</option>{% for y in years %}<option value="{{y}}">{{y}}</option>{% endfor %}</select>
    <select id="raMonth"><option value="">Все месяцы</option>{% for m in months %}<option value="{{m}}">{{m}}</option>{% endfor %}</select>
    <select id="raCategory"><option value="">Все категории</option>{% for c in categories %}<option value="{{c}}">{{c}}</option>{% endfor %}</select>
    <select id="raSupplier"><option value="">Все поставщики</option>{% for s in suppliers %}<option value="{{s}}">{{s}}</option>{% endfor %}</select>
    <button class="btn btn-p btn-sm" onclick="raReload()">Обновить</button>
  </div>

  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px">
    <button class="btn btn-o btn-sm" id="tab1Btn" onclick="raTab('tab1')">TAB 1 — Sales & Margin</button>
    <button class="btn btn-o btn-sm" id="tab2Btn" onclick="raTab('tab2')">TAB 2 — Growth Strategy</button>
    <button class="btn btn-o btn-sm" id="tab3Btn" onclick="raTab('tab3')">TAB 3 — Assortment</button>
    <button class="btn btn-o btn-sm" id="tab4Btn" onclick="raTab('tab4')">TAB 4 — Supplier</button>
  </div>

  <div id="tab1" class="sec">
    <div class="sec-head"><h2>Product Performance + Category Summary + Trends</h2><button class="btn btn-o btn-sm" onclick="exportTableCsv('tab1Table','tab1_performance.csv')">CSV</button></div>
    <div class="tbl-wrap"><table id="tab1Table"><thead><tr><th>Продукт</th><th>Категория</th><th>Филиал</th><th>Период</th><th>Дог.</th><th>Объём</th><th>Выручка</th><th>Маржа</th><th>Маржа %</th></tr></thead><tbody id="tab1Body"></tbody></table></div>
    <div class="sec-head" style="margin-top:10px"><h2>Category Summary</h2></div>
    <div class="tbl-wrap"><table id="tab1CatTable"><thead><tr><th>Категория</th><th>Объём</th><th>Выручка</th><th>Маржа</th><th>Маржа %</th><th>Доля выручки %</th></tr></thead><tbody id="tab1CatBody"></tbody></table></div>
  </div>

  <div id="tab2" class="sec" style="display:none">
    <div class="sec-head"><h2>Top Products & Scenarios</h2><button class="btn btn-o btn-sm" onclick="exportTableCsv('tab2Table','tab2_growth.csv')">CSV</button></div>
    <div class="tbl-wrap"><table id="tab2Table"><thead><tr><th>Продукт</th><th>Выручка</th><th>Маржа</th><th>Маржа %</th><th>Сценарий +10% объёма (прибыль)</th><th>Сценарий +5% маржи (прибыль)</th><th>Стратегия</th></tr></thead><tbody id="tab2Body"></tbody></table></div>
    <div class="cards" id="tab2Summary" style="margin-top:10px"></div>
  </div>

  <div id="tab3" class="sec" style="display:none">
    <div class="sec-head"><h2>Shelf / Assortment Optimization</h2><button class="btn btn-o btn-sm" onclick="exportTableCsv('tab3Table','tab3_assortment.csv')">CSV</button></div>
    <div class="tbl-wrap"><table id="tab3Table"><thead><tr><th>Продукт</th><th>ABC</th><th>XYZ</th><th>Tier</th><th>Роль</th><th>Потери 10д</th><th>Потери 30д</th></tr></thead><tbody id="tab3Body"></tbody></table></div>
  </div>

  <div id="tab4" class="sec" style="display:none">
    <div class="sec-head"><h2>Supplier Ranking & Actions</h2><button class="btn btn-o btn-sm" onclick="exportTableCsv('tab4Table','tab4_suppliers.csv')">CSV</button></div>
    <div class="tbl-wrap"><table id="tab4Table"><thead><tr><th>Поставщик</th><th>Дог.</th><th>Объём</th><th>Выручка</th><th>Маржа</th><th>Маржа %</th><th>Contribution %</th><th>Class</th><th>Action</th></tr></thead><tbody id="tab4Body"></tbody></table></div>
  </div>
</div>
<script>
function raParams() {
  const q = new URLSearchParams();
  ['Branch','Year','Month','Category','Supplier'].forEach(k => {
    const el = document.getElementById('ra' + k);
    if (el && el.value) q.set(k.toLowerCase(), el.value);
  });
  return q.toString();
}
function fmtN(v){ return Number(v||0).toLocaleString('ru-RU'); }
function fmtM(v){ return Number(v||0).toLocaleString('ru-RU',{maximumFractionDigits:2}); }
function raTab(id){
  ['tab1','tab2','tab3','tab4'].forEach(t=>document.getElementById(t).style.display=(t===id?'block':'none'));
}
function raReload(){
  const p = raParams();
  fetch('/api/analytics/products/performance?' + p).then(r=>r.json()).then(d=>{
    const b = document.getElementById('tab1Body');
    b.innerHTML = (d.performance||[]).map(x=>`<tr><td>${x.product||''}</td><td>${x.category||''}</td><td>${x.branch||''}</td><td>${x.year||''}-${x.month||''}</td><td>${fmtN(x.contract_count)}</td><td>${fmtM(x.sales_volume)}</td><td>${fmtM(x.revenue)}</td><td>${fmtM(x.margin)}</td><td>${fmtM(x.margin_pct)}%</td></tr>`).join('') || '<tr><td colspan="9" style="color:var(--muted);text-align:center">Нет данных</td></tr>';
    const cb = document.getElementById('tab1CatBody');
    cb.innerHTML = (d.category_summary||[]).map(x=>`<tr><td>${x.category||''}</td><td>${fmtM(x.sales_volume)}</td><td>${fmtM(x.revenue)}</td><td>${fmtM(x.margin)}</td><td>${fmtM(x.margin_pct)}%</td><td>${fmtM(x.share_of_revenue_pct)}%</td></tr>`).join('');
  });
  fetch('/api/analytics/products/growth-strategy?' + p).then(r=>r.json()).then(d=>{
    document.getElementById('tab2Body').innerHTML = (d.top_products||[]).map(x=>`<tr><td>${x.product||''}</td><td>${fmtM(x.revenue)}</td><td>${fmtM(x.margin)}</td><td>${fmtM(x.margin_pct)}%</td><td>${fmtM(x.scenario_volume_10_profit)}</td><td>${fmtM(x.scenario_margin_5_profit)}</td><td>${x.strategy||''}</td></tr>`).join('');
    const s = d.summary||{};
    document.getElementById('tab2Summary').innerHTML = `<div class="card"><h3>Текущая прибыль</h3><div class="val">${fmtM(s.current_profit)}</div></div><div class="card"><h3>Сценарий +10% объёма</h3><div class="val">${fmtM(s.profit_if_volume_10)}</div></div><div class="card"><h3>Сценарий +5% маржи</h3><div class="val">${fmtM(s.profit_if_margin_5)}</div></div>`;
  });
  fetch('/api/analytics/products/assortment?' + p).then(r=>r.json()).then(d=>{
    document.getElementById('tab3Body').innerHTML = (d.items||[]).map(x=>`<tr><td>${x.product||''}</td><td>${x.abc_class||''}</td><td>${x.xyz_class||''}</td><td>${x.tier||''}</td><td>${x.role||''}</td><td>${fmtM(x.loss_10d)}</td><td>${fmtM(x.loss_30d)}</td></tr>`).join('');
  });
  fetch('/api/analytics/suppliers/ranking?' + p).then(r=>r.json()).then(d=>{
    document.getElementById('tab4Body').innerHTML = (d.items||[]).map(x=>`<tr><td>${x.supplier||''}</td><td>${fmtN(x.contracts)}</td><td>${fmtM(x.sales_volume)}</td><td>${fmtM(x.revenue)}</td><td>${fmtM(x.margin)}</td><td>${fmtM(x.margin_pct)}%</td><td>${fmtM(x.contribution_pct)}%</td><td>${x.class||''}</td><td>${x.action||''}</td></tr>`).join('');
  });
}
function exportTableCsv(tableId, fileName){
  const t = document.getElementById(tableId); if(!t) return;
  const rows = [...t.querySelectorAll('tr')].map(tr => [...tr.querySelectorAll('th,td')].map(td => `"${(td.innerText||'').replace(/"/g,'""')}"`).join(','));
  const blob = new Blob([rows.join('\\n')], {type:'text/csv;charset=utf-8;'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = fileName; a.click();
}
raReload();
</script>
"""

DEVELOPER_T = """
<!DOCTYPE html><html><head><meta charset="utf-8"><title>Developer Portal</title>
{{ css|safe }}
<style>
.key-list td{padding:8px 12px}
.newkey{background:rgba(34,197,94,.08);padding:14px;border-radius:12px;margin:8px 0;word-break:break-all;border:1px solid rgba(34,197,94,.2)}
</style></head><body>
{{ nav|safe }}
<div class="ctr">
<div class="page-header"><h1>Developer Portal</h1><p style="font-size:13px;color:var(--muted);margin-top:4px">API-ключи, документация, тестирование</p></div>

<div style="display:flex;gap:6px;margin-bottom:20px;flex-wrap:wrap" id="devTabs">
  <button type="button" class="btn btn-sm btn-o" onclick="devTab('stats')" id="devTab-stats">Статистика</button>
  <button type="button" class="btn btn-sm btn-o" onclick="devTab('keys')" id="devTab-keys">API Ключи</button>
  <button type="button" class="btn btn-sm btn-o" onclick="devTab('docs')" id="devTab-docs">Документация</button>
  <button type="button" class="btn btn-sm btn-p" onclick="devTab('test')" id="devTab-test">Тест API</button>
  <button type="button" class="btn btn-sm btn-o" onclick="devTab('pricing')" id="devTab-pricing">Тарифы</button>
</div>

<div id="dev-stats" style="display:none">
  <div class="cards">
    <div class="card"><h3>Скорингов сегодня</h3><div class="val">{{ stats.get('today',0) }}</div></div>
    <div class="card"><h3>За 30 дней</h3><div class="val">{{ stats.get('month',0) }}</div></div>
    <div class="card"><h3>Средний балл</h3><div class="val">{{ stats.get('avg_score',0) }}</div></div>
    <div class="card"><h3>% одобрений</h3><div class="val" style="color:var(--green)">{{ stats.get('approved',0) }}%</div></div>
  </div>
  {% if stats.get('recent') %}
  <div class="sec" style="margin-top:16px">
    <div class="sec-head"><h2>Последние скоринги</h2></div>
    <div class="tbl-wrap"><table>
      <thead><tr><th>Клиент ID</th><th>Балл</th><th>Класс</th><th>Решение</th><th>Ставка</th><th>Дата</th></tr></thead>
      <tbody>
      {% for r in stats.recent %}
      <tr>
        <td>{{ r.client_id }}</td>
        <td><b>{{ r.score }}</b></td>
        <td><span class="badge {{ 'bg' if r.risk_class in ('A','B') else 'by' if r.risk_class=='C' else 'br' }}">{{ r.risk_class }}</span></td>
        <td style="font-size:12px">{{ r.decision }}</td>
        <td>{{ r.interest_rate }}%</td>
        <td style="font-size:11px;color:var(--muted)">{{ r.date }}</td>
      </tr>
      {% endfor %}
      </tbody>
    </table></div>
  </div>
  {% endif %}
</div>

<div id="dev-keys" style="display:none">
  <div class="card" style="margin-bottom:16px">
    <h3 style="margin-bottom:12px">Создать новый ключ</h3>
    <div class="g3">
      <div class="field"><label>Название</label><input id="keyName" placeholder="Например: Filial-3"></div>
      <div class="field"><label>Тип</label>
        <select id="keyType">
          <option value="test">Test — тестовые запросы</option>
          <option value="live">Live — боевые данные</option>
        </select></div>
      <div>
        <label style="visibility:hidden">-</label>
        <button class="btn btn-p" onclick="createKey()" style="display:block;margin-top:4px">+ Создать ключ</button>
      </div>
    </div>
    <div id="newKeyResult" style="margin-top:12px;display:none;padding:10px 14px;background:rgba(34,197,94,0.1);border:1px solid rgba(34,197,94,0.3);border-radius:8px">
      <div style="font-size:12px;color:var(--muted);margin-bottom:4px">Ваш новый ключ (сохраните, показывается один раз):</div>
      <div style="display:flex;gap:8px;align-items:center">
        <code id="newKeyValue" style="font-size:13px;font-family:monospace;flex:1;word-break:break-all"></code>
        <button class="btn btn-o btn-sm" onclick="copyKey()">Копировать</button>
      </div>
    </div>
  </div>
  {% if api_keys %}
  <div class="tbl-wrap"><table>
    <thead><tr><th>Название</th><th>Тип</th><th>Создан</th><th>Статус</th><th>Ключ</th><th></th></tr></thead>
    <tbody>
    {% for k in api_keys %}
    <tr>
      <td><b>{{ k.key_name }}</b></td>
      <td><span class="badge {{ 'bg' if k.key_type=='live' else 'by' }}">{{ k.key_type }}</span></td>
      <td style="font-size:11px;color:var(--muted)">{{ k.created_at[:10] if k.created_at else '' }}</td>
      <td><span class="badge {{ 'bg' if k.is_active else 'br' }}">{{ 'Активен' if k.is_active else 'Заблокирован' }}</span></td>
      <td><code style="font-size:11px">{{ k.key_preview }}</code>
        <button class="btn btn-o" style="padding:2px 8px;font-size:11px" data-copy="{{ (k.key_value or '')|e }}" onclick="copyText(this.getAttribute('data-copy'))">Копировать</button></td>
      <td><button class="btn btn-o btn-sm" style="color:var(--red)" onclick="deleteKey({{ k.id }})">Удалить</button></td>
    </tr>
    {% endfor %}
    </tbody>
  </table></div>
  {% else %}
  <div style="color:var(--muted);text-align:center;padding:24px">Ключей пока нет</div>
  {% endif %}
</div>

<div id="dev-docs" style="display:none">
  {% for lang, code in doc_examples %}
  <div class="card" style="margin-bottom:12px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <b>{{ lang }}</b>
      <button class="btn btn-o btn-sm" onclick="copyEl('code-{{ lang }}')">Копировать</button>
    </div>
    <pre id="code-{{ lang }}" style="background:rgba(0,0,0,0.3);padding:12px;border-radius:9px;font-size:12px;overflow-x:auto;color:#a5f3fc;line-height:1.5">{{ code }}</pre>
  </div>
  {% endfor %}
</div>

<div id="dev-test">
  <div class="g2" style="gap:16px;align-items:start">
    <div class="card">
      <h3 style="margin-bottom:14px">Параметры запроса</h3>
      <div class="g2">
        <div class="field"><label>Возраст</label><input type="number" id="t_age" value="35" oninput="updateCurl()"></div>
        <div class="field"><label>Пол</label><select id="t_gender" onchange="updateCurl()"><option>М</option><option>Ж</option></select></div>
      </div>
      <div class="field"><label>Источник дохода</label>
        <select id="t_income" onchange="updateCurl()">
          <option>Давлат ташкилоти</option>
          <option>Тижорат ташкилоти</option>
          <option>ЯТТ (&gt; 12 мес)</option>
          <option>ЯТТ (&lt; 12 мес)</option>
          <option>Пенсия</option>
        </select></div>
      <div class="g2">
        <div class="field"><label>Доход/мес</label><input type="number" id="t_monthly" value="4500000" oninput="updateCurl()"></div>
        <div class="field"><label>Сумма товара</label><input type="number" id="t_amount" value="8000000" oninput="updateCurl()"></div>
      </div>
      <div class="g2">
        <div class="field"><label>Первый взнос %</label><input type="number" id="t_advance" value="20" oninput="updateCurl()"></div>
        <div class="field"><label>Срок (мес)</label><input type="number" id="t_term" value="12" oninput="updateCurl()"></div>
      </div>
      <div class="g2">
        <div class="field"><label>Кафиль</label>
          <select id="t_guarantor" onchange="updateCurl()"><option value="true">Есть</option><option value="false">Нет</option></select></div>
        <div class="field"><label>МИБ/Суд долг</label>
          <select id="t_mib" onchange="updateCurl()"><option value="false">Нет</option><option value="true">Есть</option></select></div>
      </div>
      <div class="field"><label>Просрочка в др. орг. (мес)</label><input type="number" id="t_delay" value="0" oninput="updateCurl()"></div>
      <button class="btn btn-p" style="width:100%;margin-top:10px;padding:11px" onclick="runTest()">Запустить скоринг</button>
      <div style="margin-top:12px">
        <div style="font-size:11px;color:var(--muted);margin-bottom:4px">cURL эквивалент:</div>
        <pre id="curlPrev" style="background:rgba(0,0,0,0.25);padding:10px;border-radius:8px;font-size:10px;overflow-x:auto;color:#a5f3fc;line-height:1.4"></pre>
      </div>
    </div>
    <div>
      <div id="resLoading" style="display:none;text-align:center;padding:48px;color:var(--muted)">Выполняется запрос...</div>
      <div id="resError" class="err" style="display:none"></div>
      <div id="resCard" class="card" style="display:none">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
          <h3>Результат</h3>
          <span id="resTime" style="font-size:11px;color:var(--muted)"></span>
        </div>
        <div style="text-align:center;padding:16px 0">
          <div id="resCls" style="font-size:64px;font-weight:900;line-height:1"></div>
          <div id="resDec" style="font-size:17px;font-weight:700;margin-top:4px"></div>
          <div id="resRate" style="font-size:13px;color:var(--muted);margin-top:4px"></div>
        </div>
        <div style="margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;margin-bottom:4px">
            <span style="font-size:12px;color:var(--muted)">Итоговый балл</span>
            <b id="resScore"></b>
          </div>
          <div style="background:rgba(0,0,0,0.2);border-radius:99px;height:7px">
            <div id="resBar" style="height:7px;border-radius:99px;transition:width 0.5s"></div>
          </div>
        </div>
        <div id="resFactors"></div>
        <div id="resPrintBtn" style="margin-top:12px"></div>
        <details style="margin-top:10px">
          <summary style="font-size:11px;color:var(--muted);cursor:pointer">Сырой JSON</summary>
          <pre id="resJson" style="font-size:10px;background:rgba(0,0,0,0.25);padding:10px;border-radius:8px;overflow-x:auto;color:#a5f3fc;margin-top:6px"></pre>
        </details>
      </div>
    </div>
  </div>
</div>

<div id="dev-pricing" style="display:none">
  <div class="g3">
    <div class="card" style="text-align:center;border:2px solid var(--border);position:relative">
      <div style="font-size:17px;font-weight:800;margin-bottom:8px">Старт</div>
      <div style="font-size:32px;font-weight:900;margin-bottom:14px">$49<span style="font-size:13px;font-weight:400;color:var(--muted)">/мес</span></div>
      <ul style="list-style:none;font-size:12px;color:var(--muted);text-align:left;line-height:2.2">
        <li>До 5 пользователей</li><li>10 000 скорингов/мес</li><li>Базовая аналитика</li><li>API доступ</li><li>— ML модели</li><li>— Поведенческий скоринг</li>
      </ul>
      <button class="btn btn-o" style="width:100%;margin-top:16px">Выбрать</button>
    </div>
    <div class="card" style="text-align:center;border:2px solid var(--accent);position:relative">
      <div style="position:absolute;top:-13px;left:50%;transform:translateX(-50%);background:var(--accent);color:#fff;font-size:11px;font-weight:700;padding:3px 14px;border-radius:99px">ПОПУЛЯРНЫЙ</div>
      <div style="font-size:17px;font-weight:800;margin-bottom:8px">Рост</div>
      <div style="font-size:32px;font-weight:900;margin-bottom:14px">$199<span style="font-size:13px;font-weight:400;color:var(--muted)">/мес</span></div>
      <ul style="list-style:none;font-size:12px;color:var(--muted);text-align:left;line-height:2.2">
        <li>До 50 пользователей</li><li>100 000 скорингов/мес</li><li>Полная аналитика</li><li>API + Webhook</li><li>ML модели</li><li>Поведенческий скоринг</li>
      </ul>
      <button class="btn btn-p" style="width:100%;margin-top:16px">Подключить</button>
    </div>
    <div class="card" style="text-align:center;border:2px solid var(--border);position:relative">
      <div style="font-size:17px;font-weight:800;margin-bottom:8px">Enterprise</div>
      <div style="font-size:32px;font-weight:900;margin-bottom:14px">Договор</div>
      <ul style="list-style:none;font-size:12px;color:var(--muted);text-align:left;line-height:2.2">
        <li>Безлимит пользователей</li><li>Безлимит скорингов</li><li>White-label брендинг</li><li>On-premise установка</li><li>SLA 99.9%</li><li>Персональный менеджер</li>
      </ul>
      <button class="btn btn-o" style="width:100%;margin-top:16px">Связаться</button>
    </div>
  </div>
</div>

</div>
<script>
var ACTIVE_TAB = 'test';
function devTab(id) {
  ['stats','keys','docs','test','pricing'].forEach(function(t) {
    var panel = document.getElementById('dev-'+t);
    if(panel) panel.style.display = t===id ? '' : 'none';
    var btn = document.getElementById('devTab-'+t);
    if(btn) btn.className = 'btn btn-sm ' + (t===id ? 'btn-p' : 'btn-o');
  });
  ACTIVE_TAB = id;
}
devTab('test');

function buildPayload() {
  return {
    age: parseInt(document.getElementById('t_age').value, 10),
    gender: document.getElementById('t_gender').value,
    income_source: document.getElementById('t_income').value,
    monthly_income: parseInt(document.getElementById('t_monthly').value, 10),
    product_amount: parseInt(document.getElementById('t_amount').value, 10),
    advance_payment_pct: parseInt(document.getElementById('t_advance').value, 10),
    term_months: parseInt(document.getElementById('t_term').value, 10),
    has_guarantor: document.getElementById('t_guarantor').value === 'true',
    has_mib_debt: document.getElementById('t_mib').value === 'true',
    external_delay_months: parseInt(document.getElementById('t_delay').value, 10),
    client_type: 'new'
  };
}
function updateCurl() {
  var p = buildPayload();
  var el = document.getElementById('curlPrev');
  if(el) el.innerText = 'curl -X POST ' + window.location.origin + '/api/score \\\n  -H "Content-Type: application/json" \\\n  -H "X-API-Key: ccc_live_..." \\\n  -d \'' + JSON.stringify(p, null, 2) + "'";
}
var CLS_C = {A:'#22c55e',B:'#84cc16',C:'#eab308',D:'#f97316',E:'#ef4444'};
var DEC_C = {'ОДОБРИТЬ':'#22c55e','НА РАССМОТРЕНИЕ':'#eab308','ОТКЛОНИТЬ':'#ef4444'};
function runTest() {
  var payload = buildPayload();
  var resCard = document.getElementById('resCard');
  var resError = document.getElementById('resError');
  var resLoading = document.getElementById('resLoading');
  if (resCard) resCard.style.display = 'none';
  if (resError) resError.style.display = 'none';
  if (resLoading) resLoading.style.display = 'block';
  var t0 = Date.now();
  fetch('/api/score', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload), credentials: 'same-origin' })
    .then(function(r) {
      return r.text().then(function(text) {
        if (!r.ok) {
          var msg = 'HTTP ' + r.status + (text ? ': ' + String(text).slice(0, 400) : '');
          throw new Error(msg);
        }
        try { return JSON.parse(text); } catch (e) { throw new Error('Ответ не JSON: ' + String(text).slice(0, 300)); }
      });
    })
    .then(function(d) {
      if (resLoading) resLoading.style.display = 'none';
      if (d.error) {
        if (resError) { resError.style.display = ''; resError.innerText = d.error; }
        if (resCard) resCard.style.display = 'none';
        return;
      }
      if (resError) resError.style.display = 'none';
      if (resCard) resCard.style.display = '';
      var ms = Date.now() - t0;
      var resTime = document.getElementById('resTime');
      if (resTime) resTime.innerText = ms + ' мс';
      var resJson = document.getElementById('resJson');
      if (resJson) resJson.innerText = JSON.stringify(d, null, 2);
      var cls = d.risk_class || '?';
      var cc = CLS_C[cls] || '#94a3b8';
      var resCls = document.getElementById('resCls');
      if (resCls) { resCls.innerText = cls; resCls.style.color = cc; }
      var decs = {'ОДОБРИТЬ':'ОДОБРИТЬ','НА РАССМОТРЕНИЕ':'НА РАССМОТРЕНИЕ','ОТКЛОНИТЬ':'ОТКЛОНИТЬ'};
      var resDec = document.getElementById('resDec');
      if (resDec) { resDec.innerText = decs[d.decision] || d.decision; resDec.style.color = DEC_C[d.decision] || '#fff'; }
      var rateVal = d.interest_rate != null ? d.interest_rate : d.recommended_interest_rate;
      var resRate = document.getElementById('resRate');
      if (resRate) resRate.innerText = (rateVal != null && rateVal !== '') ? 'Ставка: ' + rateVal + '% в месяц' : '';
      var sc = d.score || 0;
      var resScore = document.getElementById('resScore');
      if (resScore) resScore.innerText = sc + ' / 100 баллов';
      var bar = document.getElementById('resBar');
      if (bar) { bar.style.width = Math.min(sc, 100) + '%'; bar.style.background = cc; }
      var fEl = document.getElementById('resFactors');
      if (d.breakdown && d.breakdown.length && fEl) {
        var fh = '<div style="margin-top:10px"><div style="font-size:11px;color:var(--muted);margin-bottom:6px">Разбивка:</div>';
        d.breakdown.forEach(function(f) {
          var fc = f.points > 0 ? '#22c55e' : f.points < 0 ? '#ef4444' : '#94a3b8';
          fh += '<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border);font-size:12px"><span>' + (f.criterion || f.name || '') + '</span><span style="font-weight:700;color:' + fc + '">' + (f.points >= 0 ? '+' : '') + f.points + '</span></div>';
        });
        fEl.innerHTML = fh + '</div>';
      } else if (fEl) fEl.innerHTML = '';
      var rateParam = d.interest_rate != null ? d.interest_rate : d.recommended_interest_rate;
      var printName = (d.client && d.client.full_name) || payload.full_name || 'Новый клиент';
      var clientId = (d.client && d.client.id) ? String(d.client.id) : '';
      var params = new URLSearchParams({ score: (d.score!=null ? d.score : 0), risk_class: cls, decision: d.decision||'', rate: rateParam!=null ? rateParam : '', age: payload.age, income_source: payload.income_source, amount: payload.product_amount, name: printName, branch: (d.client && d.client.branch) || payload.branch || '', client_id: clientId });
      var resPrintBtn = document.getElementById('resPrintBtn');
      if (resPrintBtn) resPrintBtn.innerHTML = '<a href="/scoring/print?' + params + '" target="_blank" style="display:block;text-align:center;padding:9px;background:rgba(59,130,246,0.15);color:var(--accent);border:1px solid var(--accent);border-radius:8px;font-size:13px;font-weight:600;text-decoration:none">Официальное заключение (PDF)</a>';
    })
    .catch(function(e) {
      if (resLoading) resLoading.style.display = 'none';
      if (resError) { resError.style.display = ''; resError.innerText = 'Ошибка: ' + (e && e.message ? e.message : String(e)); }
    });
}
function createKey() {
  var name = (document.getElementById('keyName') && document.getElementById('keyName').value) ? document.getElementById('keyName').value.trim() : '';
  if (!name) { alert('Введите название ключа'); return; }
  var type = (document.getElementById('keyType') && document.getElementById('keyType').value) || 'test';
  var body = { name: name, type: type };
  if (type === 'live') body.plan_id = 2; else body.plan_id = 1;
  fetch('/api/developer/keys', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.error) { alert(d.error); return; }
      var keyVal = d.key || d.api_key;
      document.getElementById('newKeyValue').innerText = keyVal;
      document.getElementById('newKeyResult').style.display = '';
    });
}
function copyKey() { copyText(document.getElementById('newKeyValue').innerText); }
function copyText(text) {
  navigator.clipboard.writeText(text).then(function() { showToast('Скопировано'); }).catch(function() {
    var ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta);
    showToast('Скопировано');
  });
}
function copyEl(id) { var el = document.getElementById(id); if (el) copyText(el.innerText); }
function deleteKey(id) {
  if (!confirm('Удалить ключ?')) return;
  fetch('/api/developer/keys', { method: 'DELETE', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ key_id: id }) }).then(function() { window.location.reload(); });
}
function showToast(msg) {
  var t = document.createElement('div');
  t.innerText = msg;
  t.style.cssText = 'position:fixed;bottom:24px;right:24px;background:#22c55e;color:#fff;padding:10px 20px;border-radius:10px;font-size:13px;font-weight:600;z-index:9999';
  document.body.appendChild(t);
  setTimeout(function(){ t.remove(); }, 2500);
}
updateCurl();
</script>
</body></html>
"""

@app.route('/developer')
@login_required
def developer_page():
    u = session['user']
    db = get_db()
    stats = {'today': 0, 'month': 0, 'avg_score': 0, 'approved': 0, 'recent': [], 'daily': []}
    if _table_exists(db, 'scoring_log'):
        try:
            stats['today'] = db.execute(
                "SELECT COUNT(*) as c FROM scoring_log WHERE date(scored_at)=date('now')"
            ).fetchone()['c']
            stats['month'] = db.execute(
                "SELECT COUNT(*) as c FROM scoring_log WHERE scored_at >= date('now','-30 days')"
            ).fetchone()['c']
            ar = db.execute(
                "SELECT ROUND(AVG(total_score),1) as r FROM scoring_log WHERE scored_at >= date('now','-30 days')"
            ).fetchone()
            stats['avg_score'] = ar['r'] or 0
            ap = db.execute(
                "SELECT ROUND(100.0*SUM(CASE WHEN decision='ОДОБРИТЬ' OR decision LIKE '%одобр%' THEN 1 ELSE 0 END)/COUNT(*),1) as r "
                "FROM scoring_log WHERE scored_at >= date('now','-30 days')"
            ).fetchone()
            stats['approved'] = ap['r'] or 0
            stats['recent'] = [dict(r) for r in db.execute(
                "SELECT client_id, total_score as score, risk_class, decision, interest_rate, scored_at as date "
                "FROM scoring_log ORDER BY scored_at DESC LIMIT 20"
            ).fetchall()]
            stats['daily'] = [dict(r) for r in db.execute(
                "SELECT date(scored_at) as d, COUNT(*) as cnt FROM scoring_log "
                "WHERE scored_at >= date('now','-30 days') GROUP BY d ORDER BY d"
            ).fetchall()]
        except Exception:
            pass
    api_keys = []
    try:
        from engines.saas_engine import list_api_keys
        tenant_id = u.get('tenant_id', 1)
        for r in list_api_keys(tenant_id):
            api_keys.append({
                'id': r.get('id'),
                'key_name': r.get('name', ''),
                'key_type': 'live' if (r.get('plan_id') or 1) >= 2 else 'test',
                'created_at': r.get('created_at', '')[:19] if r.get('created_at') else '',
                'is_active': bool(r.get('is_active', True)),
                'key_preview': (r.get('key_prefix') or '')[:24] + ('...' if len(r.get('key_prefix') or '') > 24 else ''),
                'key_value': r.get('key_prefix') or '',
            })
    except Exception:
        pass
    db.close()
    doc_examples = [
        ('Python', 'import requests\n\nAPI_KEY = "ccc_live_ВАШ_КЛЮЧ"\nBASE = "http://127.0.0.1:5000"\n\nr = requests.post(f"{BASE}/api/score",\n    json={\n        "age": 35,\n        "income_source": "Давлат ташкилоти",\n        "monthly_income": 4500000,\n        "product_amount": 8000000,\n        "advance_payment_pct": 20,\n        "term_months": 12,\n        "has_guarantor": True,\n        "has_mib_debt": False,\n        "external_delay_months": 0,\n        "client_type": "new"\n    },\n    headers={"X-API-Key": API_KEY}\n)\nprint(r.json())'),
        ('cURL', 'curl -X POST http://127.0.0.1:5000/api/score \\\n  -H "Content-Type: application/json" \\\n  -H "X-API-Key: ccc_live_ВАШ_КЛЮЧ" \\\n  -d \'{\n    "age": 35,\n    "income_source": "Давлат ташкилоти",\n    "monthly_income": 4500000,\n    "product_amount": 8000000,\n    "advance_payment_pct": 20,\n    "term_months": 12,\n    "has_guarantor": true,\n    "has_mib_debt": false,\n    "external_delay_months": 0,\n    "client_type": "new"\n  }\''),
        ('JavaScript', 'const r = await fetch("http://127.0.0.1:5000/api/score", {\n  method: "POST",\n  headers: {\n    "Content-Type": "application/json",\n    "X-API-Key": "ccc_live_ВАШ_КЛЮЧ"\n  },\n  body: JSON.stringify({\n    age: 35,\n    income_source: "Давлат ташкилоти",\n    monthly_income: 4500000,\n    product_amount: 8000000,\n    advance_payment_pct: 20,\n    term_months: 12,\n    has_guarantor: true,\n    has_mib_debt: false,\n    external_delay_months: 0,\n    client_type: "new"\n  })\n});\nconsole.log(await r.json());'),
    ]
    rendered_nav = render_template_string(NAV, user=u, active='developer', BRANCHES=BRANCHES)
    return render_template_string(
        DEVELOPER_T, user=u, active='developer', css=CSS, nav=rendered_nav,
        stats=stats, api_keys=api_keys, BRANCHES=BRANCHES, doc_examples=doc_examples
    )


@app.route('/scoring/print')
@app.route('/developer/score-pdf')
@login_required
def scoring_print():
    score = request.args.get('score', '0')
    risk_class = request.args.get('risk_class', '—')
    decision = request.args.get('decision', '—')
    rate = request.args.get('rate', '—')
    client_name = request.args.get('name', 'Новый клиент')
    client_age = request.args.get('age', '—')
    income_source = request.args.get('income_source', '—')
    product_amount = request.args.get('amount', '—')
    branch = request.args.get('branch') or (session.get('user') or {}).get('branch') or '—'
    client_gender = ''
    client_region = ''
    client_position = ''
    client_phone = ''
    client_passport = ''
    monthly_income = ''
    client_id_param = request.args.get('client_id', '')
    if client_id_param:
        try:
            cl = get_client(int(client_id_param))
            if cl:
                client_name = cl.get('full_name') or client_name
                client_age = cl.get('age') or client_age
                income_source = cl.get('income_source') or income_source
                cl_branch = cl.get('branch') or ''
                if not cl_branch and cl.get('contracts'):
                    cl_branch = cl['contracts'][0].get('branch', '')
                branch = cl_branch or branch or '—'
                client_gender = cl.get('gender') or ''
                client_region = cl.get('region') or ''
                client_position = cl.get('position') or ''
                client_phone = cl.get('phone') or ''
                raw_passport = cl.get('passport') or ''
                if raw_passport:
                    client_passport = str(raw_passport).replace('.0', '')
                else:
                    ext_id = cl.get('external_id')
                    if ext_id and int(ext_id) > 0:
                        client_passport = str(int(ext_id))
                mi = cl.get('monthly_income')
                if mi and float(mi) > 0:
                    try:
                        monthly_income = f"{int(float(mi)):,} сум"
                    except Exception:
                        pass
                if not monthly_income and cl.get('contracts'):
                    mp = cl['contracts'][0].get('monthly_payment', 0) or 0
                    if mp and float(mp) > 0:
                        monthly_income = f"{int(float(mp)):,} сум"
        except Exception:
            pass
    now = datetime.now().strftime('%d.%m.%Y %H:%M')
    doc_num = f"CCC-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    decision_colors = {'ОДОБРИТЬ': '#16a34a', 'НА РАССМОТРЕНИЕ': '#d97706', 'ОТКЛОНИТЬ': '#dc2626'}
    risk_colors = {'A': '#16a34a', 'B': '#65a30d', 'C': '#d97706', 'D': '#ea580c', 'E': '#dc2626'}
    dec_color = decision_colors.get(decision, '#374151')
    risk_color = risk_colors.get(risk_class, '#374151')

    try:
        amt_fmt = f"{int(float(product_amount)):,}" if product_amount and str(product_amount).replace('.', '').replace('-', '').isdigit() else product_amount
    except Exception:
        amt_fmt = product_amount
    score_pct = min(100, max(0, int(score))) if str(score).lstrip('-').isdigit() else 0
    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Скоринговое заключение {doc_num}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: 'Times New Roman', serif; background:#fff; color:#111; padding:40px; }}
@media print {{ body {{ padding:20px; }} .no-print {{ display:none; }} }}
.header {{ display:flex; justify-content:space-between; align-items:flex-start; border-bottom:2px solid #111; padding-bottom:16px; margin-bottom:20px; }}
.org {{ font-size:13px; color:#555; }} .org b {{ font-size:16px; color:#111; display:block; margin-bottom:4px; }}
.doc-info {{ text-align:right; font-size:12px; color:#555; }} .doc-info b {{ font-size:14px; color:#111; display:block; }}
h1 {{ font-size:20px; text-align:center; margin:20px 0 6px; }} .subtitle {{ text-align:center; font-size:13px; color:#666; margin-bottom:24px; }}
.section {{ margin-bottom:20px; }} .section h2 {{ font-size:13px; font-weight:bold; text-transform:uppercase; color:#444; border-bottom:1px solid #ddd; padding-bottom:4px; margin-bottom:10px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }} td {{ padding:7px 10px; border-bottom:1px solid #eee; }} td:first-child {{ color:#555; width:45%; }} td:last-child {{ font-weight:600; }}
.verdict-box {{ border:2px solid {dec_color}; border-radius:8px; padding:20px; text-align:center; margin:20px 0; }}
.verdict-label {{ font-size:12px; color:#666; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:6px; }}
.verdict-value {{ font-size:28px; font-weight:900; color:{dec_color}; }}
.risk-row {{ display:flex; gap:20px; justify-content:center; margin-top:12px; }} .risk-item {{ text-align:center; }}
.risk-item .val {{ font-size:36px; font-weight:900; color:{risk_color}; }} .risk-item .lbl {{ font-size:11px; color:#666; }}
.score-bar-wrap {{ margin:12px 0; }} .score-bar-bg {{ background:#e5e7eb; border-radius:99px; height:10px; }}
.score-bar-fill {{ background:{risk_color}; border-radius:99px; height:10px; width:{score_pct}%; }}
.footer {{ margin-top:30px; border-top:1px solid #ddd; padding-top:16px; display:flex; justify-content:space-between; font-size:11px; color:#888; }}
.sign-area {{ display:flex; gap:60px; margin-top:30px; }} .sign-block {{ flex:1; }}
.sign-block .label {{ font-size:11px; color:#666; margin-bottom:20px; }} .sign-block .line {{ border-bottom:1px solid #111; margin-bottom:4px; }}
.sign-block .name-label {{ font-size:10px; color:#999; }}
.stamp {{ width:80px; height:80px; border:2px solid #ddd; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:10px; color:#999; margin-top:8px; }}
.print-btn {{ position:fixed; top:20px; right:20px; background:#2563eb; color:#fff; border:none; padding:10px 22px; border-radius:8px; font-size:14px; cursor:pointer; font-weight:600; }}
</style></head><body>
<button class="print-btn no-print" onclick="window.print()">Печать / PDF</button>
<div class="header">
  <div class="org"><b>ООО «Акционерная компания»</b> Система управления кредитным портфелем<br>Credit Control Center (CCC)</div>
  <div class="doc-info"><b>Скоринговое заключение</b> № {doc_num}<br>Дата: {now}<br>Филиал: {branch}</div>
</div>
<h1>КРЕДИТНОЕ СКОРИНГОВОЕ ЗАКЛЮЧЕНИЕ</h1>
<p class="subtitle">Автоматизированная оценка кредитоспособности клиента</p>
<div class="verdict-box">
  <div class="verdict-label">Решение системы</div>
  <div class="verdict-value">{decision}</div>
  <div class="risk-row">
    <div class="risk-item"><div class="val">{risk_class}</div><div class="lbl">Класс риска</div></div>
    <div class="risk-item"><div class="val">{score}</div><div class="lbl">Балл (из 100)</div></div>
    <div class="risk-item"><div class="val">{rate}%</div><div class="lbl">Ставка/мес</div></div>
  </div>
  <div class="score-bar-wrap"><div class="score-bar-bg"><div class="score-bar-fill"></div></div></div>
</div>
<div class="section"><h2>Данные заявителя</h2><table>
<tr><td>ФИО / Наименование</td><td>{client_name}</td></tr>
<tr><td>Возраст</td><td>{client_age} лет</td></tr>
{'<tr><td>Пол</td><td>' + client_gender + '</td></tr>' if client_gender else ''}
{'<tr><td>Паспорт / ПИНФЛ</td><td>' + client_passport + '</td></tr>' if client_passport else ''}
{'<tr><td>Телефон</td><td>' + client_phone + '</td></tr>' if client_phone else ''}
<tr><td>Источник дохода</td><td>{income_source}</td></tr>
{'<tr><td>Должность</td><td>' + client_position + '</td></tr>' if client_position else ''}
{'<tr><td>Ежемесячный платёж</td><td>' + monthly_income + '</td></tr>' if monthly_income else ''}
{'<tr><td>Регион</td><td>' + client_region + '</td></tr>' if client_region else ''}
<tr><td>Сумма товара</td><td>{amt_fmt} сум</td></tr>
<tr><td>Филиал рассмотрения</td><td>{branch}</td></tr>
<tr><td>Дата оценки</td><td>{now}</td></tr>
</table></div>
<div class="section"><h2>Результат скоринговой оценки</h2><table>
<tr><td>Итоговый балл</td><td>{score} из 100</td></tr>
<tr><td>Класс риска</td><td style="color:{risk_color}">{risk_class}</td></tr>
<tr><td>Рекомендованная ставка</td><td>{rate}% в месяц</td></tr>
<tr><td>Решение системы</td><td style="color:{dec_color};font-size:15px">{decision}</td></tr>
<tr><td>Метод оценки</td><td>Балльная скоринговая модель CCC v3.5</td></tr>
</table></div>
<div class="section"><h2>Примечание</h2><table><tr><td colspan="2" style="font-size:12px;color:#555;line-height:1.6">
Данное заключение подготовлено автоматизированной системой кредитного скоринга Credit Control Center на основании предоставленных данных о заявителе. Решение носит рекомендательный характер и подлежит проверке уполномоченным сотрудником кредитного отдела. Срок действия заключения: 30 календарных дней.
</td></tr></table></div>
<div class="sign-area">
  <div class="sign-block"><div class="label">Кредитный сотрудник:</div><div class="line"></div><div class="name-label">подпись / расшифровка подписи</div></div>
  <div class="sign-block"><div class="label">Руководитель филиала:</div><div class="line"></div><div class="name-label">подпись / расшифровка подписи</div></div>
  <div class="stamp">М.П.</div>
</div>
<div class="footer">
  <span>CCC v3.5 · Автоматизированная система · {now}</span>
  <span>Документ сформирован системой</span>
</div></body></html>"""
    return Response(html, mimetype='text/html; charset=utf-8')


@app.route('/api/at-risk')
@login_required
def api_at_risk():
    """Contracts in early delinquency (total_late_days 1–30), not yet NPL. Only debt > 0."""
    u = session['user']
    b = u.get('branch')
    branch_arg = request.args.get('branch', '').strip()
    if branch_arg == 'all':
        b = None
    elif branch_arg:
        b = branch_arg
    flt = "AND c.branch=?" if b else ""
    p = ([b] if b else [])
    offset = int(request.args.get('offset', 0))
    db = get_db()
    rows = db.execute(f"""
        SELECT cl.full_name, c.branch, c.responsible_person as employee_name,
               c.total_late_days as delay_days, c.debt_amount as total_debt, c.product_type,
               c.status_detail as status, c.id as contract_id, cl.id as client_id
        FROM contracts c JOIN clients cl ON cl.id=c.client_id
        WHERE c.total_late_days > 0 AND c.total_late_days <= 30
        AND c.debt_amount > 0
        AND (c.status_detail IS NULL OR c.status_detail NOT IN ('Ёмон','МИБ','Судда')) {flt}
        ORDER BY c.total_late_days DESC, c.debt_amount DESC
        LIMIT 25 OFFSET ?
    """, p + [offset]).fetchall()
    total = db.execute(f"""
        SELECT COUNT(*) as cnt FROM contracts c
        WHERE c.total_late_days > 0 AND c.total_late_days <= 30
        AND c.debt_amount > 0
        AND (c.status_detail IS NULL OR c.status_detail NOT IN ('Ёмон','МИБ','Судда')) {flt}
    """, p).fetchone()['cnt']
    db.close()
    return jsonify({'rows': [dict(r) for r in rows], 'total': total})


@app.route('/api/vintage')
@login_required
def api_vintage():
    """Vintage cohort: month of issue vs DPD30/60/90/NPL %."""
    db = get_db()
    cohorts = db.execute("""
        SELECT strftime('%Y-%m', contract_date) as cohort,
               COUNT(*) as total,
               SUM(CASE WHEN total_late_days > 30 THEN 1 ELSE 0 END) as dpd30,
               SUM(CASE WHEN total_late_days > 60 THEN 1 ELSE 0 END) as dpd60,
               SUM(CASE WHEN total_late_days > 90 THEN 1 ELSE 0 END) as dpd90,
               SUM(CASE WHEN status_detail IN ('Ёмон','МИБ','Судда') THEN 1 ELSE 0 END) as npl
        FROM contracts
        GROUP BY cohort ORDER BY cohort DESC LIMIT 24
    """).fetchall()
    db.close()
    result = []
    for r in cohorts:
        rd = dict(r)
        t = rd['total'] or 1
        rd['dpd30_pct'] = round(rd['dpd30'] / t * 100, 1)
        rd['dpd60_pct'] = round(rd['dpd60'] / t * 100, 1)
        rd['dpd90_pct'] = round(rd['dpd90'] / t * 100, 1)
        rd['npl_pct'] = round(rd['npl'] / t * 100, 1)
        result.append(rd)
    return jsonify(result)


@app.route('/api/developer/stats')
@login_required
def api_developer_stats():
    """KPI and recent scoring for Developer Portal stats tab."""
    db = get_db()
    today = 0
    month = 0
    avg_score = 0
    approved = 0
    recent = []
    daily = []
    if _table_exists(db, 'scoring_log'):
        today = db.execute("""
            SELECT COUNT(*) as cnt FROM scoring_log
            WHERE date(scored_at) = date('now')
        """).fetchone()['cnt']
        month = db.execute("""
            SELECT COUNT(*) as cnt FROM scoring_log
            WHERE scored_at >= date('now','-30 days')
        """).fetchone()['cnt']
        avg_row = db.execute("""
            SELECT ROUND(AVG(total_score),1) as avg FROM scoring_log
            WHERE scored_at >= date('now','-30 days')
        """).fetchone()
        avg_score = avg_row['avg'] or 0
        appr_row = db.execute("""
            SELECT ROUND(100.0*SUM(CASE WHEN decision='ОДОБРИТЬ' OR decision LIKE '%одобр%' THEN 1 ELSE 0 END)/COUNT(*),1) as pct
            FROM scoring_log WHERE scored_at >= date('now','-30 days')
        """).fetchone()
        approved = appr_row['pct'] or 0
        recent = db.execute("""
            SELECT client_id, total_score as score, risk_class, decision, interest_rate, scored_at as date
            FROM scoring_log ORDER BY scored_at DESC LIMIT 20
        """).fetchall()
        daily = db.execute("""
            SELECT date(scored_at) as d, COUNT(*) as cnt
            FROM scoring_log WHERE scored_at >= date('now','-30 days')
            GROUP BY d ORDER BY d
        """).fetchall()
    db.close()
    return jsonify({
        'today': today, 'month': month,
        'avg_score': avg_score, 'approved': approved,
        'recent': [dict(r) for r in recent],
        'daily': [dict(r) for r in daily]
    })


@app.route('/import', methods=['GET', 'POST'])
@login_required
@role_required('import')
def import_data():
    import uuid as uuid_mod
    u = session['user']
    result = None
    preview = None
    columns = []
    detected_type = None
    filename = ''
    file_error = None

    if request.method == 'POST':
        action = request.form.get('action', 'analyze')

        if action == 'analyze' and 'file' in request.files:
            files = request.files.getlist('file')
            files = [f for f in files if f and f.filename]
            if not files:
                file_error = "Выберите один или несколько файлов (CSV/Excel)."
            else:
                save_dir = UPLOAD_DIR
                os.makedirs(save_dir, exist_ok=True)
                tmp_list = []
                names_list = []
                for file in files:
                    fn = secure_filename(file.filename)
                    ext = os.path.splitext(fn)[1].lower()
                    if ext not in ALLOWED_IMPORT_EXTENSIONS:
                        file_error = "Файл «%s»: поддерживаются только .csv, .xlsx, .xls, .xlsm, .txt. Макс. %s МБ." % (fn, MAX_UPLOAD_MB)
                        break
                    tmp_path = os.path.join(save_dir, str(uuid_mod.uuid4()) + '_' + fn)
                    try:
                        file.save(tmp_path)
                        tmp_list.append(tmp_path)
                        names_list.append(fn)
                    except Exception as e:
                        file_error = "Не удалось сохранить «%s»: %s" % (fn, str(e))
                        break
                else:
                    session['import_tmp_list'] = tmp_list
                    session['import_filenames'] = names_list
                    session['import_tmp'] = tmp_list[0]
                    session['import_filename'] = names_list[0]
                    filename = names_list[0] if len(names_list) == 1 else "%s (+%s)" % (names_list[0], len(names_list) - 1)
                    df, err = safe_read_file(tmp_list[0], names_list[0])
                    if err:
                        file_error = err
                    else:
                        detected_type = detect_file_type(df)
                        columns = list(df.columns)
                        preview = df.head(10).to_dict('records')
                        result = {
                            'rows': len(df),
                            'cols': len(df.columns),
                            'detected_type': detected_type,
                            'sample': preview,
                            'files_count': len(tmp_list)
                        }

        elif action == 'import':
            tmp_list = session.get('import_tmp_list')
            if not tmp_list:
                tmp_list = [session.get('import_tmp')] if session.get('import_tmp') else []
            names_list = session.get('import_filenames') or [session.get('import_filename', '')] * len(tmp_list)
            if len(names_list) < len(tmp_list):
                names_list = names_list + [''] * (len(tmp_list) - len(names_list))
            filename = names_list[0] if len(names_list) == 1 else "%s файлов" % len(tmp_list)
            import_type = request.form.get('import_type', 'auto')

            if not tmp_list or not any(os.path.exists(p) for p in tmp_list):
                file_error = "Временные файлы не найдены. Загрузите файл(ы) заново."
            else:
                total_imported = 0
                total_skipped = 0
                errors = []
                for tmp_path, fname in zip(tmp_list, names_list):
                    if not os.path.exists(tmp_path):
                        continue
                    df, err = safe_read_file(tmp_path, fname or 'file')
                    if err:
                        errors.append("%s: %s" % (fname or tmp_path, err))
                        continue
                    try:
                        if import_type == 'auto':
                            it = detect_file_type(df)
                        else:
                            it = import_type
                        imp, sk = do_import(df, it, u.get('username') or u.get('name', ''))
                        total_imported += imp
                        total_skipped += sk
                    except Exception as e:
                        errors.append("%s: %s" % (fname or tmp_path, str(e)))
                if errors:
                    file_error = "; ".join(errors[:3]) + (" ..." if len(errors) > 3 else "")
                result = {'success': True, 'imported': total_imported, 'skipped': total_skipped, 'type': import_type, 'files_count': len(tmp_list)}
                try:
                    log_audit(u.get('username') or u.get('name', ''), 'import', import_type,
                              details='%s: %s импортировано, %s пропущено' % (filename, total_imported, total_skipped))
                except Exception:
                    pass

    # Путь Доп.база для отображения (вкладка сохраняет туда загрузки и выгрузки)
    dop_base_path = os.path.abspath(DATA_RAW_DOP_BASE)
    # Список регионов для импорта МФЙ (кириллица, как в форме «Новый клиент»)
    MFY_REGIONS_FALLBACK = [
        "Андижон", "Фарғона", "Тошкент", "Наманган", "Самарқанд", "Бухоро",
        "Қашқадарё", "Сурхондарё", "Хоразм", "Навоий", "Жиззах", "Сирдарё",
        "Қорақалпоғистон", "Қўқон",
    ]
    from core.database import get_region_names
    try:
        db_regions = get_region_names()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("get_region_names failed (e.g. disk I/O), using fallback: %s", e)
        db_regions = []
    mfy_regions = list(dict.fromkeys(MFY_REGIONS_FALLBACK + [r for r in db_regions if r]))
    return render_template_string(IMPORT_T, user=u, active='dataio',
        result=result, preview=preview, columns=columns,
        detected_type=detected_type, filename=filename,
        file_error=file_error, dop_base_path=dop_base_path,
        mfy_regions=mfy_regions, BRANCHES=BRANCHES,
        NAV=NAV, CSS=CSS)


@app.route('/export/<data_type>')
@login_required
@role_required('export')
def export_data(data_type):
    import io
    u = session['user']
    b = u.get('branch')
    flt = "WHERE branch=?" if b else "WHERE 1=1"
    p = ([b] if b else [])
    try:
        db = get_db()
    except Exception as e:
        return jsonify({'error': 'БД: ' + str(e)}), 500
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        db.close()
        return jsonify({'error': 'Установите openpyxl: pip install openpyxl'}), 500

    wb = openpyxl.Workbook()
    ws = wb.active
    hdr_fill = PatternFill("solid", fgColor="1E293B")
    hdr_font = Font(color="FFFFFF", bold=True, size=11)
    thin = Border(
        left=Side(style='thin', color='CCCCCC'),
        right=Side(style='thin', color='CCCCCC'),
        bottom=Side(style='thin', color='CCCCCC')
    )

    def write_headers(ws, headers):
        for ci, h in enumerate(headers, 1):
            cell = ws.cell(1, ci, h)
            cell.fill = hdr_fill
            cell.font = hdr_font
            cell.alignment = Alignment(horizontal='center', vertical='center')
            ws.row_dimensions[1].height = 22

    if data_type == 'clients':
        ws.title = "Клиенты"
        rows = db.execute("""
            SELECT id, external_id, full_name, gender, age, income_source, monthly_income,
                   position, workplace, region, mfy, has_car, has_card, has_kafeel, has_mib_court,
                   admin_fine, mib_debt, phone, passport, created_at, updated_at
            FROM clients ORDER BY id
        """).fetchall()
        client_headers = ['ID', 'External ID', 'ФИО', 'Пол', 'Возраст', 'Источник дохода', 'Доход/мес', 'Должность', 'Место работы', 'Регион', 'МФЙ', 'Авто', 'Карта', 'Кафиль', 'МИБ/суд', 'Жарима', 'МИБ долг', 'Телефон', 'Паспорт', 'Создан', 'Обновлён']
        write_headers(ws, client_headers)
        client_keys = ['id', 'external_id', 'full_name', 'gender', 'age', 'income_source', 'monthly_income', 'position', 'workplace', 'region', 'mfy', 'has_car', 'has_card', 'has_kafeel', 'has_mib_court', 'admin_fine', 'mib_debt', 'phone', 'passport', 'created_at', 'updated_at']
        for ri, r in enumerate(rows, 2):
            rd = dict(r)
            for ci, k in enumerate(client_keys, 1):
                ws.cell(ri, ci, rd.get(k, '')).border = thin

    elif data_type == 'contracts':
        ws.title = "Договоры"
        rows = db.execute("""
            SELECT c.id, c.external_id, c.client_id, cl.full_name, c.branch, c.responsible_person AS employee_name,
                   c.product_type, c.contract_date, c.contract_end_date, c.contract_term, c.interest_rate,
                   c.status, c.status_detail, c.product_amount, c.advance_payment,
                   c.monthly_payment, c.paid_amount, c.debt_amount AS total_debt,
                   c.overdue_amount, c.late_count, c.total_late_days AS delay_days,
                   c.last_payment_date, c.imported_at
            FROM contracts c JOIN clients cl ON cl.id=c.client_id
            %s ORDER BY c.branch, c.status_detail
        """ % flt, p).fetchall()
        contract_headers = ['ID', 'External ID', 'ID клиента', 'ФИО', 'Филиал', 'Сотрудник', 'Продукт', 'Дата', 'Дата окончания', 'Срок', 'Ставка', 'Статус', 'Статус дет.', 'Сумма', 'ПВ', 'Платёж/мес', 'Оплачено', 'Долг', 'Просрочка сум', 'Кол-во просрочек', 'Дн.просрочки', 'Дата послед.платежа', 'Импорт']
        write_headers(ws, contract_headers)
        contract_keys = ['id', 'external_id', 'client_id', 'full_name', 'branch', 'employee_name', 'product_type', 'contract_date', 'contract_end_date', 'contract_term', 'interest_rate', 'status', 'status_detail', 'product_amount', 'advance_payment', 'monthly_payment', 'paid_amount', 'total_debt', 'overdue_amount', 'late_count', 'delay_days', 'last_payment_date', 'imported_at']
        npl_fill = PatternFill("solid", fgColor="FCA5A5")
        npl_statuses = ('Ёмон', 'МИБ', 'Судда')
        for ri, r in enumerate(rows, 2):
            rd = dict(r)
            vals = [rd.get(k, '') for k in contract_keys]
            fill = npl_fill if (rd.get('status_detail') or '') in npl_statuses else None
            for ci, v in enumerate(vals, 1):
                cell = ws.cell(ri, ci, v)
                cell.border = thin
                if fill:
                    cell.fill = fill

    elif data_type == 'audit':
        ws.title = "Аудит"
        if _table_exists(db, 'audit_log'):
            rows = db.execute("SELECT id, user, action, entity, entity_id, details, created_at FROM audit_log ORDER BY created_at DESC LIMIT 10000").fetchall()
        else:
            rows = []
        write_headers(ws, ['ID', 'Пользователь', 'Действие', 'Объект', 'ID объекта', 'Детали', 'Дата'])
        for ri, r in enumerate(rows, 2):
            rd = dict(r)
            for ci, k in enumerate(['id', 'user', 'action', 'entity', 'entity_id', 'details', 'created_at'], 1):
                ws.cell(ri, ci, rd.get(k, '')).border = thin

    elif data_type == 'portfolio':
        db.close()
        return redirect(url_for('export_portfolio'))

    else:
        db.close()
        return jsonify({'error': 'Неизвестный тип экспорта'}), 404

    for col in ws.columns:
        mx = max((len(str(cell.value or '')) for cell in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(mx + 4, 40)

    db.close()
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    # Копия в Доп.база/exports для хранения выгрузок
    try:
        from datetime import datetime as dt
        export_name = '%s_export_%s.xlsx' % (data_type, dt.now().strftime('%Y%m%d_%H%M'))
        export_path = os.path.join(EXPORT_DIR, export_name)
        with open(export_path, 'wb') as f:
            f.write(buf.getvalue())
        buf.seek(0)
    except Exception:
        pass
    try:
        log_audit(u.get('username') or u.get('name', ''), 'export', details=data_type)
    except Exception:
        pass
    return send_file(buf, as_attachment=True,
                     download_name='%s_export.xlsx' % data_type,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/data-io')
@login_required
@role_required('import')
def data_io():
    return redirect(url_for('import_data'))

# ═══════════════════════════════════════
#  TEMPLATES
# ═══════════════════════════════════════

CSS = """<style>
:root,[data-theme="dark"]{--bg:#020817;--bg2:#0b1220;--card:#0f172a;--card-glass:rgba(15,23,42,.92);--accent:#3b82f6;--green:#22c55e;--yellow:#eab308;--red:#ef4444;--text:#e2e8f0;--muted:#94a3b8;--border:rgba(148,163,184,.18);--gold:#f59e0b;--orange:#f97316;--purple:#a855f7;--cyan:#06b6d4;--card2:#16243e;--card3:#1c2f4e;--glow:rgba(59,130,246,.08);--shadow:rgba(0,0,0,.4)}
[data-theme="light"]{--bg:#f9fafb;--bg2:#f3f4f6;--card:#ffffff;--card-glass:rgba(255,255,255,.88);--accent:#2563eb;--green:#16a34a;--yellow:#ca8a04;--red:#dc2626;--text:#0f172a;--muted:#64748b;--border:rgba(0,0,0,.1);--gold:#d97706;--orange:#ea580c;--purple:#9333ea;--cyan:#0891b2;--card2:#f8fafc;--card3:#f1f5f9;--glow:rgba(59,130,246,.04);--shadow:rgba(0,0,0,.08)}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);transition:background .3s,color .3s;min-height:100vh}
body::before{content:'';position:fixed;inset:0;z-index:-1;background:radial-gradient(ellipse at 10% 0%,rgba(59,130,246,.08),transparent 60%),radial-gradient(ellipse at 90% 100%,rgba(168,85,247,.06),transparent 60%);pointer-events:none}
a{color:var(--accent);text-decoration:none}
.ctr{max-width:1400px;margin:0 auto;padding:0 20px}
nav{background:var(--card-glass);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border-bottom:1px solid var(--border);padding:0 20px;position:sticky;top:0;z-index:200;transition:background .3s}
nav .inner{max-width:1400px;margin:0 auto;display:flex;align-items:center;height:56px;gap:6px;flex-wrap:wrap}
nav .logo{display:flex;align-items:center;gap:10px;font-weight:800;font-size:14px;color:var(--text);margin-right:16px;letter-spacing:1.5px;text-transform:uppercase}
nav .logo-icon{width:32px;height:32px;border-radius:10px;background:linear-gradient(145deg,#1a365d,#0c4a6e,#164e63);box-shadow:0 2px 8px rgba(0,0,0,.3),inset 0 1px 0 rgba(255,255,255,.12);display:flex;align-items:center;justify-content:center;font-size:11px;color:#c9a96e;font-weight:900;font-family:'IBM Plex Mono',ui-monospace,monospace;letter-spacing:-.5px;transition:transform .2s,box-shadow .2s}
nav .logo-icon:hover{transform:scale(1.06);box-shadow:0 4px 14px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.15)}
nav .logo-text{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:13px;color:var(--text);letter-spacing:2px}
nav .logo-sub{font-size:8px;color:var(--muted);letter-spacing:1.5px;text-transform:uppercase;font-weight:500;margin-top:-2px}
nav a.lk{color:var(--muted);padding:5px 10px;border-radius:8px;font-size:12px;font-weight:500;transition:all .15s ease;position:relative;display:inline-flex;align-items:center;gap:5px}
nav a.lk svg{opacity:.6;transition:opacity .15s,transform .15s}
nav a.lk:hover{color:var(--text);background:var(--glow);transform:translateY(-1px)}
nav a.lk:hover svg{opacity:1;transform:scale(1.1)}
nav a.lk.on{color:var(--text);background:var(--glow);box-shadow:inset 0 -2px 0 var(--accent)}
nav a.lk.on svg{opacity:1}
nav .usr{margin-left:auto;font-size:11px;color:var(--muted);display:flex;align-items:center;gap:8px}
nav .usr b{color:var(--text)}
.theme-toggle{width:44px;height:24px;border-radius:12px;background:var(--border);border:none;cursor:pointer;position:relative;transition:background .2s}
.theme-toggle::after{content:'';position:absolute;top:2px;left:2px;width:20px;height:20px;border-radius:10px;background:var(--text);transition:transform .2s;box-shadow:0 1px 3px var(--shadow)}
[data-theme="light"] .theme-toggle::after{transform:translateX(20px)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:20px 0}
.card{background:var(--card-glass);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);border:1px solid var(--border);border-radius:16px;padding:20px;transition:transform .15s ease,box-shadow .15s ease}
.card:hover{transform:translateY(-3px);box-shadow:0 12px 32px var(--shadow),0 0 0 1px rgba(59,130,246,.08)}
.card h3{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.card .val{font-size:26px;font-weight:700;font-family:'IBM Plex Mono',ui-monospace,monospace}
.card .sub{font-size:11px;color:var(--muted);margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:var(--card);text-align:left;padding:8px 12px;font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);position:sticky;top:0;z-index:2;cursor:pointer;border-bottom:1px solid var(--border)}
th:hover{color:var(--text)}
td{padding:7px 12px;border-bottom:1px solid var(--border)}
td.num{text-align:right;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px}
tbody tr{transition:background .15s,transform .15s}
tbody tr:hover{background:var(--glow);cursor:pointer;transform:translateX(2px)}
tbody tr:active{transform:translateX(0)}
input,select{background:var(--bg2);color:var(--text);border:1px solid var(--border);border-radius:12px;padding:9px 14px;font-size:13px;width:100%;transition:all .2s ease;-webkit-appearance:none;appearance:none}
select{background-image:url("data:image/svg+xml,%3Csvg width='10' height='6' viewBox='0 0 10 6' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%2394a3b8' fill='none' stroke-width='1.5' stroke-linecap='round'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center;padding-right:32px;cursor:pointer}
select:hover{border-color:var(--accent);background-color:var(--glow)}
input:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(59,130,246,.12);transform:scale(1.01)}
.ios-select{display:inline-flex;background:var(--card-glass);backdrop-filter:blur(8px);border:1px solid var(--border);border-radius:12px;padding:3px;gap:2px;font-size:12px}
.ios-select .ios-opt{padding:5px 14px;border-radius:10px;cursor:pointer;transition:all .2s ease;color:var(--muted);font-weight:500;white-space:nowrap;user-select:none}
.ios-select .ios-opt:hover{color:var(--text);background:var(--glow)}
.ios-select .ios-opt.on{background:var(--accent);color:#fff;box-shadow:0 2px 6px rgba(59,130,246,.3)}
.filter-bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:10px 14px;background:var(--card-glass);backdrop-filter:blur(12px);border-radius:14px;border:1px solid var(--border);margin-bottom:16px}
.filter-bar label{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:500;color:var(--muted)}
.filter-bar select{width:auto;min-width:100px;font-size:12px;padding:6px 28px 6px 10px;border-radius:10px}
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 18px;border-radius:10px;border:none;font-size:13px;font-weight:600;cursor:pointer;transition:all .15s ease}
.btn:active{transform:scale(.97)}
.btn-p{background:var(--accent);color:#fff;box-shadow:0 2px 8px rgba(59,130,246,.25)}
.btn-p:hover{box-shadow:0 4px 16px rgba(59,130,246,.35);transform:translateY(-1px)}
.btn-s{background:var(--green);color:#fff}.btn-o{background:transparent;color:var(--accent);border:1px solid var(--border)}
.btn-o:hover{background:var(--glow);border-color:var(--accent)}
.btn-d{background:var(--red);color:#fff}.btn-sm{padding:4px 12px;font-size:12px;border-radius:8px}
.sec{margin:24px 0}.sec h2{font-size:18px;margin-bottom:14px;font-weight:700}
.sec-head{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.badge{display:inline-block;padding:2px 9px;border-radius:99px;font-size:11px;font-weight:600;transition:transform .15s,box-shadow .15s}
.badge:hover{transform:scale(1.06);box-shadow:0 2px 8px var(--shadow)}
.bg{background:rgba(34,197,94,.12);color:var(--green)}.by{background:rgba(234,179,8,.12);color:var(--yellow)}
.br{background:rgba(239,68,68,.12);color:var(--red)}.bb{background:rgba(59,130,246,.12);color:var(--accent)}
.bo{background:rgba(249,115,22,.12);color:var(--orange)}.bp{background:rgba(168,85,247,.12);color:var(--purple)}
.badge-critical{background:rgba(239,68,68,.12);color:var(--red)}.badge-high{background:rgba(249,115,22,.12);color:var(--orange)}
.badge-moderate{background:rgba(234,179,8,.12);color:var(--yellow)}.badge-low{background:rgba(34,197,94,.12);color:var(--green)}
.al{border-radius:14px;padding:14px 16px;margin:8px 0;border-left:4px solid;background:var(--card-glass);backdrop-filter:blur(8px)}
.al-c{border-color:var(--red)}.al-w{border-color:var(--yellow)}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:16px}.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
@media(max-width:900px){.g3,.g4{grid-template-columns:1fr 1fr}}
@media(max-width:768px){.g2,.g3,.g4{grid-template-columns:1fr}}
.sb{display:inline-flex;align-items:center;justify-content:center;width:48px;height:48px;border-radius:14px;font-size:22px;font-weight:800;color:#fff}
.factor{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border);font-size:13px}
.fl{display:flex;gap:12px;align-items:center}.fw{flex-wrap:wrap}
.field{margin-bottom:10px}.field label{display:block;font-size:12px;color:var(--muted);margin-bottom:3px;font-weight:500}
.mt{margin-top:14px}
.msg{padding:12px 16px;border-radius:12px;background:rgba(59,130,246,.08);border:1px solid rgba(59,130,246,.15);margin:10px 0;font-size:14px}
.err{padding:12px 16px;border-radius:12px;background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.15);color:var(--red);margin:10px 0;font-size:14px}
.tbl-wrap{max-height:420px;overflow-y:auto;overflow-x:auto;border-radius:12px;border:1px solid var(--border);background:var(--card-glass);backdrop-filter:blur(8px)}
.tbl-wrap::-webkit-scrollbar{width:4px;height:4px}.tbl-wrap::-webkit-scrollbar-thumb{background:var(--muted);border-radius:4px;opacity:.5}
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px}
.kpi-card{position:relative;padding:14px 16px;border-radius:14px;background:var(--card-glass);backdrop-filter:blur(12px);border:1px solid var(--border);transition:transform .15s,box-shadow .15s}
.kpi-card:hover{transform:translateY(-1px);box-shadow:0 6px 20px var(--shadow)}
.kpi-card::before{content:'';position:absolute;top:0;left:12px;right:12px;height:2px;border-radius:2px}
.kpi-accent-gold::before{background:var(--gold)}.kpi-accent-red::before{background:var(--red)}
.kpi-accent-green::before{background:var(--green)}.kpi-accent-blue::before{background:var(--accent)}
.kpi-accent-orange::before{background:var(--orange)}.kpi-accent-purple::before{background:var(--purple)}
.kpi-accent-cyan::before{background:var(--cyan)}
.kpi-card .kv{font-size:22px;font-weight:700;font-family:'IBM Plex Mono',ui-monospace,monospace}
.kpi-card .kl{font-size:11px;color:var(--muted);margin-top:2px}
.kpi-card .ks{font-size:10px;margin-top:3px}.ks.up{color:var(--green)}.ks.down{color:var(--red)}.ks.flat{color:var(--muted)}
.bar{height:6px;background:var(--border);border-radius:3px;overflow:hidden;margin-top:4px}
.barf{height:100%;border-radius:3px;transition:width .4s ease}
.barf-green{background:var(--green)}.barf-yellow{background:var(--yellow)}.barf-red{background:var(--red)}.barf-blue{background:var(--accent)}.barf-gold{background:var(--gold)}
.chart-container{position:relative;height:280px;width:100%;transition:transform .2s ease,box-shadow .2s ease;border-radius:12px}
.chart-container:hover{transform:scale(1.01);box-shadow:0 4px 20px var(--shadow)}
.chart-sm{height:180px}.chart-xs{height:100px}
.sticky-filters{position:sticky;top:56px;z-index:50;background:var(--card-glass);backdrop-filter:blur(16px);border-bottom:1px solid var(--border);padding:10px 0;margin-bottom:16px}
.stit{font-size:16px;font-weight:700;margin:28px 0 12px;padding-bottom:8px;border-bottom:2px solid var(--border);display:flex;align-items:center;gap:8px;transition:border-color .2s}
.stit:hover{border-color:var(--accent)}
.stit em{color:var(--gold);font-style:normal;transition:color .2s}
.stit:hover em{color:var(--accent)}
.page-header{margin:24px 0;padding:20px 24px;background:var(--card-glass);backdrop-filter:blur(16px);border-radius:18px;border:1px solid var(--border);transition:border-color .3s,box-shadow .3s}
.page-header:hover{border-color:rgba(59,130,246,.2);box-shadow:0 4px 24px rgba(59,130,246,.06)}
.page-header h1{font-size:24px;font-weight:800;letter-spacing:-.5px;background:linear-gradient(135deg,var(--text),var(--accent));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
@keyframes fadeInUp{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.sec,.card,.kpi-card{animation:fadeInUp .3s ease both}
.fade-in-up{animation:fadeInUp .35s ease both}
.kpi--default{border-top:2px solid var(--accent)}.kpi--success{border-top:2px solid var(--green)}.kpi--warning{border-top:2px solid var(--yellow)}.kpi--danger{border-top:2px solid var(--red)}
.badge--ok{background:rgba(34,197,94,.12);color:var(--green)}.badge--warn{background:rgba(234,179,8,.12);color:var(--yellow)}.badge--danger{background:rgba(239,68,68,.12);color:var(--red)}.badge--info{background:rgba(59,130,246,.12);color:var(--accent)}
.dev-section{background:var(--card-glass);backdrop-filter:blur(12px);border:1px solid var(--border);border-radius:16px;padding:20px;margin:16px 0}
.code{background:var(--bg2);padding:14px;border-radius:10px;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;overflow-x:auto;border:1px solid var(--border)}
pre{background:var(--bg2);padding:14px;border-radius:10px;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;overflow-x:auto;border:1px solid var(--border);color:var(--text)}
.ios-divider{height:1px;background:var(--border);margin:16px 0}
.glass-panel{background:var(--card-glass);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);border:1px solid var(--border);border-radius:16px;padding:20px}
@keyframes highlightPulse{0%{box-shadow:0 0 0 0 rgba(59,130,246,.3)}50%{box-shadow:0 0 0 8px rgba(59,130,246,0)}100%{box-shadow:0 0 0 0 transparent}}
.highlight-target{animation:highlightPulse .8s ease}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.pulse{animation:pulse 1.5s ease-in-out infinite}
.widget-card{background:var(--card-glass);backdrop-filter:blur(14px);border:1px solid var(--border);border-radius:16px;padding:18px;text-decoration:none;color:inherit;display:block;transition:all .15s ease}
.widget-card:hover{transform:translateY(-2px);box-shadow:0 8px 24px var(--shadow);border-color:var(--accent)}
.widget-card h3{font-size:13px;font-weight:700;margin-bottom:4px}.widget-card p{font-size:11px;color:var(--muted);margin-bottom:8px;line-height:1.4}
</style>"""

NAV = """<nav><div class="inner">
<a href="/" class="logo" style="text-decoration:none">
  <span class="logo-icon">CCC</span>
  <span style="display:flex;flex-direction:column;line-height:1.15">
    <span class="logo-text">Credit Control</span>
    <span class="logo-sub">Center</span>
  </span>
</a>
{% if user %}
{% if 'dashboard' in user.modules %}<a class="lk {{'on' if active=='dashboard'}}" href="/"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg> Дашборд</a>{% endif %}
{% if 'scoring' in user.modules %}<a class="lk {{'on' if active=='scoring'}}" href="/scoring"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg> Скоринг</a>{% endif %}
{% if 'portfolio' in user.modules %}<a class="lk {{'on' if active=='portfolio'}}" href="/portfolio"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18"/><path d="M7 16l4-8 4 4 5-6"/></svg> Портфель</a>{% endif %}
{% if 'alerts' in user.modules %}<a class="lk {{'on' if active=='alerts'}}" href="/alerts"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 01-3.46 0"/></svg> Алерты <span id="navAlertCount"></span></a>{% endif %}
{% if 'dashboard' in user.modules or 'intelligence' in user.modules %}<a class="lk {{'on' if active=='intelligence'}}" href="/intelligence"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg> Аналитика</a>{% endif %}
{% if 'admin' in user.modules %}<a class="lk {{'on' if active=='dataio'}}" href="/import"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> Импорт</a>{% endif %}
{% if user.role in ('director','executive','head_analyst') %}<a class="lk {{'on' if active=='finance'}}" href="/finance"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6"/></svg> OPEX</a>{% endif %}
{% if user.role in ('director','executive','head_analyst') %}<a class="lk {{'on' if active=='retail_analytics'}}" href="/retail-analytics"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M8 9h8"/><path d="M8 13h5"/></svg> Retail</a>{% endif %}
{% if 'admin' in user.modules or 'dashboard' in user.modules or 'developer' in user.modules %}<a class="lk {{'on' if active=='developer'}}" href="/developer"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg> Dev</a>{% endif %}
{% if 'admin' in user.modules %}<a class="lk {{'on' if active=='admin'}}" href="/admin"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg> Админ</a>{% endif %}
<span class="usr"><b>{{user.name}}</b> &middot; {{user.role_label}} &middot; <a href="/logout">Выход</a></span>
<button type="button" class="theme-toggle" id="themeToggle" aria-label="Theme"></button>
{% endif %}</div></nav>
<script>
(function(){var d=document.documentElement;var t=localStorage.getItem('theme')||'dark';d.setAttribute('data-theme',t);var btn=document.getElementById('themeToggle');if(btn){btn.title=t==='light'?'Dark mode':'Light mode';btn.onclick=function(){var n=t==='light'?'dark':'light';localStorage.setItem('theme',n);d.setAttribute('data-theme',n);btn.title=n==='light'?'Dark mode':'Light mode';t=n;};}})();
fetch('/api/alerts-count').then(function(r){return r.json();}).then(function(d){
  var el=document.getElementById('navAlertCount');
  if(el&&d.critical>0){el.innerText=d.critical;el.style.cssText='background:#ef4444;color:#fff;border-radius:99px;padding:1px 7px;font-size:10px;font-weight:700;margin-left:4px';}
});
document.querySelectorAll('form').forEach(function(f){
  f.addEventListener('submit',function(){
    var btn=f.querySelector('button[type="submit"]');
    if(btn&&!btn.disabled){btn.disabled=true;btn.dataset.orig=btn.textContent;btn.textContent='Загрузка…';}
  });
});
function sortTbl(tblId,col){var t=document.getElementById(tblId);if(!t||!t.tBodies[0])return;var b=t.tBodies[0];var rows=Array.from(b.rows).filter(function(r){return !r.classList.contains('drill-row')});var asc=t.dataset['s'+col]!=='asc';t.dataset['s'+col]=asc?'asc':'desc';rows.sort(function(a,b){var av=(a.cells[col]?a.cells[col].innerText:'').replace(/[,% ]/g,'');var bv=(b.cells[col]?b.cells[col].innerText:'').replace(/[,% ]/g,'');var an=parseFloat(av),bn=parseFloat(bv);if(!isNaN(an)&&!isNaN(bn))return asc?an-bn:bn-an;return asc?av.localeCompare(bv,'ru'):bv.localeCompare(av,'ru')});rows.forEach(function(r){b.appendChild(r)});}
document.querySelectorAll('a[href^="#"]').forEach(function(a){a.addEventListener('click',function(e){var t=document.querySelector(a.getAttribute('href'));if(t){e.preventDefault();t.scrollIntoView({behavior:'smooth'});t.classList.add('highlight-target');setTimeout(function(){t.classList.remove('highlight-target')},900);}});});
</script>"""

H = lambda t: f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{t}</title><link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet"><script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>{CSS}</head><body>{NAV}'
F = '</div></body></html>'

# ═══ DATA IMPORT / EXPORT PAGE ═══

IMPORT_T = H('Импорт / Экспорт') + """<div class="ctr">
<div class="page-header"><h1>📦 Импорт / Экспорт данных</h1></div>

<div class="sec">
  <div class="sec-head"><h2>📥 Импорт данных (автораспознавание)</h2></div>
  <div class="card">
    <p style="font-size:13px;color:var(--muted);margin-bottom:14px">
      Загрузите CSV или Excel (.xlsx/.xls). Система определит тип данных,
      покажет превью и импортирует в базу.
    </p>
    <p style="font-size:12px;color:var(--accent);margin-bottom:12px">
      📁 Загрузки и выгрузки сохраняются в папку базы: <code style="background:rgba(0,0,0,0.2);padding:2px 6px;border-radius:4px">{{dop_base_path}}</code>
    </p>

    <form method="POST" enctype="multipart/form-data" id="uploadForm">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <input type="hidden" name="action" value="analyze">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <input type="file" name="file" accept=".csv,.xlsx,.xls,.xlsm,.txt" multiple
          style="flex:1" id="fileInput">
        <button class="btn btn-p" type="submit">🔍 Анализировать</button>
      </div>
      <p style="font-size:11px;color:var(--muted);margin-top:6px">Можно выбрать несколько файлов — при импорте обработаются все.</p>
    </form>

    {% if file_error %}
    <div class="err" style="margin-top:12px">⚠️ {{file_error}}</div>
    {% endif %}

    {% if result and result.rows %}
    <div style="margin-top:16px;padding:14px;background:rgba(59,130,246,0.08);
      border-radius:10px;border:1px solid rgba(59,130,246,0.2)">
      <div style="font-weight:700;font-size:14px;margin-bottom:8px">📊 Результат анализа</div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:12px">
        <div style="text-align:center">
          <div style="font-size:24px;font-weight:800">{{result.rows}}</div>
          <div style="font-size:11px;color:var(--muted)">Строк</div>
        </div>
        <div style="text-align:center">
          <div style="font-size:24px;font-weight:800">{{result.cols}}</div>
          <div style="font-size:11px;color:var(--muted)">Колонок</div>
        </div>
        <div style="text-align:center">
          <div style="font-size:16px;font-weight:700;color:var(--accent)">
            {{ {'clients':'👤 Клиенты','contracts':'📄 Договоры','payments':'💳 Платежи'}.get(result.detected_type,'❓ Неизвестно') }}</div>
          <div style="font-size:11px;color:var(--muted)">Определённый тип</div>
        </div>
      </div>

      {% if preview %}
      <div style="overflow-x:auto;margin-bottom:14px">
        <table style="font-size:11px;width:100%;min-width:600px">
          <thead>
            <tr>{% for col in preview[0].keys() %}
              <th style="background:rgba(0,0,0,0.2);padding:4px 8px;text-align:left;white-space:nowrap">{{col}}</th>
            {% endfor %}</tr>
          </thead>
          <tbody>
          {% for row in preview[:5] %}
          <tr>{% for v in row.values() %}
            <td style="padding:3px 8px;border-bottom:1px solid var(--border);
              font-size:11px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{{v}}">{{v or '—'}}</td>
          {% endfor %}</tr>
          {% endfor %}
          </tbody>
        </table>
        <div style="font-size:11px;color:var(--muted);margin-top:4px">
          Показаны первые 5 из {{result.rows}} строк
        </div>
      </div>
      {% endif %}

      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <input type="hidden" name="action" value="import">
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <div class="field" style="margin:0;flex:1">
            <label style="font-size:12px">Тип при импорте</label>
            <select name="import_type">
              <option value="auto">Авто ({{result.detected_type}})</option>
              <option value="clients">Клиенты</option>
              <option value="contracts">Договоры</option>
            </select>
          </div>
          <button class="btn btn-p" type="submit"
            onclick="return confirm('Импортировать {{result.rows}} строк в базу данных?')"
            style="margin-top:18px">✅ Импортировать в систему</button>
        </div>
      </form>
    </div>
    {% endif %}

    {% if result and result.get('success') %}
    <div style="margin-top:12px;padding:12px 16px;background:rgba(34,197,94,0.1);
      border:1px solid rgba(34,197,94,0.3);border-radius:10px">
      ✅ Импортировано: <b>{{result.imported}}</b> строк.
      Пропущено: {{result.skipped}}.
      Тип: {{result.type}}.
    </div>
    {% endif %}
  </div>
</div>

<div class="sec">
  <div class="sec-head"><h2>🏘 Справочник МФЙ (из Excel)</h2></div>
  <div class="card">
    <p style="font-size:13px;color:var(--muted);margin-bottom:14px">
      Загрузите таблицу Excel (.xls/.xlsx) с колонкой <strong>«МФЙ номи»</strong> (или первая колонка).
      Все листы обрабатываются; названия МФЙ будут привязаны к выбранному региону.
    </p>
    <form id="mfyImportForm" style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
      <div class="field" style="margin:0;min-width:180px">
        <label style="font-size:12px">Регион</label>
        <select name="region_name" id="mfyRegionName" required>
          {% for r in mfy_regions or [] %}
          <option value="{{ r }}" {{ 'selected' if r == 'Андижон' else '' }}>{{ r }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="field" style="margin:0;flex:1;min-width:160px">
        <label style="font-size:12px">Файл Excel</label>
        <input type="file" name="file" accept=".xls,.xlsx,.xlsm" id="mfyFileInput" required>
      </div>
      <button type="submit" class="btn btn-p" id="mfySubmitBtn">📥 Загрузить МФЙ</button>
    </form>
    <div id="mfyResult" style="margin-top:12px;font-size:13px"></div>
    <script>
(function(){
  var form = document.getElementById('mfyImportForm');
  var resultEl = document.getElementById('mfyResult');
  var btn = document.getElementById('mfySubmitBtn');
  if (!form || !resultEl) return;
  form.onsubmit = function(e) {
    e.preventDefault();
    var fd = new FormData(form);
    if (!fd.get('file') || !fd.get('file').name) { resultEl.innerHTML = '<span style="color:var(--red)">Выберите файл</span>'; return; }
    resultEl.innerHTML = 'Загрузка…';
    btn.disabled = true;
    fetch('/api/import/mfy', { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
      .then(function(o) {
        btn.disabled = false;
        var d = o.data;
        if (d.ok) {
          resultEl.innerHTML = '<span style="color:var(--green)">✅ Загружено: ' + (d.imported || 0) + ' МФЙ. Пропущено (дубли): ' + (d.skipped || 0) + '.</span>';
        } else {
          resultEl.innerHTML = '<span style="color:var(--red)">⚠️ ' + (d.error || 'Ошибка') + '</span>';
        }
      })
      .catch(function() { btn.disabled = false; resultEl.innerHTML = '<span style="color:var(--red)">Ошибка сети</span>'; });
  };
})();
    </script>
  </div>
</div>

<div class="sec">
  <div class="sec-head"><h2>📊 Файл скоринга (клиенты + договоры по филиалу)</h2></div>
  <div class="card">
    <p style="font-size:13px;color:var(--muted);margin-bottom:14px">
      Загрузите Excel с колонками: <strong>ФИО, Серия, Паспорт_раками, JSHSHIR (ПИНФЛ), Жинси, Yoshi, region, mfy, Daromad_mj, Лавозими, avtomobili, pl_kartasi, Шартнома санаси, Шарт_тугаш санаси, Шарт_муддати, foizstav, Статуси, Неча марта кечикган</strong> и др. Выберите филиал (Ф1…Ф7) — данные пойдут в базу для поиска и <strong>Аналитики</strong>. Класс клиента (Яхши/Стандарт/Ёмон) сохраняется как status_detail; итоговый класс при скоринге определяет система.
    </p>
    <form id="scoringFullImportForm" style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
      <div class="field" style="margin:0;min-width:180px">
        <label style="font-size:12px">Филиал</label>
        <select name="branch" id="scoringFullBranch" required>
          {% for k, v in (BRANCHES or {}).items() %}
          <option value="{{ k }}">{{ k }} — {{ v }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="field" style="margin:0;flex:1;min-width:160px">
        <label style="font-size:12px">Файл Excel</label>
        <input type="file" name="file" accept=".xls,.xlsx,.xlsm" id="scoringFullFile" required>
      </div>
      <button type="submit" class="btn btn-p" id="scoringFullSubmitBtn">📥 Загрузить (клиенты + договоры)</button>
    </form>
    <div id="scoringFullResult" style="margin-top:12px;font-size:13px"></div>
    <script>
(function(){
  var form = document.getElementById('scoringFullImportForm');
  var resultEl = document.getElementById('scoringFullResult');
  var btn = document.getElementById('scoringFullSubmitBtn');
  if (!form || !resultEl) return;
  form.onsubmit = function(e) {
    e.preventDefault();
    var fd = new FormData(form);
    if (!fd.get('file') || !fd.get('file').name) { resultEl.innerHTML = '<span style="color:var(--red)">Выберите файл</span>'; return; }
    resultEl.innerHTML = 'Загрузка…';
    btn.disabled = true;
    fetch('/api/import/scoring-full', { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function(r) {
        return r.text().then(function(text) {
          var d;
          try { d = JSON.parse(text); } catch(e) {
            throw new Error('Ответ не JSON (HTTP ' + r.status + '): ' + text.slice(0, 300));
          }
          return { ok: r.ok, data: d };
        });
      })
      .then(function(o) {
        btn.disabled = false;
        var d = o.data;
        if (d.ok) {
          resultEl.innerHTML = '<span style="color:var(--green)">✅ Клиентов: ' + (d.imported_clients || 0) + ', договоров: ' + (d.imported_contracts || 0) + '. Пропущено строк: ' + (d.skipped || 0) + '.</span>';
        } else {
          resultEl.innerHTML = '<span style="color:var(--red)">⚠️ ' + (d.error || 'Ошибка импорта') + '</span>';
        }
      })
      .catch(function(e) { btn.disabled = false; resultEl.innerHTML = '<span style="color:var(--red)">Ошибка: ' + (e.message || 'Ошибка сети') + '</span>'; });
  };
})();
    </script>
  </div>
</div>

<div class="sec">
  <div class="sec-head"><h2>📤 Экспорт данных</h2></div>
  <div class="g3">
    <div class="card" style="text-align:center">
      <div style="font-size:28px;margin-bottom:8px">👤</div>
      <div style="font-weight:700;margin-bottom:4px">Клиенты</div>
      <div style="font-size:12px;color:var(--muted);margin-bottom:14px">
        ID, ФИО, паспорт, телефон, возраст
      </div>
      <a href="/export/clients" class="btn btn-p btn-sm" style="width:100%;display:block">⬇️ Скачать Excel</a>
    </div>
    <div class="card" style="text-align:center">
      <div style="font-size:28px;margin-bottom:8px">📄</div>
      <div style="font-weight:700;margin-bottom:4px">Договоры</div>
      <div style="font-size:12px;color:var(--muted);margin-bottom:14px">
        Все договоры с цветовой разметкой НПЛ
      </div>
      <a href="/export/contracts" class="btn btn-p btn-sm" style="width:100%;display:block">⬇️ Скачать Excel</a>
    </div>
    <div class="card" style="text-align:center">
      <div style="font-size:28px;margin-bottom:8px">🔍</div>
      <div style="font-weight:700;margin-bottom:4px">Аудит</div>
      <div style="font-size:12px;color:var(--muted);margin-bottom:14px">
        Лог действий пользователей
      </div>
      <a href="/export/audit" class="btn btn-p btn-sm" style="width:100%;display:block">⬇️ Скачать Excel</a>
    </div>
  </div>
</div>

</div>
""" + F

DATAIO_T = IMPORT_T

DASHBOARD_T = H('Дашборд') + """<div class="ctr">

<div class="page-header fl" style="justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap">
  <h1>📊 Дашборд{% if user.branch %} — {{user.branch}}{% endif %}</h1>
  <div class="fl">
    <select id="dashYear" onchange="dashFilter()"
      style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:6px 12px;font-size:13px">
      <option value="" {{'selected' if not cur_year else ''}}>Все годы</option>
      {% for y in years %}<option value="{{y}}" {{'selected' if cur_year and y==cur_year else ''}}>{{y}}</option>{% endfor %}
    </select>
    {% if not user.branch %}
    <select id="dashBranch" onchange="dashFilter()"
      style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:6px 12px;font-size:13px">
      <option value="">Все филиалы</option>
      {% for k,v in BRANCHES.items() %}
      <option value="{{k}}" {{'selected' if current_branch==k else ''}}>{{k}} — {{v}}</option>
      {% endfor %}
    </select>
    {% endif %}
  </div>
</div>

<div class="cards" id="kpiCards">
  <div class="card">
    <h3>Договоров</h3>
    <div class="val">{{"{:,}".format((kpi.total or 0)|int)}}</div>
    <div class="sub">активных в системе</div>
  </div>
  <div class="card">
    <h3>Клиентов</h3>
    <div class="val">{{"{:,}".format((kpi.clients or 0)|int)}}</div>
    <div class="sub">уникальных</div>
  </div>
  <div class="card">
    <h3>НПЛ</h3>
    <div class="val" style="color:{{'var(--red)' if (kpi.npl_rate or 0)>5 else 'var(--yellow)' if (kpi.npl_rate or 0)>3 else 'var(--green)'}}">
      {{kpi.npl_rate or 0}}%</div>
    <div class="sub">{{"{:,}".format((kpi.npl or 0)|int)}} договоров</div>
  </div>
  <div class="card">
    <h3>Сбор</h3>
    <div class="val" style="color:var(--green)">{{kpi.collection_rate or 0}}%</div>
    <div class="sub">стандартных</div>
  </div>
  <div class="card">
    <h3>Портфель</h3>
    <div class="val" style="font-size:20px">
      {{"{:.1f}".format((kpi.portfolio or 0)/1000000)}} млн</div>
    <div class="sub">суммарный долг</div>
  </div>
</div>

{% if by_branch %}
<div class="sec">
  <div class="sec-head"><h2>По филиалам</h2>{% if dashboard_snapshot_date %}<span style="font-size:12px;color:var(--muted);font-weight:normal"> — данные на {{ dashboard_snapshot_date }}</span>{% endif %}</div>
  <div class="tbl-wrap"><table>
    <thead><tr>
      <th style="width:28px"></th>
      <th>Филиал</th>
      {% if by_branch and 'prri' in (by_branch[0] or {}) %}
      <th>Портфел</th><th>Прри</th><th>Хатар %</th>
      {% else %}
      <th>Договоров</th><th>НПЛ</th><th>НПЛ%</th><th>Сбор%</th><th>Портфель</th>
      {% endif %}
    </tr></thead>
    <tbody>
    {% for b in by_branch %}
    <tr>
      <td><span class="drill-arrow" onclick="toggleBranch('{{b.branch}}',this)">&#9654;</span></td>
      <td><b>{{b.branch}}</b> <span style="color:var(--muted);font-size:12px">— {{BRANCHES.get(b.branch,'')}}</span></td>
      {% if 'prri' in b %}
      <td style="font-size:12px;color:var(--muted)">{{"{:,.0f}".format((b.portfolio or 0)/1)}}</td>
      <td style="font-size:12px;color:var(--muted)">{{"{:,.0f}".format((b.prri or 0)/1)}}</td>
      <td><span class="badge {{'br' if (b.npl_rate or 0)>15 else 'by' if (b.npl_rate or 0)>10 else 'bg'}}">{{"{:.2f}".format(b.npl_rate or 0)}}%</span></td>
      {% else %}
      <td>{{"{:,}".format((b.total or 0)|int)}}</td>
      <td>{{b.npl_count or '—'}}</td>
      <td><span class="badge {{'br' if (b.npl_rate or 0)>5 else 'by' if (b.npl_rate or 0)>3 else 'bg'}}">{{"{:.1f}".format(b.npl_rate or 0)}}%</span></td>
      <td>{{"{:.1f}".format(b.collection_rate or 0)}}%</td>
      <td style="font-size:12px;color:var(--muted)">{{"{:.1f}".format((b.portfolio or 0)/1000000)}} млн</td>
      {% endif %}
    </tr>
    <tr class="drill-row" id="db-{{b.branch}}">
      <td colspan="{{ 6 if 'prri' in b else 7 }}">
        <div class="drill-content" id="db-data-{{b.branch}}"><div style="color:var(--muted);font-size:12px;padding:6px">Загрузка срезов из БД...</div></div>
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table></div>
</div>
{% endif %}

<div class="sec">
  <div class="sec-head"><h2>Разбивка по годам</h2></div>
  <div class="tbl-wrap"><table>
    <thead><tr>
      <th style="width:28px"></th>
      <th>Год</th><th>Договоров</th><th>НПЛ</th>
      <th>НПЛ%</th><th>Сбор%</th><th>Портфель</th>
    </tr></thead>
    <tbody>
    {% for y in yearly %}
    <tr>
      <td><span class="drill-arrow" onclick="toggleYear('{{y.year}}',this)">&#9654;</span></td>
      <td><b>{{y.year}}</b></td>
      <td>{{"{:,}".format(y.total|int)}}</td>
      <td>{{y.npl}}</td>
      <td><span class="badge {{'br' if y.npl_rate>5 else 'by' if y.npl_rate>3 else 'bg'}}">
        {{"{:.1f}".format(y.npl_rate)}}%</span></td>
      <td>{{"{:.1f}".format(y.collection_rate)}}%</td>
      <td style="font-size:12px;color:var(--muted)">
        {{"{:.1f}".format((y.portfolio or 0)/1000000)}} млн</td>
    </tr>
    <tr class="drill-row" id="yr-{{y.year}}">
      <td colspan="7">
        <div class="drill-content" id="yr-data-{{y.year}}">Загрузка...</div>
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table></div>
</div>

<div class="g2">
  <div class="sec">
    <div class="sec-head"><h2>Просрочка (Aging)</h2></div>
    <div class="card">
      {% set total_ag = (aging.d0 or 0)|int + (aging.d30 or 0)|int + (aging.d60 or 0)|int + (aging.d90 or 0)|int + (aging.d90p or 0)|int %}
      {% for label, val, color in [
        ('0 дней (норма)', aging.d0 or 0, 'var(--green)'),
        ('1–30 дней', aging.d30 or 0, '#84cc16'),
        ('31–60 дней', aging.d60 or 0, 'var(--yellow)'),
        ('61–90 дней', aging.d90 or 0, '#f97316'),
        ('90+ дней', aging.d90p or 0, 'var(--red)')
      ] %}
      <div style="margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;margin-bottom:4px">
          <span style="font-size:12px">{{label}}</span>
          <span style="font-size:12px;font-weight:600;color:{{color}}">
            {{"{:,}".format(val|int)}}
            <span style="color:var(--muted);font-weight:400">
              ({{"{:.1f}".format(val|int / total_ag * 100 if total_ag > 0 else 0)}}%)
            </span>
          </span>
        </div>
        <div style="background:rgba(0,0,0,0.2);border-radius:99px;height:6px">
          <div style="background:{{color}};border-radius:99px;height:6px;
            width:{{"{:.1f}".format(val|int / total_ag * 100 if total_ag > 0 else 0)}}%;
            transition:width 0.5s"></div>
        </div>
      </div>
      {% endfor %}
    </div>
  </div>

  <div class="sec">
    <div class="sec-head"><h2>Топ-5 по НПЛ (12 мес)</h2></div>
    <div class="tbl-wrap"><table>
      <thead><tr><th>Сотрудник</th><th>Дог.</th><th>НПЛ</th><th>НПЛ%</th></tr></thead>
      <tbody>
      {% for e in top_employees %}
      <tr>
        <td style="font-size:13px">{{e.employee_name or '—'}}</td>
        <td>{{e.total}}</td>
        <td>{{e.npl}}</td>
        <td><span class="badge {{'br' if (e.npl_rate or 0)>10 else 'by' if (e.npl_rate or 0)>5 else 'bg'}}">
          {{e.npl_rate}}%</span></td>
      </tr>
      {% else %}
      <tr><td colspan="4" style="color:var(--muted);text-align:center;padding:16px">
        Нет данных за 12 месяцев</td></tr>
      {% endfor %}
      </tbody>
    </table></div>
  </div>
</div>

<button class="btn btn-o btn-sm" onclick="refreshAnalytics()"
  style="margin-bottom:32px">🔄 Обновить аналитику</button>

</div>

<style>
.drill-row { display:none; }
.drill-row.open { display:table-row; }
.drill-content { padding:10px 14px; background:rgba(0,0,0,0.12); }
.drill-arrow { cursor:pointer; display:inline-block; transition:transform 0.2s; user-select:none; font-size:10px; }
.drill-arrow.open { transform:rotate(90deg); }
</style>

<script>
function toggleBranch(branch, arrow) {
  var row = document.getElementById('db-' + branch);
  var dd = document.getElementById('db-data-' + branch);
  if (!row || !dd) return;
  if (row.classList.contains('open')) {
    row.classList.remove('open'); arrow.classList.remove('open'); return;
  }
  row.classList.add('open'); arrow.classList.add('open');
  dd.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:6px">Загрузка...</div>';
  fetch('/api/npl-branch-years/' + encodeURIComponent(branch)).then(r => r.json()).then(function(data) {
    if (!data.length) { dd.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:8px">Нет данных</div>'; return; }
    var h = '<table style="font-size:12px;width:100%"><thead><tr><th style="width:28px"></th><th>Год</th><th>Договоров</th><th>НПЛ</th><th>НПЛ%</th></tr></thead><tbody>';
    data.forEach(function(r) {
      var total = r.total_contracts != null ? r.total_contracts : r.total;
      var npl = r.npl_count != null ? r.npl_count : r.npl;
      var nplRate = r.npl_rate != null ? r.npl_rate : (total ? (100 * npl / total).toFixed(1) : 0);
      var clr = nplRate > 5 ? '#ef4444' : nplRate > 3 ? '#eab308' : '#22c55e';
      var key = branch + '-' + (r.year||'');
      var branchEsc = (branch || '').replace(/'/g, "\\\\'");
      var yearEsc = (r.year || '').replace(/'/g, "\\\\'");
      h += '<tr><td><span class="drill-arrow" onclick="toggleBranchYear(\\'' + branchEsc + '\\',\\'' + yearEsc + '\\',this)">&#9654;</span></td><td><b>' + (r.year||'') + '</b></td><td>' + (total||0).toLocaleString('ru') + '</td><td>' + npl + '</td><td style="color:' + clr + ';font-weight:700">' + nplRate + '%</td></tr>';
      h += '<tr class="drill-row" id="by-' + key.replace(/[^a-zA-Z0-9_-]/g, '_') + '"><td colspan="5"><div class="drill-content" id="by-data-' + key.replace(/[^a-zA-Z0-9_-]/g, '_') + '">Загрузка...</div></td></tr>';
    });
    h += '</tbody></table>'; dd.innerHTML = h;
  });
}

function toggleBranchYear(branch, year, arrow) {
  var key = (branch || '') + '-' + (year || '');
  var safeId = key.replace(/[^a-zA-Z0-9_-]/g, '_');
  var row = document.getElementById('by-' + safeId);
  var dd = document.getElementById('by-data-' + safeId);
  if (!row || !dd) return;
  if (row.classList.contains('open')) {
    row.classList.remove('open'); arrow.classList.remove('open'); return;
  }
  row.classList.add('open'); arrow.classList.add('open');
  var url = '/api/monthly/' + encodeURIComponent(year);
  if (branch) url += '?branch=' + encodeURIComponent(branch);
  fetch(url).then(r => r.json()).then(function(data) { renderMonths(dd, data, year); });
}

function toggleYear(year, arrow) {
  var row = document.getElementById('yr-' + year);
  var dd = document.getElementById('yr-data-' + year);
  if (!row || !dd) return;
  if (row.classList.contains('open')) {
    row.classList.remove('open'); arrow.classList.remove('open'); return;
  }
  row.classList.add('open'); arrow.classList.add('open');
  dd.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:6px">Загрузка...</div>';
  fetch('/api/monthly/' + encodeURIComponent(year)).then(r => r.json()).then(function(data) { renderMonths(dd, data, year); });
}

function renderMonths(dd, data, year) {
  if (!data || !data.length) { dd.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:8px">Нет данных</div>'; return; }
  var h = '<table style="font-size:12px;width:100%"><thead><tr><th style="width:28px"></th><th>Месяц</th><th>Договоров</th><th>НПЛ</th><th>НПЛ%</th><th>Сбор%</th><th>Портфель млн</th></tr></thead><tbody>';
  data.forEach(function(r) {
    var period = (year || '') + '-' + (r.month || '');
    var total = r.total_contracts != null ? r.total_contracts : r.total;
    var npl = r.npl_count != null ? r.npl_count : r.npl;
    var nplRate = r.npl_rate != null ? r.npl_rate : (total ? (100 * npl / total).toFixed(1) : 0);
    var collRate = r.collection_rate != null ? r.collection_rate : '';
    var portfolioM = r.portfolio_mln != null ? r.portfolio_mln : (r.portfolio_m != null ? r.portfolio_m : '0');
    var clr = nplRate > 5 ? '#ef4444' : nplRate > 3 ? '#eab308' : '#22c55e';
    var safeId = period.replace(/-/g, '_');
    h += '<tr><td><span class="drill-arrow" onclick="toggleMonth(\\'' + period + '\\',this)">&#9654;</span></td><td>' + period + '</td><td>' + (total||0).toLocaleString('ru') + '</td><td>' + npl + '</td><td style="color:' + clr + ';font-weight:600">' + nplRate + '%</td><td>' + (collRate !== '' ? collRate + '%' : '—') + '</td><td>' + portfolioM + ' млн</td></tr>';
    h += '<tr class="drill-row" id="mo-' + safeId + '"><td colspan="7"><div class="drill-content" id="mo-data-' + safeId + '">Загрузка...</div></td></tr>';
  });
  h += '</tbody></table>'; dd.innerHTML = h;
}

function toggleMonth(period, arrow) {
  var safeId = period.replace(/-/g, '_');
  var row = document.getElementById('mo-' + safeId);
  var dd = document.getElementById('mo-data-' + safeId);
  if (!row || !dd) return;
  if (row.classList.contains('open')) {
    row.classList.remove('open'); arrow.classList.remove('open'); return;
  }
  row.classList.add('open'); arrow.classList.add('open');
  dd.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:6px">Загрузка...</div>';
  var branch = (arrow && arrow.getAttribute && arrow.getAttribute('data-branch')) || (document.getElementById('dashBranch') ? document.getElementById('dashBranch').value : '') || '';
  var url = '/api/month-detail/' + encodeURIComponent(period);
  if (branch) url += '?branch=' + encodeURIComponent(branch);
  fetch(url).then(r => r.json()).then(function(d) {
    var h = '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;padding:4px">';
    h += '<div><div style="font-size:11px;color:var(--muted);font-weight:600;margin-bottom:6px">ПО ФИЛИАЛАМ</div><table style="font-size:11px;width:100%"><thead><tr><th>Филиал</th><th>Дог.</th><th>НПЛ%</th></tr></thead><tbody>';
    (d.by_branch || []).forEach(function(b) {
      var tot = b.total_contracts != null ? b.total_contracts : b.total;
      var nplRate = tot ? (100 * (b.npl_count || 0) / tot).toFixed(1) : '0';
      h += '<tr><td>' + (b.branch || '') + '</td><td>' + tot + '</td><td>' + nplRate + '%</td></tr>';
    });
    h += '</tbody></table></div>';
    h += '<div><div style="font-size:11px;color:var(--muted);font-weight:600;margin-bottom:6px">ПО СТАТУСАМ</div><table style="font-size:11px;width:100%"><thead><tr><th>Статус</th><th>Кол-во</th></tr></thead><tbody>';
    (d.by_status || []).forEach(function(s) {
      var cnt = s.total_contracts != null ? s.total_contracts : s.cnt;
      h += '<tr><td>' + (s.status_detail || s.status || '') + '</td><td>' + cnt + '</td></tr>';
    });
    h += '</tbody></table></div>';
    h += '<div><div style="font-size:11px;color:var(--muted);font-weight:600;margin-bottom:6px">ТОП СОТРУДНИКОВ</div><table style="font-size:11px;width:100%"><thead><tr><th>ФИО</th><th>Дог.</th></tr></thead><tbody>';
    (d.by_employee || []).slice(0, 10).forEach(function(e) {
      h += '<tr><td>' + (e.employee_name || '') + '</td><td>' + (e.total_contracts || 0) + '</td></tr>';
    });
    h += '</tbody></table></div></div>';
    dd.innerHTML = h;
  }).catch(function() {
    dd.innerHTML = '<div style="color:var(--red);font-size:12px;padding:6px">Ошибка загрузки данных</div>';
  });
}

function dashFilter() {
  var y = document.getElementById('dashYear') ? document.getElementById('dashYear').value : '';
  var b = document.getElementById('dashBranch') ? document.getElementById('dashBranch').value : '';
  var params = new URLSearchParams();
  if (y) params.set('year', y);
  if (b) params.set('branch', b);
  window.location.href = '/?' + params.toString();
}

function refreshAnalytics() {
  fetch('/api/refresh-analytics', { method: 'POST' }).then(r => r.json()).then(function() { showToast('Аналитика обновлена ✓'); });
}

function showToast(msg) {
  var t = document.createElement('div');
  t.innerText = msg;
  t.style.cssText = 'position:fixed;bottom:24px;right:24px;background:#22c55e;color:#fff;padding:10px 20px;border-radius:10px;font-size:13px;font-weight:600;z-index:9999;animation:fadeIn 0.3s';
  document.body.appendChild(t);
  setTimeout(function() { t.remove(); }, 3000);
}
</script>
""" + F

SCORING_T = H('Скоринг') + """<div class="ctr">
<div class="page-header"><h1>🎯 Скоринг</h1></div>

<div style="display:flex;gap:8px;margin-bottom:20px">
  <button class="btn btn-p btn-sm" onclick="switchTab('search')" id="tabSearch">🔍 Поиск клиента</button>
  <button class="btn btn-o btn-sm" onclick="switchTab('newclient')" id="tabNew">➕ Новый клиент</button>
</div>

<div id="panelSearch">
  <div class="card" style="margin-bottom:16px">
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <input type="hidden" name="action" value="search">
      <div style="display:flex;gap:10px">
        <input name="query" value="{{query}}" placeholder="ФИО, ID, паспорт (AB1234567), телефон" style="flex:1" autofocus>
        <button class="btn btn-p" type="submit">Найти</button>
      </div>
      <div style="font-size:11px;color:var(--muted);margin-top:6px">
        Примеры: «Усманов», «Мелибоева Зухра», «AB1234567», «998901234567», «12345»
      </div>
    </form>
  </div>

  {% if search_error %}
  <div class="err">⚠️ {{search_error}}</div>
  {% endif %}

  {% if results %}
  <div class="tbl-wrap" style="margin-bottom:16px"><table>
  <thead><tr>
      <th>Филиал</th><th>ID</th><th>ФИО</th><th>Телефон</th><th>Возраст</th>
      <th>Договоров</th><th>Долг</th><th>Просрочка</th><th>НПЛ</th><th></th>
    </tr></thead>
    <tbody>
    {% for r in results %}
    <tr>
      <td style="font-size:11px">{{r.branch or '—'}}</td>
      <td style="color:var(--muted);font-size:11px">{{r.client_code or r.client_id}}</td>
      <td><b>{{r.full_name}}</b></td>
      <td style="font-size:12px">{{r.phone or '—'}}</td>
      <td>{{r.age or '—'}}</td>
      <td>{{r.contracts_count}}</td>
      <td style="font-size:12px">
        {% if r.total_debt and r.total_debt > 0 %}{{"{:,.0f}".format(r.total_debt)}} сум{% else %}—{% endif %}
      </td>
      <td>
        {% if r.max_delay and r.max_delay > 0 %}
        <span class="badge {{'br' if r.max_delay>60 else 'by'}}">{{r.max_delay|int}} дн.</span>
        {% else %}<span style="color:var(--green)">✓</span>{% endif %}
      </td>
      <td>
        {% if r.npl_count and r.npl_count > 0 %}
        <span class="badge br">НПЛ</span>
        {% else %}<span class="badge bg">ОК</span>{% endif %}
      </td>
      <td>
        <form method="POST" style="display:inline">
          <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
          <input type="hidden" name="action" value="score_existing">
          <input type="hidden" name="client_id" value="{{r.client_id}}">
          <button class="btn btn-p btn-sm" type="submit">Скоринг</button>
        </form>
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table></div>
  <div style="font-size:12px;color:var(--muted)">Найдено: {{results|length}} клиентов</div>
  {% elif query %}
  <div class="card" style="text-align:center;padding:32px;color:var(--muted)">
    По запросу «{{query}}» ничего не найдено.<br>
    <span style="font-size:12px">Проверьте написание. Поиск нечувствителен к регистру.</span>
  </div>
  {% endif %}
</div>

<div id="panelNew" style="display:none">
<form method="POST">
<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
<input type="hidden" name="action" value="score_new">
<div class="g3">
  <div class="field"><label>ФИО</label>
    <input type="text" name="full_name" placeholder="ФИО заявителя" required></div>
  <div class="field"><label>Возраст</label>
    <input type="number" name="age" value="35" min="18" max="80" required></div>
  <div class="field"><label>Пол</label>
    <select name="gender">
      <option value="М">Мужской</option>
      <option value="Ж">Женский</option>
    </select></div>
  <div class="field"><label>Паспорт (серия+номер)</label>
    <input type="text" name="passport" placeholder="AB1234567" maxlength="20"></div>
  <div class="field"><label>ПИНФЛ</label>
    <input type="text" name="pinfl" placeholder="14 цифр" maxlength="14" pattern="[0-9]*" inputmode="numeric"></div>
  <div class="field"><label>Филиал</label>
    <select name="branch">
      {% for k,v in BRANCHES.items() %}
      <option value="{{k}}" {{'selected' if user.branch==k else ''}}>{{k}} — {{v}}</option>
      {% endfor %}
    </select></div>
</div>
<div class="g3">
  <div class="field"><label>Источник дохода</label>
    <select name="income_source" required>
      {% for s in INCOME_SOURCES %}
      <option value="{{s}}">{{s}}</option>
      {% endfor %}
    </select></div>
  <div class="field"><label>Должность</label>
    <select name="position">
      {% for p in POSITIONS %}
      <option value="{{p}}">{{p}}</option>
      {% endfor %}
    </select></div>
  <div class="field"><label>Доход в месяц (сум)</label>
    <input type="number" name="monthly_income" value="4000000" min="0" required></div>
</div>
<div class="g3">
  <div class="field"><label>Доп. доход (сум/мес)</label>
    <input type="number" name="extra_income" value="0" min="0"></div>
  <div class="field"><label>Тип доп. дохода</label>
    <select name="extra_income_type">
      <option value="">Нет</option>
      <option>Дивиденды</option>
      <option>Аренда</option>
      <option>Возврат долга</option>
      <option>Прочее</option>
    </select></div>
  <div class="field"><label>Регион</label>
    <select name="region" id="regionSel" onchange="loadMfy()">
      <option value="">Выберите регион</option>
    </select></div>
</div>
<div class="g3">
  <div class="field"><label>МФЙ/Махалля</label>
    <input type="text" name="mfy" id="mfySel" list="mfyList" placeholder="Введите или выберите МФЙ">
    <datalist id="mfyList">
      <option value="">Выберите МФЙ</option>
    </datalist>
  </div>
  <div class="field"><label>Сумма товара (сум)</label>
    <input type="number" name="product_amount" value="8000000" min="0" required></div>
  <div class="field"><label>Первоначальный взнос (%)</label>
    <input type="number" name="advance_pct" value="20" min="0" max="90"></div>
</div>
<div class="g3">
  <div class="field"><label>Срок кредита (месяцев)</label>
    <input type="number" name="term_months" value="12" min="3" max="36"></div>
  <div class="field"><label>Просрочка в др. орг. (мес)</label>
    <select name="external_delay">
      <option value="0">Нет просрочек</option>
      <option value="1">1 месяц</option>
      <option value="2">2 месяца</option>
      <option value="3">3+ месяца</option>
    </select></div>
  <div class="field"><label>Административный штраф</label>
    <select name="has_fine">
      <option value="0">Нет</option>
      <option value="1">Есть</option>
    </select></div>
</div>
<div class="g2">
  <div class="field"><label>Кафиль / поручитель</label>
    <select name="has_guarantor">
      <option value="0">Нет поручителя</option>
      <option value="1">Есть поручитель (+5 баллов)</option>
    </select></div>
  <div class="field"><label>МИБ / Суд задолженность</label>
    <select name="has_mib_debt">
      <option value="0">Нет задолженности</option>
      <option value="1">Есть задолженность (−10 баллов)</option>
    </select></div>
</div>
<div class="g3">
  <div class="field"><label>Наличие банковской карты</label>
    <select name="has_card">
      <option value="0">Нет</option>
      <option value="1">Есть</option>
    </select></div>
  <div class="field"><label>Наличие автомобиля</label>
    <select name="has_car">
      <option value="0">Нет</option>
      <option value="1">Есть</option>
    </select></div>
  <div></div>
</div>
<button class="btn btn-p" type="submit" style="margin-top:8px;width:100%;padding:12px;font-size:15px">⚡ Рассчитать скоринг</button>
</form>
</div>

<!-- РЕЗУЛЬТАТ СКОРИНГА -->
{% if score_result %}
{% if score_result.get('error') %}
<div class="err" style="margin-top:16px">Ошибка: {{score_result.error}}</div>
{% else %}
{% set cls = score_result.get('risk_class','?') %}
{% set cls_color = {'A':'#22c55e','B':'#84cc16','C':'#eab308','D':'#f97316','E':'#ef4444'}.get(cls,'#94a3b8') %}
{% set dec = score_result.get('decision', score_result.get('risk_label','—')) %}
{% set dec_color = {'ОДОБРИТЬ':'#22c55e','НА РАССМОТРЕНИЕ':'#eab308','ОТКЛОНИТЬ':'#ef4444'}.get(dec,'#94a3b8') %}

<div class="card" style="margin-top:20px;border:1px solid {{cls_color}}33">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">
    <div>
      <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">Результат скоринга</div>
      <div style="font-size:48px;font-weight:900;color:{{cls_color}};letter-spacing:-2px;line-height:1">{{cls}}</div>
      <div style="font-size:16px;font-weight:700;color:{{dec_color}};margin-top:4px">{{dec}}</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:13px;color:var(--muted)">Итоговый балл</div>
      <div style="font-size:32px;font-weight:800">{{score_result.get('score', score_result.get('total_score',0))}}</div>
      <div style="font-size:13px;color:var(--muted);margin-top:4px">Ставка</div>
      <div style="font-size:20px;font-weight:700;color:{{cls_color}}">{{score_result.get('interest_rate','—')}}%/мес</div>
    </div>
    <div>
      <a href="#" onclick="printResult(event); return false;" class="btn btn-o">🖨️ Распечатать</a>
    </div>
  </div>

  <div style="margin:16px 0">
    <div style="background:rgba(0,0,0,0.2);border-radius:99px;height:8px">
      <div style="background:{{cls_color}};border-radius:99px;height:8px;width:{{[score_result.get('score', score_result.get('total_score',0))|int,100]|min}}%;transition:width 0.8s"></div>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:3px">
      <span>0</span><span>E(&lt;20)</span><span>D(20)</span><span>C(30)</span><span>B(42)</span><span>A(55+)</span><span>100</span>
    </div>
  </div>

  {% set cl = score_result.get('client', {}) %}
  {% if cl and cl.get('full_name') %}
  <div style="margin-top:14px;padding:12px;background:rgba(59,130,246,0.06);border:1px solid rgba(59,130,246,0.2);border-radius:10px">
    <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Карточка клиента</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:13px">
      <div><span style="color:var(--muted)">ФИО:</span> <b>{{cl.full_name}}</b></div>
      {% if cl.get('age') %}<div><span style="color:var(--muted)">Возраст:</span> {{cl.age}}</div>{% endif %}
      {% if cl.get('passport') %}<div><span style="color:var(--muted)">Паспорт:</span> {{cl.passport|string|replace('.0','')}}</div>
      {% elif cl.get('external_id') %}<div><span style="color:var(--muted)">ПИНФЛ:</span> {{cl.external_id|int}}</div>{% endif %}
      {% if cl.get('gender') %}<div><span style="color:var(--muted)">Пол:</span> {{cl.gender}}</div>{% endif %}
      {% if cl.get('phone') %}<div><span style="color:var(--muted)">Тел:</span> {{cl.phone}}</div>{% endif %}
      {% if cl.get('income_source') %}<div><span style="color:var(--muted)">Источник дохода:</span> {{cl.income_source}}</div>{% endif %}
      {% if cl.get('region') %}<div><span style="color:var(--muted)">Регион:</span> {{cl.region}}</div>{% endif %}
      {% if cl.get('position') %}<div><span style="color:var(--muted)">Должность:</span> {{cl.position}}</div>{% endif %}
      {% set cl_br = cl.get('branch') or (cl.get('contracts', [{}])[0].get('branch') if cl.get('contracts') else '') %}
      {% if cl_br %}<div><span style="color:var(--muted)">Филиал:</span> {{cl_br}}</div>{% endif %}
      {% if cl.get('contracts_count') %}<div><span style="color:var(--muted)">Договоров:</span> {{cl.contracts_count}}</div>{% endif %}
      {% if cl.get('id') %}<div><span style="color:var(--muted)">ID:</span> {{cl.id}}</div>{% endif %}
    </div>
  </div>
  {% endif %}

  {% if score_result.get('restriction') %}
  <div style="margin-top:12px;padding:10px 14px;background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.4);border-radius:8px;font-size:13px;color:#fca5a5">
    ⛔ Ограничение: {{ score_result.restriction }}
  </div>
  {% endif %}
  {% if score_result.get('incentive') %}
  <div style="margin-top:12px;padding:10px 14px;background:rgba(34,197,94,0.12);border:1px solid rgba(34,197,94,0.4);border-radius:8px;font-size:13px;color:#86efac">
    ✅ Поощрение: {{ score_result.incentive }}
  </div>
  {% endif %}

  {% if score_result.get('breakdown') %}
  <div class="sec-head" style="margin-top:12px"><h3 style="font-size:14px">Разбивка по факторам</h3></div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:0">
    {% for f in score_result.breakdown %}
    <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);font-size:13px">
      <span>{{f.criterion}}</span>
      <span style="font-weight:700;color:{{'#22c55e' if f.points > 0 else '#ef4444' if f.points < 0 else 'var(--muted)'}}">
        {{'+' if f.points > 0 else ''}}{{f.points}}</span>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  {% if score_result.get('decision') != 'ОТКЛОНИТЬ' and score_result.get('risk_label') != 'Отказ' %}
  <div style="margin-top:16px;padding:14px;background:rgba(59,130,246,0.08);border-radius:10px">
    <div style="font-size:13px;font-weight:600;margin-bottom:10px">💰 Калькулятор платежа</div>
    <div class="g3">
      <div>
        <div style="font-size:11px;color:var(--muted)">Сумма кредита</div>
        <input type="number" id="calcAmount" value="{{score_result.get('form_data',{}).get('product_amount',8000000)|int}}" style="margin-top:4px" oninput="calcPayment()">
      </div>
      <div>
        <div style="font-size:11px;color:var(--muted)">Первоначальный взнос %</div>
        <input type="number" id="calcAdvance" value="{{score_result.get('form_data',{}).get('advance_pct',20)|int}}" min="0" max="90" style="margin-top:4px" oninput="calcPayment()">
      </div>
      <div>
        <div style="font-size:11px;color:var(--muted)">Срок (мес)</div>
        <input type="number" id="calcTerm" value="{{score_result.get('form_data',{}).get('term_months',12)|int}}" min="3" max="36" style="margin-top:4px" oninput="calcPayment()">
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-top:12px">
      <div style="text-align:center">
        <div style="font-size:11px;color:var(--muted)">К выдаче</div>
        <div style="font-size:16px;font-weight:700" id="calcBody">—</div>
      </div>
      <div style="text-align:center">
        <div style="font-size:11px;color:var(--muted)">Платёж/мес</div>
        <div style="font-size:16px;font-weight:700;color:{{cls_color}}" id="calcMonthly">—</div>
      </div>
      <div style="text-align:center">
        <div style="font-size:11px;color:var(--muted)">Итого с %</div>
        <div style="font-size:16px;font-weight:700" id="calcTotal">—</div>
      </div>
    </div>
  </div>
  {% endif %}

  {% if score_result.get('max_limit') %}
  <div style="margin-top:12px;padding:10px 14px;background:rgba(0,0,0,0.1);border-radius:9px;font-size:13px">
    📋 Максимальный лимит: <b>{{"{:,.0f}".format(score_result.max_limit)}} сум</b>
    {% if score_result.get('limit_blocked') %}
    <span class="badge br" style="margin-left:8px">Лимит заблокирован</span>
    {% endif %}
  </div>
  {% endif %}
</div>

<script>
var RATE = {{ score_result.get('interest_rate', 3.3) }};
function calcPayment() {
  var amount = parseFloat(document.getElementById('calcAmount').value) || 0;
  var adv = parseFloat(document.getElementById('calcAdvance').value) || 0;
  var term = parseInt(document.getElementById('calcTerm').value) || 12;
  var body = amount * (1 - adv/100);
  var monthly = body * (RATE/100) * Math.pow(1+RATE/100,term) / (Math.pow(1+RATE/100,term)-1);
  var total = monthly * term;
  var fmt = function(n) { return Math.round(n).toLocaleString('ru') + ' сум'; };
  var elBody = document.getElementById('calcBody');
  var elMon = document.getElementById('calcMonthly');
  var elTot = document.getElementById('calcTotal');
  if (elBody) elBody.innerText = fmt(body);
  if (elMon) elMon.innerText = fmt(monthly);
  if (elTot) elTot.innerText = fmt(total);
}
function printResult(e) {
  if (e) e.preventDefault();
  var data = {% if score_result %}{{ score_result|tojson|safe }}{% else %}null{% endif %};
  if (!data) return;
  var client = data.client || {};
  var params = new URLSearchParams({
    score: data.score != null ? data.score : (data.total_score || 0),
    risk_class: data.risk_class || '—',
    decision: data.decision || data.risk_label || '—',
    rate: data.interest_rate != null ? data.interest_rate : '—',
    name: client.full_name || 'Новый клиент',
    age: client.age != null ? client.age : '',
    income_source: client.income_source || '',
    amount: (data.form_data && data.form_data.product_amount != null)
      ? data.form_data.product_amount
      : (data.calculator && data.calculator.credit != null ? data.calculator.credit : ''),
    branch: client.branch || '',
    client_id: client.id || ''
  });
  window.open('/scoring/print?' + params.toString(), '_blank');
}
calcPayment();
</script>
{% endif %}
{% endif %}

<script>
function switchTab(tab) {
  document.getElementById('panelSearch').style.display = tab === 'search' ? '' : 'none';
  document.getElementById('panelNew').style.display = tab === 'newclient' ? '' : 'none';
  var tS = document.getElementById('tabSearch'); var tN = document.getElementById('tabNew');
  if (tS) tS.className = 'btn btn-sm ' + (tab === 'search' ? 'btn-p' : 'btn-o');
  if (tN) tN.className = 'btn btn-sm ' + (tab === 'newclient' ? 'btn-p' : 'btn-o');
}
fetch('/api/geo/regions').then(r => r.json()).then(function(data) {
  var sel = document.getElementById('regionSel');
  if (!sel) return;
  (data || []).forEach(function(r) {
    var o = document.createElement('option');
    o.value = (r && r.name) != null ? r.name : r; o.textContent = (r && r.name) != null ? r.name : r;
    sel.appendChild(o);
  });
}).catch(function() {});
function loadMfy() {
  var region = document.getElementById('regionSel') && document.getElementById('regionSel').value;
  var list = document.getElementById('mfyList');
  if (!list) return;
  list.innerHTML = '<option value="">Выберите МФЙ</option>';
  if (!region) return;
  fetch('/api/geo/mfy/' + encodeURIComponent(region))
    .then(r => r.json())
    .then(function(data) {
      (data || []).forEach(function(m) {
        var opt = document.createElement('option');
        var val = (m && m.name) != null ? m.name : m;
        opt.value = val;
        list.appendChild(opt);
      });
    })
    .catch(function() {});
}
switchTab('search');
</script>
""" + F

PRINT_T = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Скоринг</title>
<style>*{font-family:Arial,sans-serif;margin:0;padding:0}body{padding:30px;max-width:800px;margin:0 auto}
h1{font-size:20px;border-bottom:2px solid #333;padding-bottom:8px;margin-bottom:16px}
.row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #eee;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0}
th{background:#f0f0f0;padding:6px;border:1px solid #ddd;text-align:left}td{padding:5px 6px;border:1px solid #ddd}
.big{font-size:48px;font-weight:800;text-align:center;padding:14px;margin:14px auto;border:3px solid;border-radius:16px;display:inline-block}
.pb{padding:8px 24px;background:#3b82f6;color:#fff;border:none;border-radius:8px;cursor:pointer;margin:14px 0}
@media print{.pb{display:none}}</style></head><body>
<h1>💳 CCC — Скоринг-карта</h1>
<div style="text-align:center"><div class="big" style="color:{{result.color}};border-color:{{result.color}}">{{result.total_score}}</div>
<div style="font-size:18px;font-weight:700">Класс {{result.risk_class}} — {{result.decision}} | Ставка: {{result.interest_rate}}%</div></div>
<div class="row"><b>ФИО</b><span>{{result.client.full_name}}</span></div>
{% if result.client.id %}<div class="row"><b>ID клиента</b><span>{{result.client.id}}</span></div>{% endif %}
<div class="row"><b>Возраст</b><span>{{result.client.get('age','')}}</span></div>
<div class="row"><b>Доход</b><span>{{result.client.get('income_source','')}}</span></div>
<div class="row"><b>Регион</b><span>{{result.client.get('region','')}}</span></div>
<table><thead><tr><th>Критерий</th><th>Баллы</th><th>Описание</th></tr></thead><tbody>
{% for n,s,d in result.breakdown %}<tr><td><b>{{n}}</b></td><td style="font-weight:700;color:{% if s>0 %}green{% elif s<0 %}red{% else %}gray{% endif %}">{{s}}</td><td>{{d}}</td></tr>{% endfor %}
<tr style="background:#f0f0f0"><td><b>ИТОГО</b></td><td style="font-size:15px;color:{{result.color}}">{{result.total_score}}</td><td>{{result.risk_label}}</td></tr></tbody></table>
{% if result.calculator %}<div style="display:flex;gap:12px;margin:10px 0">
<div style="flex:1;padding:8px;background:#f5f5f5;border-radius:8px;text-align:center"><div style="font-size:10px;color:#666">Кредит</div><div style="font-weight:700">{{"{:,.0f}".format(result.calculator.credit)}}</div></div>
<div style="flex:1;padding:8px;background:#f5f5f5;border-radius:8px;text-align:center"><div style="font-size:10px;color:#666">Ежемесячно</div><div style="font-weight:700">{{"{:,.0f}".format(result.calculator.monthly)}}</div></div>
<div style="flex:1;padding:8px;background:#f5f5f5;border-radius:8px;text-align:center"><div style="font-size:10px;color:#666">Итого ({{result.calculator.rate}}%)</div><div style="font-weight:700">{{"{:,.0f}".format(result.calculator.total)}}</div></div></div>{% endif %}
<div class="row" style="margin-top:14px;border-top:1px solid #333;padding-top:8px"><b>Дата</b><span>{{now}}</span></div>
<button class="pb" onclick="window.print()">🖨️ Печать</button></body></html>"""

ALERTS_T = H('Алерты') + """<div class="ctr">
<div class="page-header">
  <h1>🚨 Алерты <span id="alertBadge" style="font-size:16px;background:#ef4444;color:#fff;
    padding:2px 10px;border-radius:99px;vertical-align:middle">{{critical}}</span></h1>
  <div class="fl" style="gap:8px">
    <select onchange="filterAlerts()" id="periodFilter"
      style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:5px 10px;font-size:13px">
      <option value="" {{'selected' if not flt_days and not flt_from else ''}}>Всё время</option>
      <option value="7"   {{'selected' if flt_days=='7' else ''}}>За 7 дней</option>
      <option value="30"  {{'selected' if flt_days=='30' else ''}}>За 30 дней</option>
      <option value="90"  {{'selected' if flt_days=='90' else ''}}>За 90 дней</option>
      <option value="180" {{'selected' if flt_days=='180' else ''}}>За 6 мес</option>
      <option value="365" {{'selected' if flt_days=='365' else ''}}>За год</option>
      <option value="custom" {{'selected' if flt_from else ''}}>Период от-до</option>
    </select>
    <span id="dateRangeWrap" style="display:{{'flex' if flt_from else 'none'}};gap:6px;align-items:center">
      <input type="date" id="dateFrom" value="{{flt_from or ''}}"
        style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 8px;font-size:12px">
      <span style="font-size:12px;color:var(--muted)">—</span>
      <input type="date" id="dateTo" value="{{flt_to or ''}}"
        style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 8px;font-size:12px">
      <button class="btn btn-p btn-sm" onclick="filterAlerts()" style="padding:4px 10px;font-size:12px">OK</button>
    </span>
    <select onchange="filterAlerts()" id="typeFilter"
      style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:5px 10px;font-size:13px">
      <option value="">Все типы</option>
      <option value="critical" {{'selected' if flt_type=='critical' else ''}}>Критические</option>
      <option value="warning"  {{'selected' if flt_type=='warning' else ''}}>Предупреждения</option>
      <option value="info"     {{'selected' if flt_type=='info' else ''}}>Информационные</option>
      <option value="positive" {{'selected' if flt_type=='positive' else ''}}>Позитивные</option>
    </select>
    {% if not user.branch %}
    <select onchange="filterAlerts()" id="branchFilter"
      style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:5px 10px;font-size:13px">
      <option value="">Все филиалы</option>
      {% for k,v in BRANCHES.items() %}
      <option value="{{k}}" {{'selected' if k==flt_branch else ''}}>{{k}} — {{v}}</option>
      {% endfor %}
    </select>
    {% endif %}
    <button class="btn btn-o btn-sm" onclick="refreshAlerts()">🔄 Обновить</button>
  </div>
</div>

<div class="cards" style="margin-bottom:20px">
  <div class="card" onclick="filterByType('critical')" style="cursor:pointer;border:1px solid #ef4444aa">
    <h3 style="color:#ef4444">Критических</h3>
    <div class="val" style="color:#ef4444">{{critical}}</div>
    <div class="sub">Требуют немедленного действия</div>
  </div>
  <div class="card" onclick="filterByType('warning')" style="cursor:pointer;border:1px solid #eab308aa">
    <h3 style="color:#eab308">Предупреждений</h3>
    <div class="val" style="color:#eab308">{{warning}}</div>
    <div class="sub">Требуют внимания</div>
  </div>
  <div class="card">
    <h3>Всего алертов</h3>
    <div class="val">{{alerts|length}}</div>
    <div class="sub">Обновлено только что</div>
  </div>
</div>

<div class="card" style="margin-bottom:14px">
  <div class="sec-head"><h3>Алерты процесса взыскания</h3></div>
  <div class="fl" style="gap:8px;flex-wrap:wrap;margin-bottom:10px">
    <select id="cStageFilter" onchange="filterAlerts()" style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:5px 10px;font-size:13px">
      <option value="" {{'selected' if not c_stage else ''}}>Все этапы</option>
      <option value="call_center" {{'selected' if c_stage=='call_center' else ''}}>Ундирув оператор (1-43)</option>
      <option value="collection" {{'selected' if c_stage=='collection' else ''}}>Ундирувчи (43-90)</option>
      <option value="legal" {{'selected' if c_stage=='legal' else ''}}>Юрист (90+)</option>
    </select>
    <select id="cStatusFilter" onchange="filterAlerts()" style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:5px 10px;font-size:13px">
      <option value="" {{'selected' if not c_status else ''}}>Все статусы</option>
      <option value="pending" {{'selected' if c_status=='pending' else ''}}>Ожидает</option>
      <option value="in_progress" {{'selected' if c_status=='in_progress' else ''}}>В работе</option>
      <option value="done" {{'selected' if c_status=='done' else ''}}>Выполнено</option>
      <option value="overdue" {{'selected' if c_status=='overdue' else ''}}>Просрочено</option>
    </select>
    <select id="cOverdueFilter" onchange="filterAlerts()" style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:5px 10px;font-size:13px">
      <option value="" {{'selected' if not c_overdue else ''}}>Все просрочки</option>
      <option value="1_43" {{'selected' if c_overdue=='1_43' else ''}}>1-43</option>
      <option value="43_90" {{'selected' if c_overdue=='43_90' else ''}}>43-90</option>
      <option value="90p" {{'selected' if c_overdue=='90p' else ''}}>90+</option>
    </select>
  </div>
  <div class="tbl-wrap"><table>
    <thead><tr><th>Клиент</th><th>Филиал</th><th>Ответственный</th><th>Этап</th><th>Просрочка</th><th>Дедлайн</th><th>Статус</th><th>Приоритет</th></tr></thead>
    <tbody>
      {% for r in collection_rows %}
      {% set st_color = '#22c55e' if r.status=='done' else '#eab308' if r.status in ('pending','in_progress') else '#ef4444' %}
      <tr>
        <td><b>{{r.full_name or ('ID ' ~ r.client_id)}}</b></td>
        <td>{{r.branch_id or '—'}}</td>
        <td>{{r.assigned_to or '—'}}</td>
        <td>{{r.stage_label}}</td>
        <td>{{r.overdue_days or 0}} дн.</td>
        <td>{{r.deadline_date or '—'}}</td>
        <td><span class="badge" style="background:{{st_color}}22;color:{{st_color}};border:1px solid {{st_color}}66">{{r.status_label}}</span></td>
        <td>{{r.priority_label}}</td>
      </tr>
      {% else %}
      <tr><td colspan="8" style="text-align:center;color:var(--muted);padding:16px">Нет алертов процесса взыскания по выбранным фильтрам</td></tr>
      {% endfor %}
    </tbody>
  </table></div>
</div>

{% if alerts %}
<div id="alertsList">
{% for a in alerts %}
{% set bg = {'critical':'rgba(239,68,68,0.08)','warning':'rgba(234,179,8,0.08)',
             'info':'rgba(59,130,246,0.08)','positive':'rgba(34,197,94,0.08)'}.get(a.type,'') %}
{% set border = {'critical':'#ef444433','warning':'#eab30833',
                 'info':'#3b82f633','positive':'#22c55e33'}.get(a.type,'var(--border)') %}
<div class="alert-card" id="alert-{{a.id}}"
  style="background:{{bg}};border:1px solid {{border}};border-radius:12px;
         padding:16px;margin-bottom:12px;display:flex;align-items:flex-start;gap:14px">
  <div style="font-size:24px;flex-shrink:0">{{a.icon}}</div>
  <div style="flex:1">
    <div style="font-size:15px;font-weight:700;margin-bottom:4px">{{a.title}}</div>
    <div style="font-size:13px;color:var(--muted);line-height:1.5">{{a.text}}</div>
  </div>
  <div style="display:flex;gap:8px;flex-shrink:0">
    {% if a.action_url %}
    <a href="{{a.action_url}}" class="btn btn-p btn-sm alert-detail-link">Детали</a>
    {% endif %}
    <button class="btn btn-o btn-sm" onclick="dismissAlert('{{a.id}}')">✕</button>
  </div>
</div>
{% endfor %}
</div>
{% else %}
<div class="card" style="text-align:center;padding:48px;color:var(--muted)">
  <div style="font-size:48px;margin-bottom:12px">✅</div>
  <div style="font-size:18px;font-weight:600">Все показатели в норме</div>
  <div style="font-size:13px;margin-top:8px">Алертов нет. Портфель под контролем.</div>
</div>
{% endif %}

</div>

<script>
function filterAlerts() {
  var d = document.getElementById('periodFilter') && document.getElementById('periodFilter').value;
  var t = document.getElementById('typeFilter').value;
  var b = document.getElementById('branchFilter') ? document.getElementById('branchFilter').value : '';
  var cs = document.getElementById('cStageFilter') ? document.getElementById('cStageFilter').value : '';
  var cst = document.getElementById('cStatusFilter') ? document.getElementById('cStatusFilter').value : '';
  var cod = document.getElementById('cOverdueFilter') ? document.getElementById('cOverdueFilter').value : '';
  var wrap = document.getElementById('dateRangeWrap');
  if (d === 'custom') { if (wrap) wrap.style.display = 'flex'; }
  else { if (wrap) wrap.style.display = 'none'; }
  var q = new URLSearchParams();
  if (d === 'custom') {
    var df = document.getElementById('dateFrom') && document.getElementById('dateFrom').value;
    var dt = document.getElementById('dateTo') && document.getElementById('dateTo').value;
    if (df) q.set('from', df);
    if (dt) q.set('to', dt);
  } else {
    if(d) q.set('days',d);
  }
  if(t) q.set('type',t); if(b) q.set('branch',b);
  if(cs) q.set('c_stage', cs);
  if(cst) q.set('c_status', cst);
  if(cod) q.set('c_overdue', cod);
  window.location.href = '/alerts?' + q.toString();
}
document.getElementById('periodFilter').addEventListener('change', function() {
  var wrap = document.getElementById('dateRangeWrap');
  if (this.value === 'custom') { if (wrap) wrap.style.display = 'flex'; }
  else { if (wrap) wrap.style.display = 'none'; }
});
function filterByType(t) {
  document.getElementById('typeFilter').value = t;
  filterAlerts();
}
function dismissAlert(id) {
  var el = document.getElementById('alert-' + id);
  if(el) el.style.transition='opacity 0.3s', el.style.opacity='0',
    setTimeout(function(){ el.remove(); }, 300);
}
function refreshAlerts() { window.location.reload(); }
function goDetail(e) {
  e.preventDefault();
  var el = e.currentTarget;
  var url = (el.getAttribute('href') || el.href) || '';
  var b = document.getElementById('branchFilter') ? document.getElementById('branchFilter').value : '';
  var d = document.getElementById('periodFilter') ? document.getElementById('periodFilter').value : '';
  if (url) {
    try {
      var u = new URL(url, window.location.origin);
      if (b) u.searchParams.set('branch', b);
      if (d) u.searchParams.set('days', d);
      url = u.pathname + (u.search ? u.search : '');
    } catch (err) {}
  }
  if (url) window.location.href = url;
}
document.querySelectorAll('.alert-detail-link').forEach(function(a) {
  a.addEventListener('click', goDetail);
});
setTimeout(function(){ window.location.reload(); }, 60000);
</script>
""" + F

ALERT_DETAIL_T = H('Детали алерта') + """<div class="ctr">
<div class="page-header">
  <h1>🔍 {{title}}</h1>
  <a href="/alerts{{back_qs or ''}}" class="btn btn-o btn-sm">← Назад</a>
</div>
<div class="tbl-wrap"><table>
  <thead><tr>
    <th>ФИО</th><th>Филиал</th><th>Сотрудник</th>
    <th>Дней просрочки</th><th>Долг (договор / всего)</th><th>Статус</th>
    <th>Действие</th>
  </tr></thead>
  <tbody>
  {% for r in rows %}
  <tr>
    <td><b>{{r.full_name}}</b></td>
    <td>{{r.branch}}</td>
    <td style="font-size:12px">{{r.employee_name or '—'}}</td>
    <td>
      <span class="badge {{'br' if (r.delay_days or 0)>30 else 'by' if (r.delay_days or 0)>0 else 'bg'}}">
        {{r.delay_days or 0}} дн.</span>
    </td>
    <td>
      {{ "{:,.0f}".format(r.total_debt or 0) }}
      <span style="color:var(--muted);font-size:11px">/ {{ "{:,.0f}".format(r.client_total_debt or 0) }}</span>
    </td>
    <td><span class="badge {{'br' if r.status in ('Ёмон','МИБ','Судда') else 'bg'}}">
      {{r.status}}</span></td>
    <td>
      <form method="POST" action="/scoring" style="display:inline">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <input type="hidden" name="action" value="score_existing">
        <input type="hidden" name="client_id" value="{{r.get('client_id','')}}">
        <button class="btn btn-p btn-sm" type="submit">Скоринг</button>
      </form>
    </td>
  </tr>
  {% else %}
  <tr><td colspan="7" style="text-align:center;color:var(--muted);padding:24px">Нет данных</td></tr>
  {% endfor %}
  </tbody>
</table></div>
<div style="margin-top:12px;font-size:12px;color:var(--muted)">Всего: {{rows|length}} записей</div>
</div>
""" + F

PORTFOLIO_T = H('Портфель') + """<div class="ctr">
<div class="page-header">
  <h1>📁 Портфель</h1>
  <div class="fl" style="gap:8px;flex-wrap:wrap">
    <select onchange="applyFilter()" id="pfYear"
      style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:5px 10px;font-size:13px">
      <option value="" {{'selected' if not year else ''}}>Все годы</option>
      {% for y in years %}<option value="{{y}}" {{'selected' if year and y==year else ''}}>{{y}}</option>{% endfor %}
    </select>
    {% if not user.branch %}
    <select onchange="applyFilter()" id="pfBranch"
      style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:5px 10px;font-size:13px">
      <option value="" {{'selected' if not branch_f else ''}}>Все филиалы</option>
      {% for k,v in BRANCHES.items() %}
      <option value="{{k}}" {{'selected' if branch_f and k==branch_f else ''}}>{{k}} — {{v}}</option>
      {% endfor %}
    </select>
    {% endif %}
    <select onchange="applyFilter()" id="pfProduct"
      style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:5px 10px;font-size:13px">
      <option value="" {{'selected' if not product_f else ''}}>Все продукты</option>
      {% for pr in products %}<option value="{{pr}}" {{'selected' if product_f and pr==product_f else ''}}>{{pr}}</option>{% endfor %}
    </select>
    <div class="debt-filter-chip" onclick="toggleDebtModal()" title="Сумма долга по договору">
      <span id="debtFilterLabel">Сумма долга: {% if not debt_filters %}Все{% else %}{{ debt_filters[0:2]|join(', ') }}{% if debt_filters|length > 2 %}…{% endif %}{% endif %}</span>
      <span class="chip-arrow">▾</span>
    </div>
    <div id="debtModal" class="debt-modal" style="display:none">
      <div class="debt-modal-inner">
        <h4>Сумма долга по договору</h4>
        <form id="debtFilterForm">
          {% for label, mn, mx in DEBT_BANDS %}
          <label><input type="checkbox" name="debt_filter" value="{{label}}" {{'checked' if label in debt_filters else ''}}> {{label}}</label>
          {% endfor %}
        </form>
        <div style="margin-top:12px;display:flex;gap:8px">
          <button type="button" class="btn btn-p btn-sm" onclick="applyDebtFilter()">Применить</button>
          <button type="button" class="btn btn-o btn-sm" onclick="toggleDebtModal()">Отмена</button>
        </div>
      </div>
    </div>
    <a href="/export/portfolio?year={{year}}&branch={{branch_f}}&product={{product_f}}{% for d in debt_filters %}&debt_filter={{d|urlencode}}{% endfor %}" class="btn btn-o btn-sm">⬇️ Excel</a>
  </div>
</div>

<div class="cards">
  <div class="card"><h3>Договоров</h3><div class="val">{{"{:,}".format((kpi.total or 0)|int)}}</div></div>
  <div class="card"><h3>НПЛ</h3><div class="val" style="color:{{'var(--red)' if (kpi.npl_rate or 0)>5 else 'var(--yellow)' if (kpi.npl_rate or 0)>3 else 'var(--green)'}}">{{kpi.npl_rate or 0}}%</div><div class="sub">{{"{:,}".format((kpi.npl or 0)|int)}} договоров</div></div>
  <div class="card"><h3>Портфель</h3><div class="val" style="font-size:18px">{{"{:.1f}".format((kpi.portfolio or 0)/1000000)}} млн</div></div>
  <div class="card"><h3>НПЛ-долг</h3><div class="val" style="font-size:18px;color:var(--red)">{{"{:.1f}".format((kpi.npl_debt or 0)/1000000)}} млн</div></div>
  <div class="card"><h3>Сбор</h3><div class="val" style="color:var(--green)">{{kpi.collection_rate or 0}}%</div></div>
</div>

<div class="sec">
  <div class="sec-head"><h2>По филиалам</h2></div>
  <div class="tbl-wrap"><table id="tblBranch">
    <thead>
      <tr>
        <th style="width:28px"></th>
        <th onclick="sortTable('tblBranch',1)" style="cursor:pointer">Филиал ↕</th>
        <th onclick="sortTable('tblBranch',2)" style="cursor:pointer">Договоров ↕</th>
        <th>НПЛ</th>
        <th onclick="sortTable('tblBranch',4)" style="cursor:pointer">НПЛ% ↕</th>
        <th onclick="sortTable('tblBranch',5)" style="cursor:pointer">Сбор% ↕</th>
        <th>Портфель</th>
      </tr>
    </thead>
    <tbody>
    {% for br in by_branch %}
    <tr>
      <td><span class="drill-arrow" onclick="toggleBranch('{{br.branch}}',this)">▶</span></td>
      <td><b>{{br.branch}}</b> <span style="color:var(--muted);font-size:11px">{{BRANCHES.get(br.branch,'')}}</span></td>
      <td>{{"{:,}".format(br.total|int)}}</td>
      <td>{{br.npl_count}}</td>
      <td><span class="badge {{'br' if br.npl_rate>5 else 'by' if br.npl_rate>3 else 'bg'}}">{{"{:.1f}".format(br.npl_rate)}}%</span></td>
      <td>{{"{:.1f}".format(br.collection_rate)}}%</td>
      <td style="font-size:12px;color:var(--muted)">{{"{:.1f}".format((br.portfolio or 0)/1000000)}} млн</td>
    </tr>
    <tr class="drill-row" id="pb-{{br.branch}}"><td colspan="7"><div class="drill-content" id="pb-data-{{br.branch}}">Загрузка...</div></td></tr>
    {% endfor %}
    </tbody>
  </table></div>
</div>

<div class="g2">
<div class="sec">
  <div class="sec-head"><h2>Aging (просрочка)</h2></div>
  <div class="card">
  {% set tot = (aging.d0 or 0)|int + (aging.d30 or 0)|int + (aging.d60 or 0)|int + (aging.d90 or 0)|int + (aging.d90p or 0)|int %}
  {% for label,cnt,debt,color in [
    ('0 дней',aging.d0 or 0,aging.d0_sum or 0,'var(--green)'),
    ('1–30 дней',aging.d30 or 0,aging.d30_sum or 0,'#84cc16'),
    ('31–60 дней',aging.d60 or 0,aging.d60_sum or 0,'var(--yellow)'),
    ('61–90 дней',aging.d90 or 0,aging.d90_sum or 0,'#f97316'),
    ('90+ дней',aging.d90p or 0,aging.d90p_sum or 0,'var(--red)')
  ] %}
  <div style="margin-bottom:12px">
    <div style="display:flex;justify-content:space-between;margin-bottom:3px;font-size:12px">
      <span>{{label}}</span>
      <span style="color:{{color}};font-weight:600">
        {{"{:,}".format(cnt|int)}} дог. · {{"{:.1f}".format((debt or 0)/1000000)}} млн
      </span>
    </div>
    <div style="background:rgba(0,0,0,0.2);border-radius:99px;height:7px">
      <div style="background:{{color}};border-radius:99px;height:7px;width:{{"{:.1f}".format((cnt|int)*100/tot if tot and tot>0 else 0)}}%"></div>
    </div>
  </div>
  {% endfor %}
  </div>
</div>

<div class="sec">
  <div class="sec-head"><h2>По продуктам</h2></div>
  <div class="tbl-wrap"><table id="productTable">
    <thead><tr><th></th><th>Продукт</th><th>Дог.</th><th>НПЛ</th><th>НПЛ%</th><th>Портфель</th></tr></thead>
    <tbody>
    {% for pr in by_product %}
    <tr>
      <td><span class="drill-arrow" onclick="toggleProduct('{{pr.product_type|e}}', this)">&#9654;</span></td>
      <td style="font-size:12px"><b>{{pr.product_type}}</b></td>
      <td>{{"{:,}".format(pr.total|int)}}</td>
      <td>{{pr.npl}}</td>
      <td><span class="badge {{'br' if pr.npl_rate>5 else 'by' if pr.npl_rate>3 else 'bg'}}">{{"{:.1f}".format(pr.npl_rate)}}%</span></td>
      <td style="font-size:11px;color:var(--muted)">{{"{:.1f}".format((pr.portfolio or 0)/1000000)}} млн</td>
    </tr>
    <tr class="drill-row" id="prod-{{loop.index}}"><td colspan="6">
      <div class="drill-content" id="prod-data-{{loop.index}}">Загрузка...</div>
    </td></tr>
    {% endfor %}
    </tbody>
  </table></div>
</div>

<div class="sec">
  <div class="sec-head"><h2>Портфель по продуктам и сегментам (фокус на долге ≥ {{"{:.0f}".format((debt_threshold or 4000000)/1000000)}} млн)</h2></div>
  <div class="tbl-wrap"><table>
    <thead><tr><th>Продукт</th><th>Сегмент</th><th>Клиентов</th><th>Договоров</th><th>Портфель</th><th>Долг всего</th><th>Долг ≥ X</th><th>Доля %</th><th>НПЛ%</th></tr></thead>
    <tbody>
    {% for ps in products_segments %}
    <tr>
      <td>{{ps.product_type}}</td><td>{{ps.client_segment}}</td>
      <td>{{"{:,}".format(ps.clients|int)}}</td><td>{{"{:,}".format(ps.contracts|int)}}</td>
      <td>{{"{:.1f}".format((ps.portfolio or 0)/1000000)}} млн</td>
      <td>{{"{:.1f}".format((ps.debt_total or 0)/1000000)}} млн</td>
      <td>{{"{:.1f}".format((ps.debt_above_x or 0)/1000000)}} млн</td>
      <td>{{ps.debt_above_x_pct}}%</td>
      <td><span class="badge {{'br' if (ps.npl_pct or 0)>15 else 'by' if (ps.npl_pct or 0)>8 else 'bg'}}">{{ps.npl_pct or 0}}%</span></td>
    </tr>
    {% endfor %}
    {% if not products_segments %}<tr><td colspan="9" style="color:var(--muted);text-align:center;padding:16px">Нет данных по выбранным фильтрам</td></tr>{% endif %}
    </tbody>
  </table></div>
</div>

<div class="sec">
  <div class="sec-head"><h2>Сегментация по региону и МФЙ (фокус на долге ≥ {{"{:.0f}".format((debt_threshold or 4000000)/1000000)}} млн)</h2></div>
  <div class="tbl-wrap"><table>
    <thead><tr><th>Регион</th><th>МФЙ</th><th>Клиентов</th><th>Договоров</th><th>Долг всего</th><th>Долг ≥ X</th><th>Ср. долг ≥ X</th></tr></thead>
    <tbody>
    {% for rm in regions_mfy %}
    <tr>
      <td>{{rm.region}}</td><td>{{rm.mfy}}</td>
      <td>{{"{:,}".format(rm.clients|int)}}</td><td>{{"{:,}".format(rm.contracts|int)}}</td>
      <td>{{"{:.1f}".format((rm.debt_total or 0)/1000000)}} млн</td>
      <td>{{"{:.1f}".format((rm.debt_above_x or 0)/1000000)}} млн</td>
      <td>{{"{:,.0f}".format(rm.avg_debt_above_x or 0)}}</td>
    </tr>
    {% endfor %}
    {% if not regions_mfy %}<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:16px">Нет данных по выбранным фильтрам</td></tr>{% endif %}
    </tbody>
  </table></div>
</div>

<script>
function toggleProduct(pt, arrow) {
  var row = arrow.parentElement.parentElement.nextElementSibling;
  if (!row) return;
  var dd = row.querySelector('.drill-content');
  if (row.classList.contains('open')) { row.classList.remove('open'); arrow.classList.remove('open'); return; }
  row.classList.add('open'); arrow.classList.add('open');
  if (dd) dd.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:6px">Загрузка...</div>';
  fetch('/api/product-detail/' + encodeURIComponent(pt), { credentials: 'same-origin' })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!dd) return;
      var h = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:12px">';
      h += '<div><b style="color:var(--muted)">По филиалам</b><table style="font-size:11px;width:100%;margin-top:4px"><thead><tr><th>Филиал</th><th>Дог.</th><th>НПЛ</th><th>Портфель</th></tr></thead><tbody>';
      (d.by_branch || []).forEach(function(b) {
        h += '<tr><td>' + b.branch + '</td><td>' + b.total + '</td><td>' + b.npl + '</td><td>' + (b.portfolio_mln || 0).toFixed(1) + ' млн</td></tr>';
      });
      h += '</tbody></table></div>';
      h += '<div><b style="color:var(--muted)">По статусам</b><table style="font-size:11px;width:100%;margin-top:4px"><thead><tr><th>Статус</th><th>Кол-во</th></tr></thead><tbody>';
      (d.by_status || []).forEach(function(s) {
        h += '<tr><td>' + s.status_detail + '</td><td>' + s.total + '</td></tr>';
      });
      h += '</tbody></table></div></div>';
      dd.innerHTML = h;
    })
    .catch(function() { if (dd) dd.innerHTML = '<div style="color:var(--red);font-size:12px">Ошибка загрузки</div>'; });
}
</script>
</div>
</div>

<style>
.drill-row{display:none}.drill-row.open{display:table-row}
.drill-content{padding:10px 14px;background:rgba(0,0,0,0.12)}
.drill-arrow{cursor:pointer;display:inline-block;transition:transform 0.2s;user-select:none;font-size:10px}
.drill-arrow.open{transform:rotate(90deg)}
.debt-filter-chip{display:inline-flex;align-items:center;gap:4px;padding:5px 12px;background:var(--card);border:1px solid var(--border);border-radius:8px;cursor:pointer;font-size:13px;transition:all 0.2s}
.debt-filter-chip:hover{background:var(--surface);border-color:var(--accent)}
.chip-arrow{font-size:10px;color:var(--muted)}
.debt-modal{position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center}
.debt-modal-inner{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;max-width:320px;max-height:80vh;overflow-y:auto}
.debt-modal-inner h4{margin-bottom:12px;font-size:14px}
.debt-modal-inner label{display:block;padding:6px 0;cursor:pointer;font-size:13px}
.debt-modal-inner input{margin-right:8px}
</style>
<script>
function applyFilter() {
  var y = document.getElementById('pfYear') && document.getElementById('pfYear').value;
  var b = document.getElementById('pfBranch') ? document.getElementById('pfBranch').value : '';
  var pr = document.getElementById('pfProduct') && document.getElementById('pfProduct').value;
  var q = new URLSearchParams();
  if (y) q.set('year', y); if (b) q.set('branch', b); if (pr) q.set('product', pr);
  var cbs = document.querySelectorAll('#debtFilterForm input[name=debt_filter]:checked');
  for (var i = 0; i < cbs.length; i++) q.append('debt_filter', cbs[i].value);
  window.location.href = '/portfolio?' + q.toString();
}
function toggleDebtModal() {
  var m = document.getElementById('debtModal');
  if (m) m.style.display = m.style.display === 'none' ? 'block' : 'none';
}
function applyDebtFilter() {
  toggleDebtModal();
  applyFilter();
}
function toggleBranch(branch, arrow) {
  var row = document.getElementById('pb-' + branch);
  var dd = document.getElementById('pb-data-' + branch);
  if (!row || !dd) return;
  if (row.classList.contains('open')) { row.classList.remove('open'); arrow.classList.remove('open'); return; }
  row.classList.add('open'); arrow.classList.add('open');
  fetch('/api/npl-branch-years/' + encodeURIComponent(branch)).then(r=>r.json()).then(function(data) {
    if (!data.length) { dd.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:8px">Нет данных</div>'; return; }
    var h = '<table style="font-size:12px;width:100%"><thead><tr><th style="width:28px"></th><th>Год</th><th>Дог.</th><th>НПЛ</th><th>НПЛ%</th></tr></thead><tbody>';
    data.forEach(function(r) {
      var total = r.total_contracts != null ? r.total_contracts : r.total;
      var npl = r.npl_count != null ? r.npl_count : r.npl;
      var nplRate = r.npl_rate != null ? r.npl_rate : 0;
      var c = nplRate > 5 ? '#ef4444' : nplRate > 3 ? '#eab308' : '#22c55e';
      var key = branch + '-' + r.year;
      h += '<tr><td><span class="drill-arrow" onclick="toggleYear(\\'' + key.replace(/'/g, "\\\\'") + '\\',\\'' + r.year + '\\',this)">▶</span></td><td><b>' + r.year + '</b></td><td>' + (total || 0).toLocaleString('ru') + '</td><td>' + npl + '</td><td style="color:' + c + ';font-weight:700">' + nplRate + '%</td></tr>';
      h += '<tr class="drill-row" id="py-' + key + '"><td colspan="5"><div class="drill-content" id="py-data-' + key + '">Загрузка...</div></td></tr>';
    });
    dd.innerHTML = h + '</tbody></table>';
  });
}
function toggleYear(key, year, arrow) {
  var row = document.getElementById('py-' + key);
  var dd = document.getElementById('py-data-' + key);
  if (!row || !dd) return;
  if (row.classList.contains('open')) { row.classList.remove('open'); arrow.classList.remove('open'); return; }
  row.classList.add('open'); arrow.classList.add('open');
  var branch = (key || '').split('-')[0] || '';
  var url = '/api/monthly/' + encodeURIComponent(year);
  if (branch) url += '?branch=' + encodeURIComponent(branch);
  fetch(url).then(r=>r.json()).then(function(data) {
    if (!data.length) { dd.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:8px">Нет данных</div>'; return; }
    var h = '<table style="font-size:12px;width:100%"><thead><tr><th style="width:28px"></th><th>Месяц</th><th>Дог.</th><th>НПЛ</th><th>НПЛ%</th><th>Портфель</th></tr></thead><tbody>';
    data.forEach(function(r) {
      var total = r.total_contracts != null ? r.total_contracts : r.total;
      var npl = r.npl_count != null ? r.npl_count : r.npl;
      var nplRate = r.npl_rate != null ? r.npl_rate : 0;
      var pm = r.portfolio_mln != null ? r.portfolio_mln : (r.portfolio_m || 0);
      var period = year + '-' + (r.month || '');
      var safeId = period.replace(/-/g, '_');
      var c = nplRate > 5 ? '#ef4444' : nplRate > 3 ? '#eab308' : '#22c55e';
      h += '<tr><td><span class="drill-arrow" data-branch=\"' + branch + '\" onclick="toggleMonth(\\'' + period + '\\',this)">▶</span></td><td>' + period + '</td><td>' + (total || 0).toLocaleString('ru') + '</td><td>' + npl + '</td><td style="color:' + c + ';font-weight:600">' + nplRate + '%</td><td style="font-size:11px;color:#94a3b8">' + pm + ' млн</td></tr>';
      h += '<tr class="drill-row" id="mo-' + safeId + '"><td colspan="6"><div class="drill-content" id="mo-data-' + safeId + '">Загрузка...</div></td></tr>';
    });
    dd.innerHTML = h + '</tbody></table>';
  });
}
function toggleMonth(period, arrow) {
  var safeId = period.replace(/-/g, '_');
  var row = document.getElementById('mo-' + safeId);
  var dd = document.getElementById('mo-data-' + safeId);
  if (!row || !dd) return;
  if (row.classList.contains('open')) { row.classList.remove('open'); arrow.classList.remove('open'); return; }
  row.classList.add('open'); arrow.classList.add('open');
  var branch = (arrow && arrow.getAttribute && arrow.getAttribute('data-branch')) || (document.getElementById('dashBranch') ? document.getElementById('dashBranch').value : '') || '';
  var url = '/api/month-detail/' + encodeURIComponent(period);
  if (branch) url += '?branch=' + encodeURIComponent(branch);
  fetch(url).then(r=>r.json()).then(function(d) {
    var h = '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px">';
    h += '<div><b style="font-size:11px;color:var(--muted)">ФИЛИАЛЫ</b><table style="font-size:11px;margin-top:6px"><thead><tr><th>Филиал</th><th>Дог.</th><th>НПЛ%</th></tr></thead><tbody>';
    (d.by_branch || []).forEach(function(b) { var tot = b.total_contracts != null ? b.total_contracts : b.total; var nr = tot ? (100 * (b.npl_count || 0) / tot).toFixed(1) : '0'; h += '<tr><td>' + (b.branch || '') + '</td><td>' + tot + '</td><td>' + nr + '%</td></tr>'; });
    h += '</tbody></table></div>';
    h += '<div><b style="font-size:11px;color:var(--muted)">СТАТУСЫ</b><table style="font-size:11px;margin-top:6px"><thead><tr><th>Статус</th><th>Кол-во</th></tr></thead><tbody>';
    (d.by_status || []).forEach(function(s) { var cnt = s.total_contracts != null ? s.total_contracts : s.cnt; h += '<tr><td>' + (s.status_detail || s.status || '') + '</td><td>' + cnt + '</td></tr>'; });
    h += '</tbody></table></div>';
    h += '<div><b style="font-size:11px;color:var(--muted)">СОТРУДНИКИ</b><table style="font-size:11px;margin-top:6px"><thead><tr><th>ФИО</th><th>Дог.</th></tr></thead><tbody>';
    (d.by_employee || []).forEach(function(e) { h += '<tr><td>' + (e.employee_name || '') + '</td><td>' + (e.total_contracts || 0) + '</td></tr>'; });
    h += '</tbody></table></div></div>';
    dd.innerHTML = h;
  });
}
function sortTable(id, col) {
  var tbl = document.getElementById(id); if (!tbl || !tbl.tBodies[0]) return;
  var tbody = tbl.tBodies[0];
  var rows = Array.from(tbody.rows).filter(function(r) { return !r.classList.contains('drill-row'); });
  var asc = tbl.dataset['sort' + col] !== 'asc';
  tbl.dataset['sort' + col] = asc ? 'asc' : 'desc';
  rows.sort(function(a, b) {
    var av = (a.cells[col] && a.cells[col].innerText) ? a.cells[col].innerText.replace(/[,%\\s]/g, '') : '';
    var bv = (b.cells[col] && b.cells[col].innerText) ? b.cells[col].innerText.replace(/[,%\\s]/g, '') : '';
    var an = parseFloat(av); var bn = parseFloat(bv);
    if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
    return asc ? (av + '').localeCompare(bv + '', 'ru') : (bv + '').localeCompare(av + '', 'ru');
  });
  rows.forEach(function(r) { tbody.appendChild(r); });
}
</script>
""" + F

ADMIN_T = """
<!DOCTYPE html><html><head><meta charset="utf-8"><title>Администрирование</title>{{ css|safe }}</head><body>
{{ nav|safe }}
<div class="ctr">
<div class="page-header"><h1>Администрирование</h1></div>

{% if msg %}
<div style="padding:10px 16px;background:rgba(34,197,94,0.1);border:1px solid rgba(34,197,94,0.3);border-radius:10px;margin-bottom:14px;color:#22c55e;font-size:14px">{{ msg }}</div>
{% endif %}
{% if error %}
<div class="err" style="margin-bottom:14px">{{ error }}</div>
{% endif %}

<div class="cards" style="margin-bottom:20px">
  <div class="card"><h3>БД размер</h3><div class="val" style="font-size:20px">{{ db_size_mb }} МБ</div></div>
  <div class="card"><h3>Клиентов</h3><div class="val">{% set v = table_counts.get('clients', 0) %}{% if v is number %}{{ "{:,}".format(v).replace(",", " ") }}{% else %}{{ v }}{% endif %}</div></div>
  <div class="card"><h3>Договоров</h3><div class="val">{% set v = table_counts.get('contracts', 0) %}{% if v is number %}{{ "{:,}".format(v).replace(",", " ") }}{% else %}{{ v }}{% endif %}</div></div>
  <div class="card"><h3>Скорингов</h3><div class="val">{% set v = table_counts.get('scoring_log', 0) %}{% if v is number %}{{ "{:,}".format(v).replace(",", " ") }}{% else %}{{ v }}{% endif %}</div></div>
  <div class="card"><h3>Аудит записей</h3><div class="val">{% set v = table_counts.get('audit_log', 0) %}{% if v is number %}{{ "{:,}".format(v).replace(",", " ") }}{% else %}{{ v }}{% endif %}</div></div>
</div>

<div class="sec" style="margin-bottom:20px">
  <div class="sec-head"><h2>Системные действия</h2></div>
  <div class="fl" style="gap:10px;flex-wrap:wrap">
    <button class="btn btn-o" onclick="sysAction('backup')">Бэкап БД</button>
    <button class="btn btn-o" onclick="sysAction('integrity')">Проверка целостности</button>
    <button class="btn btn-o" onclick="sysAction('refresh')">Обновить аналитику</button>
    <button class="btn btn-o" onclick="sysAction('dq_dry')">DQ Dry-Run merge</button>
    <button class="btn btn-o" onclick="sysAction('dq_apply')">DQ Apply merge</button>
    <button class="btn btn-o" onclick="sysAction('dq_nightly')">Запустить nightly DQ</button>
    <button class="btn btn-o" onclick="sysAction('ml')">Обучить ML</button>
    <a href="/export/contracts" class="btn btn-o">Экспорт договоров</a>
    <a href="/export/clients" class="btn btn-o">Экспорт клиентов</a>
  </div>
  <div id="sysResult" style="margin-top:10px;display:none;padding:10px 14px;background:rgba(59,130,246,0.1);border-radius:8px;font-size:13px"></div>
</div>

<div class="sec" style="margin-bottom:20px">
  <div class="sec-head"><h2>Качество данных</h2></div>
  <div id="dqInfo" style="font-size:13px;color:var(--muted)">Загрузка профиля данных...</div>
  <div id="dqNightlyInfo" style="font-size:12px;color:var(--muted);margin-top:6px"></div>
  <div id="dqReadinessTable" style="margin-top:10px"></div>
</div>

<div class="sec" style="margin-bottom:20px">
  <div class="sec-head" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap">
    <h2>Управление пользователями ({{ users|length }})</h2>
    <button class="btn btn-p btn-sm" onclick="showCreateUser()">+ Добавить</button>
  </div>
  <div id="createUserForm" style="display:none;margin-bottom:16px">
    <div class="card" style="border:1px solid var(--accent)">
      <h3 style="margin-bottom:12px">Новый пользователь</h3>
      <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <input type="hidden" name="action" value="create_user">
        <div class="g3">
          <div class="field"><label>Логин</label><input name="username" placeholder="employee_f1" required></div>
          <div class="field"><label>Пароль</label><input name="password" type="text" placeholder="минимум 6 символов" required></div>
          <div class="field"><label>Роль</label>
            <select name="role">{% for k,v in ROLES.items() %}<option value="{{ k }}">{{ v.label }}</option>{% endfor %}</select></div>
        </div>
        <div class="g2">
          <div class="field"><label>Филиал (для менеджера/сотрудника)</label>
            <select name="branch">
              <option value="">— Без привязки —</option>{% for k,v in BRANCHES.items() %}<option value="{{ k }}">{{ k }} — {{ v }}</option>{% endfor %}
            </select></div>
          <div style="display:flex;gap:8px;align-items:flex-end;padding-bottom:1px">
            <button class="btn btn-p" type="submit">Создать</button>
            <button class="btn btn-o" type="button" onclick="hideCreateUser()">Отмена</button>
          </div>
        </div>
      </form>
    </div>
  </div>
  <div class="tbl-wrap"><table>
    <thead><tr><th>ID</th><th>Логин</th><th>Роль</th><th>Филиал</th><th>Статус</th><th>Пароль</th><th>Роль/Филиал</th></tr></thead>
    <tbody>
    {% for usr in users %}
    <tr style="{{ 'opacity:0.5' if not usr.active else '' }}">
      <td style="color:var(--muted);font-size:11px">{{ usr.id }}</td>
      <td><b>{{ usr.username }}</b></td>
      <td style="font-size:12px">{{ (ROLES.get(usr.role) or {}).label or usr.role }}</td>
      <td style="font-size:12px">{{ BRANCHES.get(usr.branch, '—') if usr.branch else '—' }}</td>
      <td>
        <form method="POST" style="display:inline">
          <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
          <input type="hidden" name="action" value="toggle_user">
          <input type="hidden" name="uid" value="{{ usr.id }}">
          <input type="hidden" name="active" value="{{ '0' if usr.active else '1' }}">
          <button type="submit" class="btn btn-sm {{ 'btn-o' if usr.active else 'btn-p' }}" style="{{ 'color:var(--red)' if usr.active else '' }}">{{ 'Заблокировать' if usr.active else 'Активировать' }}</button>
        </form>
      </td>
      <td>
        <button class="btn btn-o btn-sm" onclick="showPassForm({{ usr.id }})">Сменить пароль</button>
        <div id="passForm-{{ usr.id }}" style="display:none;margin-top:6px">
          <form method="POST" style="display:flex;gap:6px">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <input type="hidden" name="action" value="change_password">
            <input type="hidden" name="uid" value="{{ usr.id }}">
            <input name="new_password" type="text" placeholder="Новый пароль" style="flex:1">
            <button class="btn btn-p btn-sm" type="submit">OK</button>
          </form>
        </div>
      </td>
      <td>
        <button class="btn btn-o btn-sm" onclick="showRoleForm({{ usr.id }})">Изменить</button>
        <div id="roleForm-{{ usr.id }}" style="display:none;margin-top:6px">
          <form method="POST" style="display:flex;gap:6px;flex-wrap:wrap">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <input type="hidden" name="action" value="change_role">
            <input type="hidden" name="uid" value="{{ usr.id }}">
            <select name="new_role" style="flex:1">{% for k,v in ROLES.items() %}<option value="{{ k }}" {{ 'selected' if k==usr.role else '' }}>{{ v.label }}</option>{% endfor %}</select>
            <select name="new_branch" style="flex:1">
              <option value="">Без филиала</option>{% for k,v in BRANCHES.items() %}<option value="{{ k }}" {{ 'selected' if k==usr.branch else '' }}>{{ k }}</option>{% endfor %}
            </select>
            <button class="btn btn-p btn-sm" type="submit">OK</button>
          </form>
        </div>
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table></div>
</div>

<div class="sec">
  <div class="sec-head"><h2>Лог аудита</h2></div>
  <form method="GET" style="margin-bottom:12px;display:flex;gap:10px">
    <input name="audit_q" value="{{ audit_query }}" placeholder="Поиск: пользователь, действие, детали" style="flex:1">
    <button class="btn btn-p btn-sm" type="submit">Поиск</button>
    {% if audit_query %}<a href="/admin" class="btn btn-o btn-sm">Сбросить</a>{% endif %}
  </form>
  <div class="tbl-wrap"><table>
    <thead><tr><th>Дата</th><th>Пользователь</th><th>Действие</th><th>Объект</th><th>Детали</th></tr></thead>
    <tbody>
    {% for row in audit_rows %}
    <tr>
      <td style="font-size:11px;color:var(--muted);white-space:nowrap">{{ (row.created_at or '')[:16] }}</td>
      <td><b>{{ row.user }}</b></td>
      <td style="font-size:12px">{{ row.action }}</td>
      <td style="font-size:12px">{{ row.entity or '—' }}{% if row.entity_id %} <span style="color:var(--muted)">#{{ row.entity_id }}</span>{% endif %}</td>
      <td style="font-size:11px;color:var(--muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ row.details or '—' }}</td>
    </tr>
    {% else %}
    <tr><td colspan="5" style="text-align:center;color:var(--muted);padding:20px">Нет записей</td></tr>
    {% endfor %}
    </tbody>
  </table></div>
  {% if audit_rows|length == 100 %}<div style="font-size:12px;color:var(--muted);margin-top:6px">Показаны последние 100 записей</div>{% endif %}
</div>
</div>

<script>
function showCreateUser() { document.getElementById('createUserForm').style.display = ''; }
function hideCreateUser() { document.getElementById('createUserForm').style.display = 'none'; }
function showPassForm(uid) { var el = document.getElementById('passForm-' + uid); el.style.display = el.style.display === 'none' ? '' : 'none'; }
function showRoleForm(uid) { var el = document.getElementById('roleForm-' + uid); el.style.display = el.style.display === 'none' ? '' : 'none'; }
function sysAction(type) {
  var res = document.getElementById('sysResult');
  res.style.display = '';
  res.innerText = 'Выполняется...';
  var actions = {
    'backup': ['/api/backup', 'POST'],
    'integrity': ['/api/integrity-check', 'GET'],
    'refresh': ['/api/refresh-analytics', 'POST'],
    'dq_dry': ['/api/data-quality/optimize?passport_mode=dry_run', 'POST'],
    'dq_apply': ['/api/data-quality/optimize?passport_mode=apply', 'POST'],
    'dq_nightly': ['/api/data-quality/nightly/run-now', 'POST'],
    'ml': ['/api/train-ml', 'POST']
  };
  var url = (actions[type] && actions[type][0]) || '/api/refresh-analytics';
  var method = (actions[type] && actions[type][1]) || 'POST';
  fetch(url, { method: method, credentials: 'same-origin' }).then(function(r) {
    return r.text().then(function(text) { return { status: r.status, text: text }; });
  }).then(function(o) {
    var text = o.text;
    if (o.status === 401) {
      try {
        var d = JSON.parse(text);
        res.innerText = d.message || 'Сессия истекла. Обновите страницу и войдите снова.';
      } catch (e) {
        res.innerText = 'Сессия истекла. Обновите страницу и войдите снова.';
      }
      res.style.background = 'rgba(239,68,68,0.1)';
      return;
    }
    try {
      var d = JSON.parse(text);
      res.innerText = d.message || d.summary || (d.error || JSON.stringify(d));
      res.style.background = d.error ? 'rgba(239,68,68,0.1)' : 'rgba(34,197,94,0.1)';
      if (!d.error && (type === 'dq_dry' || type === 'dq_apply' || type === 'dq_nightly')) {
        loadDataQualityProfile();
      }
    } catch (e) {
      if (text && text.toLowerCase().indexOf('<!doctype') !== -1) {
        res.innerText = 'Ошибка: сервер вернул HTML вместо JSON (возможно, требуется повторный вход). Обновите страницу и войдите снова.';
      } else {
        res.innerText = 'Ошибка: ответ не JSON. ' + String(text).slice(0, 300);
      }
      res.style.background = 'rgba(239,68,68,0.1)';
    }
  }).catch(function(e) {
    res.innerText = 'Ошибка сети: ' + (e && e.message ? e.message : String(e));
    res.style.background = 'rgba(239,68,68,0.1)';
  });
}
function loadDataQualityProfile() {
  var el = document.getElementById('dqInfo');
  var readinessEl = document.getElementById('dqReadinessTable');
  var nightlyEl = document.getElementById('dqNightlyInfo');
  if (!el) return;
  fetch('/api/data-quality/profile', { credentials: 'same-origin' })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var c = d.completeness || {};
      el.innerHTML =
        'Клиенты: <b>' + (d.clients_total || 0).toLocaleString('ru-RU') + '</b> · ' +
        'Договоры: <b>' + (d.contracts_total || 0).toLocaleString('ru-RU') + '</b> · ' +
        'Дубли клиентов по external_id: <b>' + (d.duplicate_client_external_groups || 0) + '</b> · ' +
        'Дубли договоров по external_id+branch: <b>' + (d.duplicate_contract_external_branch_groups || 0) + '</b><br>' +
        'Заполненность: возраст <b>' + (c.age_fill_rate_pct || 0) + '%</b>, регион <b>' + (c.region_fill_rate_pct || 0) + '%</b>, доход <b>' + (c.income_fill_rate_pct || 0) + '%</b>.';
      fetch('/api/data-quality/reference', { credentials: 'same-origin' })
        .then(function(r2) { return r2.json(); })
        .then(function(ref) {
          if (!ref || !ref.available) return;
          el.innerHTML += '<br>Референс-пакет: строк <b>' + (ref.reference_rows || 0).toLocaleString('ru-RU') +
            '</b>, уникальных клиентов <b>' + (ref.reference_unique_clients || 0).toLocaleString('ru-RU') +
            '</b>, NPL <b>' + (ref.reference_npl_rate_pct || 0) + '%</b>.';
        })
        .catch(function(){});
    })
    .catch(function() {
      el.innerText = 'Не удалось загрузить профиль качества данных';
    });
  fetch('/api/data-quality/nightly/status', { credentials: 'same-origin' })
    .then(function(r) { return r.json(); })
    .then(function(st) {
      if (!nightlyEl) return;
      nightlyEl.innerHTML = 'Nightly DQ: статус <b>' + (st.last_status || 'never') +
        '</b>, последний запуск: <b>' + (st.last_run_at || '—') +
        '</b>, длительность: <b>' + (st.last_duration_sec != null ? st.last_duration_sec + 's' : '—') + '</b>.';
    })
    .catch(function(){});
  fetch('/api/data-quality/readiness', { credentials: 'same-origin' })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!readinessEl) return;
      var items = (d && d.items) || [];
      if (!items.length) { readinessEl.innerHTML = '<div style="font-size:12px;color:var(--muted)">Нет данных readiness</div>'; return; }
      var h = '<table style="font-size:12px;width:100%"><thead><tr><th>Филиал</th><th>Score</th><th>Статус</th><th>Причины</th></tr></thead><tbody>';
      items.forEach(function(it){
        h += '<tr><td>' + (it.branch || '—') + '</td><td><b>' + (it.score || 0) + '</b></td><td>' + (it.status || '—') + '</td><td>' + ((it.top_reasons || []).join(', ')) + '</td></tr>';
      });
      readinessEl.innerHTML = h + '</tbody></table>';
    })
    .catch(function(){});
}
loadDataQualityProfile();
</script>
</body></html>
"""

INTELLIGENCE_T = H('Аналитика') + """<div class="ctr">
<div class="page-header">
  <h1>Credit Intelligence — центр принятия решений</h1>
  <p style="font-size:13px;color:var(--muted);margin-top:6px">Где теряются деньги &middot; Где растёт риск &middot; Где точка роста</p>
  <div class="fl fw" style="gap:8px;margin-top:12px">
    <button class="btn btn-p btn-sm" onclick="refreshIntel()">Обновить</button>
    <a href="/export/portfolio" class="btn btn-o btn-sm">Экспорт</a>
    <a href="/finance" class="btn btn-o btn-sm">OPEX / Финансы</a>
    {% if user.role in ('director','head_analyst') %}
    <button class="btn btn-o btn-sm" onclick="trainML()">Обучить ML</button>
    <button class="btn btn-o btn-sm" onclick="checkIntegrity()">Целостность</button>
    <button class="btn btn-o btn-sm" onclick="runBackup()">Backup</button>
    {% endif %}
  </div>
</div>

<div class="sec" style="margin-bottom:24px">
  <div class="sec-head"><h2>📑 Структура аналитики</h2></div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px">
    <a href="/" class="card" style="text-decoration:none;color:inherit;display:block">
      <h3 style="margin-bottom:6px">1. Executive Dashboard</h3>
      <p style="font-size:12px;color:var(--muted);margin-bottom:10px">KPI: портфель, выдача, погашение, NPL%, просрочка 30+/60+/90+, сбор%</p>
      <span class="btn btn-o btn-sm">→ Дашборд</span>
    </a>
    <a href="/portfolio" class="card" style="text-decoration:none;color:inherit;display:block">
      <h3 style="margin-bottom:6px">2. Анализ портфеля</h3>
      <p style="font-size:12px;color:var(--muted);margin-bottom:10px">По филиалам, продуктам, годам/месяцам, детализация, Сбор%</p>
      <span class="btn btn-o btn-sm">→ Портфель</span>
    </a>
    <a href="/portfolio" class="card" style="text-decoration:none;color:inherit;display:block">
      <h3 style="margin-bottom:6px">3. NPL и просрочка</h3>
      <p style="font-size:12px;color:var(--muted);margin-bottom:10px">Aging, NPL по сегментам. Ниже на странице: vintage-когорты</p>
      <span class="btn btn-o btn-sm">→ Портфель + ниже</span>
    </a>
    <a href="/portfolio" class="card" style="text-decoration:none;color:inherit;display:block">
      <h3 style="margin-bottom:6px">4. Аналитика филиалов</h3>
      <p style="font-size:12px;color:var(--muted);margin-bottom:10px">Таблица: филиал, портфель, NPL, сбор%, drill-down по годам/месяцам</p>
      <span class="btn btn-o btn-sm">→ Портфель</span>
    </a>
    <a href="/" class="card" style="text-decoration:none;color:inherit;display:block">
      <h3 style="margin-bottom:6px">5. Аналитика сотрудников</h3>
      <p style="font-size:12px;color:var(--muted);margin-bottom:10px">Топ по НПЛ, договорам — на дашборде; риски в алертах</p>
      <span class="btn btn-o btn-sm">→ Дашборд</span>
    </a>
    <div class="card" style="border-style:dashed">
      <h3 style="margin-bottom:6px">6. Сегменты клиентов</h3>
      <p style="font-size:12px;color:var(--muted);margin-bottom:10px">Риск по возрасту — ниже на странице (риск-матрица)</p>
      <span class="btn btn-o btn-sm" onclick="document.getElementById('risk-matrix-card').scrollIntoView({behavior:'smooth'})">↓ На этой странице</span>
    </div>
    <div class="card" style="border-style:dashed">
      <h3 style="margin-bottom:6px">7. Канальная аналитика</h3>
      <p style="font-size:12px;color:var(--muted);margin-bottom:10px">По ответственным лицам: выдача, NPL, средний чек. Ниже на странице.</p>
      <span class="btn btn-o btn-sm" onclick="document.querySelector('.stit em')&&document.querySelector('[id=chBranch]').parentElement.scrollIntoView({behavior:'smooth'})">На этой странице</span>
    </div>
    <a href="/finance" class="card" style="text-decoration:none;color:inherit;display:block">
      <h3 style="margin-bottom:6px">8. Финансовая аналитика / OPEX</h3>
      <p style="font-size:12px;color:var(--muted);margin-bottom:10px">Операционные расходы по филиалам, категориям, тренды, OPEX/договор</p>
      <span class="btn btn-o btn-sm">OPEX / Финансы</span>
    </a>
    <a href="/static/dashboards/product_analytics_dashboard.html" target="_blank" rel="noopener" class="card" style="text-decoration:none;color:inherit;display:block;border-left:4px solid var(--accent2)">
      <h3 style="margin-bottom:6px">9. Анализ по продукту</h3>
      <p style="font-size:12px;color:var(--muted);margin-bottom:10px">Category Management Dashboard: продажи, маржа, ABC/XYZ, топ-товары, поставщики</p>
      <span class="btn btn-o btn-sm">→ Открыть дашборд</span>
    </a>
    <div class="card" style="border-style:dashed">
      <h3 style="margin-bottom:6px">10. Аналитика продаж</h3>
      <p style="font-size:12px;color:var(--muted);margin-bottom:10px">Повторные клиенты, распределение по кол-ву договоров. Ниже на странице.</p>
      <span class="btn btn-o btn-sm" onclick="document.querySelectorAll('.stit')[2]&&document.querySelectorAll('.stit')[2].scrollIntoView({behavior:'smooth'})">На этой странице</span>
    </div>
    <a href="/alerts" class="card" style="text-decoration:none;color:inherit;display:block">
      <h3 style="margin-bottom:6px">11. Risk & Early Warning</h3>
      <p style="font-size:12px;color:var(--muted);margin-bottom:10px">Рост просрочки, риски филиалов/менеджеров; ниже — инсайты и хит-лист</p>
      <span class="btn btn-o btn-sm">→ Алерты</span>
    </a>
    <div class="card">
      <h3 style="margin-bottom:6px">12. AI Predictions</h3>
      <p style="font-size:12px;color:var(--muted);margin-bottom:10px">Прогноз просрочки, EBITDA, риск дефолта. Обучите ML-модель в разделе Админ → «Обучить ML» — после обучения здесь появятся прогнозы на основе <code>ml_engine</code>.</p>
      <a href="/admin" class="btn btn-o btn-sm">→ Админ</a>
    </div>
    <div class="card" style="border-left:4px solid var(--accent)">
      <h3 style="margin-bottom:6px">13. Decision Center</h3>
      <p style="font-size:12px;color:var(--muted);margin-bottom:10px">Рекомендации системе — внизу страницы</p>
      <span class="btn btn-o btn-sm" onclick="document.getElementById('recommendations-block').scrollIntoView({behavior:'smooth'})">↓ Рекомендации</span>
    </div>
  </div>
</div>

<p style="font-size:12px;color:var(--muted);margin-bottom:8px">Год и месяц влияют на когорты, vintage и риск-матрицу по возрасту. Филиал — на все блоки.</p>
<form method="get" action="/intelligence" style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:16px;padding:12px;background:var(--card);border-radius:10px;border:1px solid var(--border)">
  <span style="font-size:13px;font-weight:600">Фильтры:</span>
  <label style="display:flex;align-items:center;gap:6px;font-size:13px">
    Филиал
    <select name="branch" onchange="this.form.submit()" style="padding:6px 10px;border-radius:6px;border:1px solid var(--border);background:var(--bg)">
      <option value="all" {{ 'selected' if filter_branch == 'all' else '' }}>Все</option>
      {% for br in branches_for_filter %}
      <option value="{{ br }}" {{ 'selected' if filter_branch == br else '' }}>{{ br }}</option>
      {% endfor %}
    </select>
  </label>
  <label style="display:flex;align-items:center;gap:6px;font-size:13px">
    Год
    <select name="year" onchange="this.form.submit()" style="padding:6px 10px;border-radius:6px;border:1px solid var(--border);background:var(--bg)">
      <option value="">—</option>
      {% for y in years_for_filter %}
      <option value="{{ y }}" {{ 'selected' if filter_year == y else '' }}>{{ y }}</option>
      {% endfor %}
    </select>
  </label>
  <label style="display:flex;align-items:center;gap:6px;font-size:13px">
    Месяц
    <select name="month" onchange="this.form.submit()" style="padding:6px 10px;border-radius:6px;border:1px solid var(--border);background:var(--bg)">
      <option value="">—</option>
      <option value="01" {{ 'selected' if filter_month == '01' else '' }}>01</option>
      <option value="02" {{ 'selected' if filter_month == '02' else '' }}>02</option>
      <option value="03" {{ 'selected' if filter_month == '03' else '' }}>03</option>
      <option value="04" {{ 'selected' if filter_month == '04' else '' }}>04</option>
      <option value="05" {{ 'selected' if filter_month == '05' else '' }}>05</option>
      <option value="06" {{ 'selected' if filter_month == '06' else '' }}>06</option>
      <option value="07" {{ 'selected' if filter_month == '07' else '' }}>07</option>
      <option value="08" {{ 'selected' if filter_month == '08' else '' }}>08</option>
      <option value="09" {{ 'selected' if filter_month == '09' else '' }}>09</option>
      <option value="10" {{ 'selected' if filter_month == '10' else '' }}>10</option>
      <option value="11" {{ 'selected' if filter_month == '11' else '' }}>11</option>
      <option value="12" {{ 'selected' if filter_month == '12' else '' }}>12</option>
    </select>
  </label>
  <button type="submit" class="btn btn-p btn-sm">Применить</button>
</form>

<div class="card" style="margin-bottom:12px">
  <div style="font-size:12px;color:var(--muted)">Data Readiness (для скоринга и аналитики)</div>
  <div id="intel-readiness" style="font-size:13px;margin-top:4px">Загрузка...</div>
</div>
<script>
(function(){
  var branch = '{{ filter_branch or '' }}';
  var q = (branch && branch !== 'all') ? ('?branch=' + encodeURIComponent(branch)) : '';
  fetch('/api/data-quality/readiness' + q, { credentials: 'same-origin' })
    .then(function(r){ return r.ok ? r.json() : null; })
    .then(function(d){
      var el = document.getElementById('intel-readiness');
      if (!el || !d) return;
      var item = (d.items && d.items.length) ? d.items[0] : null;
      if (!item) { el.innerText = 'Нет данных readiness'; return; }
      el.innerHTML = 'Score: <b>' + item.score + '</b> · статус: <b>' + item.status + '</b> · причины: ' + ((item.top_reasons || []).join(', '));
    })
    .catch(function(){ var el = document.getElementById('intel-readiness'); if (el) el.innerText = 'Не удалось загрузить readiness'; });
})();
</script>

<div class="stit"><em>Executive KPI</em></div>
<div class="kpi-grid" id="executive-kpi">
  <a href="/" class="kpi-card kpi-accent-gold" style="text-decoration:none;color:inherit"><div class="kv" id="kpi-portfolio">—</div><div class="kl">Портфель</div></a>
  <a href="/" class="kpi-card kpi-accent-blue" style="text-decoration:none;color:inherit"><div class="kv" id="kpi-issued">—</div><div class="kl">Выдано за мес.</div></a>
  <a href="/" class="kpi-card kpi-accent-green" style="text-decoration:none;color:inherit"><div class="kv" id="kpi-collected">—</div><div class="kl">Погашено за мес.</div></a>
  <div class="kpi-card kpi-accent-orange"><div class="kv" id="kpi-debt">—</div><div class="kl">Остаток долга</div></div>
  <a href="/portfolio" class="kpi-card kpi-accent-red" style="text-decoration:none;color:inherit"><div class="kv" id="kpi-npl-pct">—</div><div class="kl">NPL %</div></a>
  <div class="kpi-card kpi-accent-orange"><div class="kv" id="kpi-dpd30">—</div><div class="kl">Просрочка 30+</div></div>
  <div class="kpi-card kpi-accent-red"><div class="kv" id="kpi-dpd60">—</div><div class="kl">Просрочка 60+</div></div>
  <div class="kpi-card kpi-accent-red"><div class="kv" id="kpi-dpd90">—</div><div class="kl">Просрочка 90+</div></div>
  <div class="kpi-card kpi-accent-green"><div class="kv" id="kpi-repay">—</div><div class="kl">Сбор %</div></div>
  <div class="kpi-card kpi-accent-blue"><div class="kv" id="kpi-contracts">—</div><div class="kl">Договоров</div></div>
  <div class="kpi-card kpi-accent-purple"><div class="kv" id="kpi-clients">—</div><div class="kl">Клиентов</div></div>
</div>
<script>
(function(){
  var branch = '{{ filter_branch or "" }}';
  var url = '/api/analytics/kpi' + (branch && branch !== 'all' ? '?branch=' + encodeURIComponent(branch) : '');
  fetch(url, { credentials: 'same-origin' }).then(function(r){return r.ok?r.json():null}).then(function(d){
    if(!d)return;
    function N(x){return x==null?'—':Number(x).toLocaleString('ru-RU',{maximumFractionDigits:0})}
    function M(x){return x==null?'—':(Number(x)/1e6).toFixed(1)+' млн'}
    function P(x){return x==null?'—':Number(x)+'%'}
    document.getElementById('kpi-portfolio').textContent=M(d.total_portfolio);
    document.getElementById('kpi-issued').textContent=M(d.issued_this_month);
    document.getElementById('kpi-collected').textContent=M(d.collected_this_month);
    document.getElementById('kpi-debt').textContent=M(d.remaining_debt);
    document.getElementById('kpi-npl-pct').textContent=P(d.npl_rate_pct);
    document.getElementById('kpi-dpd30').textContent=P(d.dpd_30_pct);
    document.getElementById('kpi-dpd60').textContent=P(d.dpd_60_pct);
    document.getElementById('kpi-dpd90').textContent=P(d.dpd_90_pct);
    document.getElementById('kpi-repay').textContent=P(d.portfolio_repayment_rate);
    document.getElementById('kpi-contracts').textContent=N(d.number_of_contracts);
    document.getElementById('kpi-clients').textContent=N(d.active_clients);
  });
})();
</script>

{% if insights %}
<div style="display:flex;flex-direction:column;gap:10px;margin-bottom:20px">
  {% for ins in insights %}
  {% set border = {'critical':'#ef4444','warning':'#eab308','info':'#3b82f6','positive':'#22c55e'}.get(ins.type,'#64748b') %}
  <div style="display:flex;align-items:flex-start;gap:12px;padding:14px 16px;
    background:rgba(0,0,0,0.15);border-left:4px solid {{border}};border-radius:0 10px 10px 0">
    <span style="font-size:20px">{{ins.icon}}</span>
    <div>
      <div style="font-weight:700;font-size:14px;margin-bottom:3px">{{ins.title}}</div>
      <div style="font-size:13px;color:var(--muted)">{{ins.text}}</div>
    </div>
  </div>
  {% endfor %}
</div>
{% else %}
<div class="card" style="color:var(--muted);text-align:center;padding:20px;margin-bottom:20px">
  Нет автоматических инсайтов. Нажмите «Обновить» для пересчёта.
</div>
{% endif %}

<div class="g2" style="margin-bottom:20px">
  <div class="card">
    <div class="sec-head"><h3>🎯 Хит-лист: риск дефолта</h3></div>
    <div style="display:flex;gap:16px;margin-bottom:12px">
      <div style="text-align:center">
        <div style="font-size:24px;font-weight:800">{{hitlist|length}}</div>
        <div style="font-size:11px;color:var(--muted)">Анализировано</div>
      </div>
      <div style="text-align:center">
        <div style="font-size:24px;font-weight:800;color:var(--red)">{{hitlist|selectattr('delay_days','gt',20)|list|length}}</div>
        <div style="font-size:11px;color:var(--muted)">Высокий риск</div>
      </div>
    </div>
    {% if hitlist %}
    <div style="max-height:220px;overflow-y:auto">
    {% for r in hitlist %}
    <div style="display:flex;justify-content:space-between;align-items:center;
      padding:7px 0;border-bottom:1px solid var(--border);font-size:12px">
      <div>
        <div style="font-weight:600">{{r.full_name}}</div>
        <div style="color:var(--muted)">{{r.branch}} · {{r.employee_name or '—'}}</div>
      </div>
      <div style="text-align:right">
        <span class="badge {{'badge-critical' if (r.delay_days or 0)>25 else 'badge-high' if (r.delay_days or 0)>20 else 'badge-moderate'}}">{{r.delay_days or 0}} дн.</span>
        <form method="POST" action="/scoring" style="display:inline;margin-left:4px">
          <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
          <input type="hidden" name="action" value="score_existing">
          <input type="hidden" name="client_id" value="{{r.client_id}}">
          <button class="btn btn-p" style="padding:2px 8px;font-size:11px" type="submit">Скоринг</button>
        </form>
      </div>
    </div>
    {% endfor %}
    </div>
    {% else %}
    <div style="color:var(--muted);font-size:13px;text-align:center;padding:16px">Клиентов в зоне риска нет</div>
    {% endif %}
  </div>

  <div class="card" id="risk-matrix-card">
    <div class="sec-head"><h3>Риск-матрица по возрасту</h3></div>
    {% if risk_matrix_error %}
    <p style="color:var(--red);font-size:13px;margin-bottom:10px">Ошибка: {{ risk_matrix_error }}</p>
    {% endif %}
    <table style="font-size:13px;width:100%">
      <thead><tr><th>Сегмент</th><th>Договоров</th><th>НПЛ</th><th>PD%</th><th style="width:80px"></th></tr></thead>
      <tbody>
      {% for r in risk_matrix %}
      {% set color = 'var(--red)' if (r.pd or 0) > 8 else 'var(--yellow)' if (r.pd or 0) > 4 else 'var(--green)' %}
      {% set bclass = 'barf-red' if (r.pd or 0) > 8 else 'barf-yellow' if (r.pd or 0) > 4 else 'barf-green' %}
      <tr>
        <td>{{r.segment}}</td>
        <td class="num">{{"{:,}".format(r.total)}}</td>
        <td class="num">{{r.npl}}</td>
        <td style="font-weight:700;color:{{color}}" class="num">{{r.pd}}%</td>
        <td><div class="bar"><div class="barf {{bclass}}" style="width:{{[r.pd, 100]|min}}%"></div></div></td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
    <div class="chart-container chart-xs" style="margin-top:12px"><canvas id="chRiskAge"></canvas></div>
    <script>
    (function(){
      var labels = [{% for r in risk_matrix %}'{{r.segment}}',{% endfor %}];
      var totals = [{% for r in risk_matrix %}{{r.total}},{% endfor %}];
      var npls = [{% for r in risk_matrix %}{{r.npl}},{% endfor %}];
      if(typeof Chart!=='undefined'&&document.getElementById('chRiskAge')){
        new Chart(document.getElementById('chRiskAge'),{type:'bar',data:{labels:labels,datasets:[
          {label:'Всего',data:totals,backgroundColor:'rgba(59,130,246,.5)',borderRadius:4,borderSkipped:false},
          {label:'NPL',data:npls,backgroundColor:'rgba(239,68,68,.7)',borderRadius:4,borderSkipped:false}
        ]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#94a3b8',boxWidth:8,font:{size:10}}}},scales:{x:{ticks:{color:'#94a3b8',font:{size:9}}},y:{ticks:{color:'#94a3b8',font:{size:9}},grid:{color:'rgba(51,65,85,.5)'}}}}});
      }
    })();
    </script>
  </div>

  <div class="card">
    <div class="sec-head"><h3>Тренд НПЛ (12 мес)</h3></div>
    <div class="chart-container chart-sm"><canvas id="chNplTrend"></canvas></div>
    <script>
    (function(){
      var vals = [{% for v in npl_values %}{{v}},{% endfor %}];
      if(typeof Chart==='undefined'||!document.getElementById('chNplTrend')||!vals.length)return;
      var labels = vals.map(function(_,i){return 'M'+(i+1)});
      var colors = vals.map(function(v,i){return i>0&&(v-vals[i-1])>2?'#ef4444':'#3b82f6'});
      new Chart(document.getElementById('chNplTrend'),{type:'line',data:{labels:labels,datasets:[{
        label:'NPL %',data:vals,borderColor:'#3b82f6',backgroundColor:'rgba(59,130,246,.12)',fill:true,tension:.35,pointRadius:vals.map(function(v,i){return i>0&&(v-vals[i-1])>2?5:2}),pointBackgroundColor:colors
      }]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{backgroundColor:'#0f172a',borderColor:'#334155',padding:8,cornerRadius:5}},scales:{x:{ticks:{color:'#94a3b8',font:{size:9}}},y:{ticks:{color:'#94a3b8',font:{size:9}},grid:{color:'rgba(51,65,85,.5)'}}}}});
    })();
    </script>
  </div>

  <div class="card" id="recommendations-block">
    <div class="sec-head"><h3>Decision Center — рекомендации</h3></div>
    {% if recommendations %}
    {% for rec in recommendations %}
    {% set badge_cls = {'high':'badge-critical','medium':'badge-high','low':'badge-low'}.get(rec.priority,'badge-moderate') %}
    {% set col = {'high':'var(--red)','medium':'var(--yellow)','low':'var(--green)'}.get(rec.priority,'var(--muted)') %}
    <div style="padding:9px 0;border-bottom:1px solid var(--border);font-size:13px;border-left:3px solid {{col}};padding-left:10px;margin-bottom:6px;display:flex;gap:8px;align-items:flex-start">
      <span class="badge {{badge_cls}}" style="white-space:nowrap">{{rec.priority or 'info'}}</span>
      <span>{{rec.text}}</span>
    </div>
    {% endfor %}
    {% else %}
    <div style="color:var(--muted);font-size:13px">Нет рекомендаций. Нажмите &laquo;Обновить&raquo;.</div>
    {% endif %}
  </div>
</div>

<div class="sec" style="margin-bottom:20px">
  <div class="sec-head"><h2>📋 Под угрозой сейчас (просрочка 1–24 дней)</h2></div>
  {% if at_risk %}
  <div class="tbl-wrap"><table>
    <thead><tr><th>ФИО</th><th>Филиал</th><th>Сотрудник</th>
      <th>Дней просрочки</th><th>Долг</th><th>До МИБ</th><th></th></tr></thead>
    <tbody>
    {% for r in at_risk %}
    <tr>
      <td><b>{{r.full_name}}</b></td>
      <td>{{r.branch}}</td>
      <td style="font-size:12px">{{r.employee_name or '—'}}</td>
      <td><span class="badge {{'by' if (r.delay_days or 0) > 15 else ''}}">{{r.delay_days or 0}} дн.</span></td>
      <td style="font-size:12px">{{"{:,.0f}".format(r.total_debt or 0)}} сум</td>
      <td style="font-size:12px;color:var(--red)">{{25 - (r.delay_days or 0)}} дн.</td>
      <td>
        <form method="POST" action="/scoring" style="display:inline">
          <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
          <input type="hidden" name="action" value="score_existing">
          <input type="hidden" name="client_id" value="{{r.client_id}}">
          <button class="btn btn-p btn-sm" type="submit">Скоринг</button>
        </form>
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table></div>
  {% else %}
  <div style="padding:20px;text-align:center;color:var(--muted)">Нет договоров с просрочкой 1–24 дней</div>
  {% endif %}
</div>

<div class="sec">
  <div class="sec-head" onclick="toggleVintage()" style="cursor:pointer">
    <h2>📊 Vintage-когортный анализ <span id="vintageArrow">▶</span></h2>
  </div>
  <div id="vintageTable" style="display:none">
  {% if vintage %}
  <div class="tbl-wrap"><table>
    <thead><tr><th>Когорта</th><th>Всего</th><th>DPD30%</th><th>DPD60%</th><th>DPD90%</th><th>НПЛ%</th></tr></thead>
    <tbody>
    {% for v in vintage %}
    <tr>
      <td><b>{{v.cohort}}</b></td>
      <td>{{"{:,}".format(v.total)}}</td>
      <td style="font-weight:{{'700' if (v.dpd30 or 0) > 20 else '400'}};color:{{'var(--red)' if (v.dpd30 or 0) > 30 else 'var(--yellow)' if (v.dpd30 or 0) > 20 else 'var(--green)'}}">{{v.dpd30}}%</td>
      <td style="font-weight:{{'700' if (v.dpd60 or 0) > 15 else '400'}};color:{{'var(--red)' if (v.dpd60 or 0) > 22 else 'var(--yellow)' if (v.dpd60 or 0) > 15 else 'var(--green)'}}">{{v.dpd60}}%</td>
      <td style="font-weight:{{'700' if (v.dpd90 or 0) > 10 else '400'}};color:{{'var(--red)' if (v.dpd90 or 0) > 15 else 'var(--yellow)' if (v.dpd90 or 0) > 10 else 'var(--green)'}}">{{v.dpd90}}%</td>
      <td style="font-weight:{{'700' if (v.npl_pct or 0) > 3 else '400'}};color:{{'var(--red)' if (v.npl_pct or 0) > 4.5 else 'var(--yellow)' if (v.npl_pct or 0) > 3 else 'var(--green)'}}">{{v.npl_pct}}%</td>
    </tr>
    {% endfor %}
    </tbody>
  </table></div>
  {% endif %}
  </div>
</div>

<div class="stit"><em>Chart.js</em> Графики портфеля</div>
<div class="g3" style="margin-bottom:20px">
  <div class="card"><div class="sec-head"><h3>Портфель по филиалам (млн)</h3></div><div class="chart-container"><canvas id="chBranch"></canvas></div></div>
  <div class="card"><div class="sec-head"><h3>Структура по срокам</h3></div><div class="chart-container"><canvas id="chTerm"></canvas></div></div>
  <div class="card"><div class="sec-head"><h3>Тренд выдач / погашений (млн)</h3></div><div class="chart-container"><canvas id="chTrend"></canvas></div></div>
</div>
<script>
(function(){
  var TT={backgroundColor:'#0f172a',borderColor:'#334155',padding:8,cornerRadius:5};
  var branch='{{ filter_branch or "" }}';
  var bq=(branch&&branch!=='all')?'?branch='+encodeURIComponent(branch):'';
  function go(url,cb){fetch(url,{credentials:'same-origin'}).then(function(r){return r.ok?r.json():null}).then(cb).catch(function(){});}
  go('/api/analytics/charts/portfolio-by-branch',function(d){
    if(!d||typeof Chart==='undefined')return;
    new Chart(document.getElementById('chBranch'),{type:'bar',data:{labels:d.labels,datasets:[
      {label:'Портфель',data:d.portfolio,backgroundColor:'rgba(59,130,246,.6)',borderRadius:5,borderSkipped:false},
      {label:'NPL%',data:d.npl_rate,type:'line',borderColor:'#ef4444',backgroundColor:'transparent',tension:.3,pointRadius:3,yAxisID:'y1'}
    ]},options:{responsive:true,maintainAspectRatio:false,plugins:{tooltip:TT,legend:{labels:{color:'#94a3b8',boxWidth:8,font:{size:10}}}},scales:{x:{ticks:{color:'#94a3b8',font:{size:9}}},y:{ticks:{color:'#94a3b8',font:{size:9}},grid:{color:'rgba(51,65,85,.4)'}},y1:{position:'right',ticks:{color:'#ef4444',font:{size:9}},grid:{display:false}}}}});
  });
  go('/api/analytics/charts/portfolio-by-term',function(d){
    if(!d||typeof Chart==='undefined')return;
    new Chart(document.getElementById('chTerm'),{type:'doughnut',data:{labels:d.labels,datasets:[{data:d.data,backgroundColor:['#3b82f6','#22c55e','#f59e0b','#ef4444','#a855f7','#06b6d4'],borderWidth:2,borderColor:'var(--card)'}]},options:{responsive:true,maintainAspectRatio:false,cutout:'60%',plugins:{tooltip:TT,legend:{position:'right',labels:{color:'#94a3b8',boxWidth:8,font:{size:10}}}}}});
  });
  go('/api/analytics/charts/monthly-trend'+bq,function(d){
    if(!d||typeof Chart==='undefined')return;
    new Chart(document.getElementById('chTrend'),{type:'line',data:{labels:d.labels,datasets:[
      {label:'Выдано',data:d.issued,borderColor:'#3b82f6',backgroundColor:'rgba(59,130,246,.1)',fill:true,tension:.35,pointRadius:2},
      {label:'Погашено',data:d.collected,borderColor:'#22c55e',backgroundColor:'rgba(34,197,94,.1)',fill:true,tension:.35,pointRadius:2}
    ]},options:{responsive:true,maintainAspectRatio:false,plugins:{tooltip:TT,legend:{labels:{color:'#94a3b8',boxWidth:8,font:{size:10}}}},scales:{x:{ticks:{color:'#94a3b8',font:{size:9}}},y:{ticks:{color:'#94a3b8',font:{size:9}},grid:{color:'rgba(51,65,85,.4)'}}}}});
  });
})();
</script>

{% if channel_data %}
<div class="stit"><em>Канальная аналитика</em> (по ответственным лицам)</div>
<div class="card" style="margin-bottom:20px">
  <div class="tbl-wrap"><table>
    <thead><tr><th>Канал / ответственный</th><th>Договоров</th><th>Портфель (млн)</th><th>NPL%</th><th>Сред. чек</th></tr></thead>
    <tbody>
    {% for ch in channel_data %}
    <tr>
      <td>{{ch.channel}}</td>
      <td class="num">{{"{:,}".format(ch.total)}}</td>
      <td class="num">{{ch.portfolio_mln}} млн</td>
      <td class="num" style="color:{{'var(--red)' if (ch.npl_rate or 0)>30 else 'var(--yellow)' if (ch.npl_rate or 0)>15 else 'var(--green)'}}">{{ch.npl_rate}}%</td>
      <td class="num">{{"{:,.0f}".format(ch.avg_ticket or 0)}}</td>
    </tr>
    {% endfor %}
    </tbody>
  </table></div>
</div>
{% endif %}

{% if repeat_data %}
<div class="stit"><em>Повторные клиенты</em></div>
<div class="g2" style="margin-bottom:20px">
  <div class="card">
    <h3>Распределение по кол-ву договоров</h3>
    <table style="font-size:13px;width:100%;margin-top:8px">
      <thead><tr><th>Кол-во договоров</th><th>Клиентов</th><th>Доля</th></tr></thead>
      <tbody>
      {% for rd in repeat_data.distribution %}
      <tr><td>{{rd.bucket}}</td><td class="num">{{"{:,}".format(rd.count)}}</td><td class="num">{{rd.pct}}%</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  <div class="card">
    <h3>Повторные по филиалам</h3>
    <table style="font-size:13px;width:100%;margin-top:8px">
      <thead><tr><th>Филиал</th><th>Всего кл.</th><th>Повторных</th><th>Доля</th></tr></thead>
      <tbody>
      {% for rb in repeat_data.by_branch %}
      <tr><td>{{rb.branch}}</td><td class="num">{{"{:,}".format(rb.total_clients)}}</td><td class="num">{{"{:,}".format(rb.repeat_clients)}}</td><td class="num" style="color:var(--green)">{{rb.repeat_pct}}%</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% endif %}

{% if aging_data %}
<div class="stit"><em>Aging</em> распределение просрочки</div>
<div class="g2" style="margin-bottom:20px">
  <div class="card">
    <table style="font-size:13px;width:100%">
      <thead><tr><th>Бакет</th><th>Договоров</th><th>Долг</th><th>Доля</th><th style="width:80px"></th></tr></thead>
      <tbody>
      {% for a in aging_data %}
      {% set bc = 'barf-red' if 'дней' not in a.bucket and a.bucket not in ['0 дней','1-30'] else 'barf-green' if a.bucket in ['0 дней'] else 'barf-yellow' %}
      <tr>
        <td>{{a.bucket}}</td>
        <td class="num">{{"{:,}".format(a.cnt)}}</td>
        <td class="num">{{"{:,.0f}".format(a.debt_sum)}}</td>
        <td class="num">{{a.pct}}%</td>
        <td><div class="bar"><div class="barf {{bc}}" style="width:{{[a.pct, 100]|min}}%"></div></div></td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  <div class="card"><div class="chart-container chart-sm"><canvas id="chAging"></canvas></div></div>
</div>
<script>
(function(){
  var labels=[{% for a in aging_data %}'{{a.bucket}}',{% endfor %}];
  var data=[{% for a in aging_data %}{{a.cnt}},{% endfor %}];
  if(typeof Chart!=='undefined'&&document.getElementById('chAging')){
    new Chart(document.getElementById('chAging'),{type:'bar',data:{labels:labels,datasets:[{label:'Договоров',data:data,backgroundColor:['#22c55e','#eab308','#f97316','#ef4444','#dc2626','#991b1b','#7f1d1d'],borderRadius:4,borderSkipped:false}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#94a3b8',font:{size:9}}},y:{ticks:{color:'#94a3b8',font:{size:9}},grid:{color:'rgba(51,65,85,.4)'}}}}});
  }
})();
</script>
{% endif %}

{% if employee_risk %}
<div class="stit"><em>Сотрудники</em> рейтинг по риску и эффективности</div>
<div class="g2" style="margin-bottom:20px">
  <div class="card">
    <h3 style="margin-bottom:8px">Топ-10 высокий NPL%</h3>
    <table style="font-size:12px;width:100%">
      <thead><tr><th>Сотрудник</th><th>Дог.</th><th>NPL%</th><th>Сбор%</th></tr></thead>
      <tbody>
      {% for e in employee_risk %}
      <tr>
        <td>{{e.emp}}</td>
        <td class="num">{{e.total}}</td>
        <td class="num" style="color:{{'var(--red)' if (e.npl_rate or 0)>40 else 'var(--yellow)' if (e.npl_rate or 0)>20 else 'var(--green)'}}">{{e.npl_rate}}%</td>
        <td class="num">{{e.collection_rate}}%</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  <div class="card">
    <h3 style="margin-bottom:8px">Топ-10 лучших (низкий NPL%)</h3>
    <table style="font-size:12px;width:100%">
      <thead><tr><th>Сотрудник</th><th>Дог.</th><th>NPL%</th><th>Сбор%</th></tr></thead>
      <tbody>
      {% for e in employee_best %}
      <tr>
        <td>{{e.emp}}</td>
        <td class="num">{{e.total}}</td>
        <td class="num" style="color:var(--green)">{{e.npl_rate}}%</td>
        <td class="num">{{e.collection_rate}}%</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% endif %}

{% if income_segments %}
<div class="stit"><em>Сегменты</em> по источнику дохода</div>
<div class="card" style="margin-bottom:20px">
  <table style="font-size:13px;width:100%">
    <thead><tr><th>Источник дохода</th><th>Договоров</th><th>NPL</th><th>PD%</th><th style="width:80px"></th></tr></thead>
    <tbody>
    {% for s in income_segments %}
    {% set ic = 'barf-red' if (s.pd or 0)>50 else 'barf-yellow' if (s.pd or 0)>30 else 'barf-green' %}
    <tr>
      <td>{{s.segment}}</td>
      <td class="num">{{"{:,}".format(s.total)}}</td>
      <td class="num">{{"{:,}".format(s.npl_cnt)}}</td>
      <td class="num" style="font-weight:700;color:{{'var(--red)' if (s.pd or 0)>50 else 'var(--yellow)' if (s.pd or 0)>30 else 'var(--green)'}}">{{s.pd}}%</td>
      <td><div class="bar"><div class="barf {{ic}}" style="width:{{[s.pd, 100]|min}}%"></div></div></td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
</div>
{% endif %}

{% if sales_channel %}
<div class="stit"><em>Канал продаж</em> онлайн vs офлайн (из таблицы sales)</div>
<div class="g2" style="margin-bottom:20px">
  <div class="card">
    <table style="font-size:13px;width:100%">
      <thead><tr><th>Канал</th><th>Продаж</th><th>Сумма (млн)</th><th>Сред. чек</th></tr></thead>
      <tbody>
      {% for sc in sales_channel %}
      <tr>
        <td><span class="badge {{'bg' if sc.channel=='Онлайн' else 'bb'}}">{{sc.channel}}</span></td>
        <td class="num">{{"{:,}".format(sc.cnt)}}</td>
        <td class="num">{{sc.total_mln}}</td>
        <td class="num">{{"{:,.0f}".format(sc.avg_ticket or 0)}}</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  {% if sales_products %}
  <div class="card">
    <h3 style="margin-bottom:8px">Топ товарных групп</h3>
    <table style="font-size:12px;width:100%">
      <thead><tr><th>Группа</th><th>Кол-во</th><th>Сумма</th></tr></thead>
      <tbody>
      {% for sp in sales_products %}
      <tr>
        <td>{{sp.pg}}</td>
        <td class="num">{{"{:,}".format(sp.cnt)}}</td>
        <td class="num">{{sp.total_mln}} млн</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  {% endif %}
</div>
{% endif %}

<div class="stit"><em>AI Predictions</em> (прогнозы)</div>
<div class="g3" style="margin-bottom:20px">
  <div class="card" style="text-align:center;padding:30px 16px">
    <div style="font-size:28px;margin-bottom:6px">NPL</div>
    <div style="font-size:13px;color:var(--muted)">Прогноз просрочки</div>
    <div style="font-size:11px;color:var(--muted);margin-top:8px">API: /api/intelligence/forecast</div>
    <div class="badge bb" style="margin-top:8px">Подключается</div>
  </div>
  <div class="card" style="text-align:center;padding:30px 16px">
    <div style="font-size:28px;margin-bottom:6px">Portfolio</div>
    <div style="font-size:13px;color:var(--muted)">Прогноз портфеля</div>
    <div style="font-size:11px;color:var(--muted);margin-top:8px">API: /api/intelligence/forecast</div>
    <div class="badge bb" style="margin-top:8px">Подключается</div>
  </div>
  <div class="card" style="text-align:center;padding:30px 16px">
    <div style="font-size:28px;margin-bottom:6px">EBITDA</div>
    <div style="font-size:13px;color:var(--muted)">Прогноз финансов</div>
    <div style="font-size:11px;color:var(--muted);margin-top:8px">Для /finance</div>
    <div class="badge bb" style="margin-top:8px">Подключается</div>
  </div>
</div>

</div>

<script>
function toggleVintage() {
  var t = document.getElementById('vintageTable');
  var a = document.getElementById('vintageArrow');
  if (t.style.display === 'none') { t.style.display = ''; a.innerText = '▼'; }
  else { t.style.display = 'none'; a.innerText = '▶'; }
}
function refreshIntel() {
  fetch('/api/refresh-analytics', { method: 'POST', credentials: 'same-origin' })
    .then(function(r) {
      if (r.status === 401) {
        return r.json().then(function(d) {
          alert(d.message || 'Сессия истекла. Обновите страницу и войдите снова.');
          window.location.href = '/login';
        });
      }
      window.location.reload();
    })
    .catch(function() { alert('Ошибка сети'); });
}
function trainML() {
  if (!confirm('Запустить обучение ML-модели? Это займёт 1–2 минуты.')) return;
  fetch('/api/ml/train', { method: 'POST' }).then(function(r) { return r.json(); }).then(function(d) {
    alert(d.message || 'ML-модель обучена');
  });
}
function checkIntegrity() {
  fetch('/api/integrity').then(function(r) { return r.json(); }).then(function(d) {
    alert('Результат проверки: ' + (d.summary || (d.issues_count != null ? d.issues_count + ' замечаний' : JSON.stringify(d))));
  });
}
function runBackup() {
  fetch('/api/backup', { method: 'POST' }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.status === 'error' || !d.ok) {
      alert('Ошибка: ' + (d.error || d.message || 'не удалось создать бэкап'));
    } else {
      alert(d.message || 'Бэкап создан');
    }
  });
}
</script>
""" + F

INTEL_T = INTELLIGENCE_T

FINANCE_T = H('OPEX / Финансы') + """<div class="ctr">
<div class="page-header">
  <h1>OPEX / Финансовая аналитика</h1>
  <p style="font-size:13px;color:var(--muted);margin-top:6px">Операционные расходы по филиалам, категориям и периодам</p>
  <div class="fl fw" style="gap:8px;margin-top:12px">
    <a href="/intelligence" class="btn btn-o btn-sm">Аналитика</a>
    <a href="/" class="btn btn-o btn-sm">Дашборд</a>
  </div>
</div>

<form method="get" action="/finance" class="sticky-filters" style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:10px 12px;background:var(--card);border-radius:10px;border:1px solid var(--border);margin-bottom:16px">
  <span style="font-size:13px;font-weight:600">Фильтры:</span>
  <label style="display:flex;align-items:center;gap:6px;font-size:13px">Филиал
    <select name="branch" style="padding:6px 10px;border-radius:6px;border:1px solid var(--border);background:var(--bg)">
      <option value="">Все</option>
      {% for k,v in BRANCHES.items() %}<option value="{{ k }}" {{ 'selected' if cur_branch==k else '' }}>{{ k }} — {{ v }}</option>{% endfor %}
    </select>
  </label>
  <label style="display:flex;align-items:center;gap:6px;font-size:13px">Период
    <select name="period" style="padding:6px 10px;border-radius:6px;border:1px solid var(--border);background:var(--bg)">
      <option value="">Все</option>
      {% for p in periods %}<option value="{{ p }}" {{ 'selected' if cur_period==p else '' }}>{{ p }}</option>{% endfor %}
    </select>
  </label>
  <button type="submit" class="btn btn-p btn-sm">Применить</button>
</form>

<div class="kpi-grid" style="margin-bottom:20px">
  <div class="kpi-card kpi-accent-gold"><div class="kv" id="opex-total">—</div><div class="kl">Общий OPEX</div></div>
  <div class="kpi-card kpi-accent-blue"><div class="kv" id="opex-per-contract">—</div><div class="kl">OPEX / договор</div></div>
  <div class="kpi-card kpi-accent-orange"><div class="kv" id="opex-top-cat">—</div><div class="kl">Top категория</div></div>
  <div class="kpi-card kpi-accent-purple"><div class="kv" id="opex-entries">—</div><div class="kl">Записей</div></div>
</div>

<div class="g2" style="margin-bottom:20px">
  <div class="card"><div class="sec-head"><h3>Структура по категориям</h3></div><div class="chart-container"><canvas id="chOpexCat"></canvas></div></div>
  <div class="card"><div class="sec-head"><h3>Тренд OPEX по месяцам (млн)</h3></div><div class="chart-container"><canvas id="chOpexTrend"></canvas></div></div>
</div>

<div class="card" style="margin-bottom:20px">
  <div class="sec-head"><h3>OPEX по филиалам</h3></div>
  <div class="chart-container"><canvas id="chOpexBranch"></canvas></div>
</div>

<div class="card" style="margin-bottom:20px">
  <div class="sec-head"><h3>Таблица по категориям</h3></div>
  <div id="opexCatTable" style="font-size:13px">Загрузка...</div>
</div>

<script>
(function(){
  var TT={backgroundColor:'#0f172a',borderColor:'#334155',padding:8,cornerRadius:5};
  var branch='{{ cur_branch or "" }}';
  var period='{{ cur_period or "" }}';
  var qs=[];
  if(branch)qs.push('branch='+encodeURIComponent(branch));
  if(period)qs.push('period='+encodeURIComponent(period));
  var q=qs.length?'?'+qs.join('&'):'';
  var bq=branch?'?branch='+encodeURIComponent(branch):'';

  function go(url,cb){fetch(url,{credentials:'same-origin'}).then(function(r){return r.ok?r.json():null}).then(cb).catch(function(){});}
  function N(x){return x==null?'—':Number(x).toLocaleString('ru-RU',{maximumFractionDigits:0})}
  function M(x){return x==null?'—':(Number(x)/1e6).toFixed(1)+' млн'}

  go('/api/finance/summary'+q,function(d){
    if(!d)return;
    document.getElementById('opex-total').textContent=M(d.total_opex);
    document.getElementById('opex-per-contract').textContent=N(d.opex_per_contract);
    document.getElementById('opex-top-cat').textContent=d.top_category||'—';
    document.getElementById('opex-entries').textContent=N(d.entries);
  });

  go('/api/finance/by-category'+q,function(d){
    if(!d||typeof Chart==='undefined')return;
    new Chart(document.getElementById('chOpexCat'),{type:'doughnut',data:{labels:d.map(function(r){return r.category}),datasets:[{data:d.map(function(r){return r.total}),backgroundColor:['#3b82f6','#22c55e','#f59e0b','#ef4444','#a855f7','#06b6d4','#f97316','#64748b'],borderWidth:2,borderColor:'var(--card)'}]},options:{responsive:true,maintainAspectRatio:false,cutout:'60%',plugins:{tooltip:TT,legend:{position:'right',labels:{color:'#94a3b8',boxWidth:8,font:{size:10}}}}}});
    var h='<table style="width:100%"><thead><tr><th>Категория</th><th>Сумма</th><th>%</th><th></th></tr></thead><tbody>';
    d.forEach(function(r){
      h+='<tr><td>'+r.category+'</td><td class="num">'+N(r.total)+'</td><td class="num">'+r.pct+'%</td><td><div class="bar" style="width:80px"><div class="barf barf-blue" style="width:'+r.pct+'%"></div></div></td></tr>';
    });
    document.getElementById('opexCatTable').innerHTML=h+'</tbody></table>';
  });

  go('/api/finance/monthly-trend'+bq,function(d){
    if(!d||typeof Chart==='undefined')return;
    new Chart(document.getElementById('chOpexTrend'),{type:'line',data:{labels:d.labels,datasets:[{label:'OPEX (млн)',data:d.data,borderColor:'#f59e0b',backgroundColor:'rgba(245,158,11,.1)',fill:true,tension:.35,pointRadius:3}]},options:{responsive:true,maintainAspectRatio:false,plugins:{tooltip:TT,legend:{labels:{color:'#94a3b8',boxWidth:8,font:{size:10}}}},scales:{x:{ticks:{color:'#94a3b8',font:{size:9}}},y:{ticks:{color:'#94a3b8',font:{size:9}},grid:{color:'rgba(51,65,85,.4)'}}}}});
  });

  go('/api/finance/by-branch'+(period?'?period='+encodeURIComponent(period):''),function(d){
    if(!d||typeof Chart==='undefined')return;
    new Chart(document.getElementById('chOpexBranch'),{type:'bar',data:{labels:d.map(function(r){return r.branch}),datasets:[{label:'OPEX',data:d.map(function(r){return r.total/1e6}),backgroundColor:'rgba(245,158,11,.6)',borderRadius:5,borderSkipped:false}]},options:{responsive:true,maintainAspectRatio:false,plugins:{tooltip:TT,legend:{labels:{color:'#94a3b8',boxWidth:8,font:{size:10}}}},scales:{x:{ticks:{color:'#94a3b8',font:{size:9}}},y:{ticks:{color:'#94a3b8',font:{size:9}},grid:{color:'rgba(51,65,85,.4)'}}}}});
  });
})();
</script>
""" + F

# ═══ ANALYTICS ROUTES (vNext: aging, employee, segments, decision center) ═══

@app.route('/aging')
@login_required
def aging_report():
    u = session['user']
    mods = u.get('modules', [])
    if 'dashboard' not in mods and 'intelligence' not in mods and u.get('role') not in ('executive', 'director', 'risk_director', 'head_analyst'):
        return "Нет доступа", 403
    from core.kpi import get_aging_table, get_npl_by_branch, get_npl_trend
    branch = u.get('branch') if u.get('role') in ('branch_manager', 'branch_employee', 'credit_officer') else None
    return render_template('aging.html',
                           aging=get_aging_table(branch),
                           branches=get_npl_by_branch(),
                           npl_trend=get_npl_trend(12),
                           user=u,
                           now=datetime.now().strftime('%d.%m.%Y %H:%M'))

@app.route('/employees-analytics')
@login_required
def employees_analytics():
    u = session['user']
    mods = u.get('modules', [])
    if 'employees' not in mods and u.get('role') not in ('executive', 'director', 'risk_director', 'head_analyst'):
        return "Нет доступа", 403
    from core.kpi import get_employee_performance
    branch = u.get('branch') if u.get('role') in ('branch_manager', 'branch_employee', 'credit_officer') else None
    employees = get_employee_performance(branch=branch, limit=100)
    return render_template('employees_analytics.html',
                           employees=employees, user=u,
                           now=datetime.now().strftime('%d.%m.%Y %H:%M'))

@app.route('/segments')
@login_required
def segments():
    u = session['user']
    mods = u.get('modules', [])
    if 'intelligence' not in mods and 'dashboard' not in mods and u.get('role') not in ('executive', 'director', 'risk_director', 'head_analyst'):
        return "Нет доступа", 403
    from core.kpi import get_client_segments, get_income_segments, get_product_segments
    return render_template('segments.html',
                           age_segments=get_client_segments(),
                           income_segments=get_income_segments(),
                           product_segments=get_product_segments(),
                           user=u,
                           now=datetime.now().strftime('%d.%m.%Y %H:%M'))

@app.route('/decision-center')
@login_required
def decision_center():
    u = session['user']
    if u.get('role') not in ('executive', 'director', 'risk_director', 'head_analyst', 'super_admin'):
        return "Нет доступа", 403
    from core.kpi import generate_recommendations
    branch = u.get('branch') if u.get('role') in ('branch_manager',) else None
    return render_template('decision_center.html',
                           recommendations=generate_recommendations(branch=branch),
                           user=u,
                           now=datetime.now().strftime('%d.%m.%Y %H:%M'))

# ═══ ANALYTICS API (unified KPI) ═══

@app.route('/api/kpi/portfolio')
@login_required
def api_kpi_portfolio():
    from core.kpi import get_portfolio_kpi
    branch = request.args.get('branch', '').strip() or None
    u = session['user']
    if u.get('role') in ('branch_manager', 'branch_employee', 'credit_officer') and u.get('branch'):
        branch = u['branch']
    return jsonify(get_portfolio_kpi(branch))

@app.route('/api/kpi/aging')
@login_required
def api_kpi_aging():
    from core.kpi import get_aging_table
    branch = request.args.get('branch', '').strip() or None
    return jsonify(get_aging_table(branch))

@app.route('/api/kpi/npl-trend')
@login_required
def api_kpi_npl_trend():
    from core.kpi import get_npl_trend
    months = int(request.args.get('months', '12'))
    return jsonify(get_npl_trend(months))

@app.route('/api/kpi/employees')
@login_required
def api_kpi_employees():
    from core.kpi import get_employee_performance
    branch = request.args.get('branch', '').strip() or None
    u = session['user']
    if u.get('role') in ('branch_manager', 'branch_employee', 'credit_officer') and u.get('branch'):
        branch = u['branch']
    return jsonify(get_employee_performance(branch))

@app.route('/api/kpi/segments')
@login_required
def api_kpi_segments():
    from core.kpi import get_client_segments, get_income_segments, get_product_segments
    return jsonify({
        'age': get_client_segments(),
        'income': get_income_segments(),
        'products': get_product_segments(),
    })

@app.route('/api/kpi/recommendations')
@login_required
def api_kpi_recommendations():
    from core.kpi import generate_recommendations
    branch = request.args.get('branch', '').strip() or None
    return jsonify(generate_recommendations(branch))

@app.route('/api/kpi/npl-by-branch')
@login_required
def api_kpi_npl_by_branch():
    from core.kpi import get_npl_by_branch
    year = request.args.get('year', '').strip() or None
    product_type = request.args.get('product_type', '').strip() or None
    return jsonify(get_npl_by_branch(year, product_type))


# ═══ WIZARD API (Guided flows) ═══

@app.route('/api/wizard/branch-scoring', methods=['POST'])
@login_required
def api_wizard_branch_scoring():
    """Сохранить параметры скоринга для филиала."""
    data = request.get_json() or {}
    branch = (data.get('branch') or '').strip()
    if not branch:
        return jsonify({'error': 'branch required'}), 400
    u = session.get('user', {})
    try:
        db = get_db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS branch_scoring_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch TEXT NOT NULL,
                min_score INTEGER,
                max_amount REAL,
                blocked_products TEXT,
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            INSERT INTO branch_scoring_rules (branch, min_score, max_amount, blocked_products, created_by)
            VALUES (?, ?, ?, ?, ?)
        """, (branch, data.get('minScore'), data.get('maxAmount'),
              ','.join(data.get('blockedProducts') or []), u.get('username', '')))
        db.commit()
        db.close()
        log_audit(u.get('username', ''), 'wizard_branch_scoring', entity='branch_scoring_rules', details=f'branch={branch}')
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


@app.route('/api/wizard/segment-limits', methods=['POST'])
@login_required
def api_wizard_segment_limits():
    """Сохранить лимиты для сегмента."""
    data = request.get_json() or {}
    segment = (data.get('segment') or '').strip()
    if not segment:
        return jsonify({'error': 'segment required'}), 400
    u = session.get('user', {})
    try:
        db = get_db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS segment_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                segment TEXT NOT NULL,
                max_limit REAL,
                min_score INTEGER,
                max_term INTEGER,
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            INSERT INTO segment_rules (segment, max_limit, min_score, max_term, created_by)
            VALUES (?, ?, ?, ?, ?)
        """, (segment, data.get('maxLimit'), data.get('minScoreThreshold'), data.get('maxTerm'), u.get('username', '')))
        db.commit()
        db.close()
        log_audit(u.get('username', ''), 'wizard_segment_limits', entity='segment_rules', details=f'segment={segment}')
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


@app.route('/api/wizard/promotion', methods=['POST'])
@login_required
def api_wizard_promotion():
    """Сохранить акцию."""
    data = request.get_json() or {}
    u = session.get('user', {})
    try:
        db = get_db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS promotions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch TEXT,
                segment TEXT,
                rate REAL,
                max_sum REAL,
                date_from TEXT,
                date_to TEXT,
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            INSERT INTO promotions (branch, segment, rate, max_sum, date_from, date_to, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (data.get('branch') or '', data.get('segment') or '', data.get('rate'),
              data.get('maxSum'), data.get('dateFrom'), data.get('dateTo'), u.get('username', '')))
        db.commit()
        db.close()
        log_audit(u.get('username', ''), 'wizard_promotion', entity='promotions', details=f"branch={data.get('branch')}")
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


# ═══ ERROR HANDLERS (404, 403, 500) ═══

def _wants_json():
    return request.accept_mimetypes.best_match(['application/json', 'text/html']) == 'application/json' or request.path.startswith('/api/')

@app.errorhandler(404)
def not_found(e):
    if _wants_json():
        return jsonify({"error": "not_found", "message": "Страница не найдена"}), 404
    return ("""
    <!DOCTYPE html><html><head><meta charset="UTF-8"><title>404</title>
    <style>body{background:#0f172a;color:#e2e8f0;font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;}
    .box{text-align:center;padding:40px;background:#1e293b;border-radius:16px;border:1px solid #334155;}
    h1{color:#94a3b8;} a{color:#3b82f6;text-decoration:none;}</style></head><body>
    <div class="box"><h1>404 — Страница не найдена</h1><p><a href="/">← На главную</a></p></div></body></html>
    """, 404)

@app.errorhandler(403)
def forbidden(e):
    if _wants_json():
        return jsonify({"error": "forbidden", "message": "Доступ запрещён"}), 403
    return ("""
    <!DOCTYPE html><html><head><meta charset="UTF-8"><title>403</title>
    <style>body{background:#0f172a;color:#e2e8f0;font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;}
    .box{text-align:center;padding:40px;background:#1e293b;border-radius:16px;border:1px solid #334155;}
    h1{color:#ef4444;} a{color:#3b82f6;text-decoration:none;}</style></head><body>
    <div class="box"><h1>403 — Доступ запрещён</h1><p><a href="/">← На главную</a></p></div></body></html>
    """, 403)

@app.errorhandler(500)
def server_error(e):
    app.logger.error("Server error: %s", e, exc_info=True)
    if _wants_json():
        return jsonify({"error": "internal_error", "message": "Внутренняя ошибка сервера"}), 500
    return ("""
    <!DOCTYPE html><html><head><meta charset="UTF-8"><title>500</title>
    <style>body{background:#0f172a;color:#e2e8f0;font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;}
    .box{text-align:center;padding:40px;background:#1e293b;border-radius:16px;border:1px solid #334155;}
    h1{color:#f59e0b;} a{color:#3b82f6;text-decoration:none;}</style></head><body>
    <div class="box"><h1>500 — Внутренняя ошибка</h1><p><a href="/">← На главную</a></p></div></body></html>
    """, 500)


# ═══ SPA FALLBACK ═══

SPA_DIR = os.path.join(BASE_DIR, 'static', 'spa')

@app.route('/spa/')
@app.route('/spa/<path:path>')
def serve_spa(path=''):
    if path and os.path.isfile(os.path.join(SPA_DIR, path)):
        return send_file(os.path.join(SPA_DIR, path))
    index = os.path.join(SPA_DIR, 'index.html')
    if os.path.isfile(index):
        return send_file(index)
    return "SPA not built yet. Run: cd frontend && npm run build", 404


# ═══ STARTUP ═══

if __name__ == '__main__':
    import socket
    import sys
    def _p(msg):
        try:
            print(msg)
        except UnicodeEncodeError:
            print(msg.encode('ascii', errors='replace').decode('ascii'))
    print("=" * 55)
    _p("  CREDIT CONTROL CENTER - PRODUCTION")
    _p("  Credit Intelligence Platform")
    print("=" * 55)
    try:
        init_db()
    except Exception as e:
        err_lower = str(e).lower()
        if any(k in err_lower for k in ("readonly", "unable to open", "attempt to write", "operationalerror")):
            _p("  [ПРЕДУПРЕЖДЕНИЕ] БД по умолчанию недоступна.")
            _p(f"  Исходный путь: {DB_PATH}")
            fallback = os.path.join(os.environ.get('TEMP', os.environ.get('TMP', '.')), 'ccc_credit_control.db')
            _p(f"  Используем fallback: {fallback}")
            import config.settings
            import core.database
            config.settings.DB_PATH = fallback
            core.database.DB_PATH = fallback
            try:
                init_db()
            except Exception as e2:
                _p(f"  [ОШИБКА] Fallback тоже не сработал: {e2}")
                raise SystemExit(1)
        else:
            raise
    nc = count_contracts()
    ncl = count_clients()
    _p(f"  БД: {DB_PATH}")
    _p(f"  Клиентов: {ncl:,}")
    _p(f"  Договоров: {nc:,}")
    if nc > 0 and ncl == 0:
        _p("\n  [!] Запустите миграцию: python migrate.py --source old.db")
    if nc > 0:
        # Analytics snapshot
        compute_daily_snapshot()
        _p("  Аналитический снимок OK")
        # Intelligence tables
        try:
            compute_intelligence_tables()
            prepare_training_dataset()
            _p("  Intelligence tables OK")
        except Exception as e:
            _p(f"  Intelligence: {e}")
        # Backup
        try:
            r = create_backup()
            _p(f"  Backup: {r.get('size_mb',0)} MB OK")
        except: pass
        # Integrity
        try:
            ic = run_integrity_checks()
            _p(f"  Целостность: {ic['status']} ({ic['issues_count']} issues)")
        except: pass
    # Start scheduler
    start_scheduler()
    _p("  Scheduler запущен (hourly/daily)")
    # Intelligence layer batch refresh (background, non-blocking)
    try:
        from engines.behavioral_scoring_engine import compute_behavioral_score_batch
        from engines.feature_store_engine import refresh_feature_store
        import threading
        def _background_refresh():
            try:
                refresh_feature_store(limit=500)
                compute_behavioral_score_batch(limit=500)
                _p("  Feature store + behavioral scores refreshed OK")
            except Exception as _be:
                _p(f"  Background refresh: {_be}")
        threading.Thread(target=_background_refresh, daemon=True).start()
    except Exception as _e:
        pass
    # Network
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
    except: ip = "127.0.0.1"
    _p(f"\n  Этот ПК:  http://127.0.0.1:5000")
    _p(f"  Сеть:     http://{ip}:5000")
    _p(f"  Health:   http://127.0.0.1:5000/system/health")
    _p(f"  API:      POST http://127.0.0.1:5000/api/score")
    _p(f"  Логин: director / Director@2026!")
    print("=" * 55)
    debug = os.environ.get('FLASK_DEBUG', '1') == '1'
    app.run(host='0.0.0.0', port=5000, debug=debug, use_reloader=debug)