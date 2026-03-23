"""
CCC Intelligence Platform — Extended RBAC
PART 7: Operational role definitions for branch network

Roles (5 levels):
  super_admin      — Platform admin, all access, user management
  risk_director    — Risk intelligence, all analytics, no branch restriction
  branch_manager   — Own branch: import, scoring, portfolio, employees
  credit_officer   — Own branch: scoring only, can view own history
  analyst          — Read-only analytics across branches (no scoring)

Each role defines:
  - allowed_modules: list of URL prefixes/page names
  - data_scope: 'all' | 'branch'
  - can_export: bool
  - can_import: bool
  - can_manage_users: bool
"""

from functools import wraps
from flask import session, redirect, url_for, jsonify, request


# ═══════════════════════════════════════════════════════════════
#  ROLE DEFINITIONS
# ═══════════════════════════════════════════════════════════════

OPERATIONAL_ROLES = {
    "super_admin": {
        "label":            "Супер Администратор",
        "label_uz":         "Super Administrator",
        "data_scope":       "all",
        "can_export":       True,
        "can_import":       True,
        "can_manage_users": True,
        "can_manage_limits": True,
        "modules": [
            "dashboard", "import", "scoring", "portfolio", "alerts",
            "employees", "export", "admin", "risk-intelligence",
            "executive", "ml", "intelligence", "backup", "scheduler",
            "integrity", "users",
        ],
        "color": "#7c3aed",
    },
    "risk_director": {
        "label":            "Директор по рискам",
        "label_uz":         "Risk Director",
        "data_scope":       "all",
        "can_export":       True,
        "can_import":       False,
        "can_manage_users": False,
        "can_manage_limits": True,
        "modules": [
            "dashboard", "scoring", "portfolio", "alerts",
            "employees", "export", "risk-intelligence", "executive",
            "ml", "intelligence",
        ],
        "color": "#dc2626",
    },
    "executive": {
        "label":            "Директор",
        "label_uz":         "Director",
        "data_scope":       "all",
        "can_export":       True,
        "can_import":       True,
        "can_manage_users": False,
        "can_manage_limits": False,
        "modules": [
            "dashboard", "import", "scoring", "portfolio", "alerts",
            "employees", "export", "admin", "risk-intelligence", "executive",
        ],
        "color": "#0891b2",
    },
    "head_analyst": {
        "label":            "Главный аналитик",
        "label_uz":         "Head Analyst",
        "data_scope":       "all",
        "can_export":       True,
        "can_import":       True,
        "can_manage_users": False,
        "can_manage_limits": False,
        "modules": [
            "dashboard", "import", "scoring", "portfolio", "alerts",
            "employees", "export", "risk-intelligence",
        ],
        "color": "#0d9488",
    },
    "branch_manager": {
        "label":            "Менеджер филиала",
        "label_uz":         "Branch Manager",
        "data_scope":       "branch",
        "can_export":       True,
        "can_import":       True,
        "can_manage_users": False,
        "can_manage_limits": False,
        "modules": [
            "import", "scoring", "portfolio", "alerts", "employees", "export",
        ],
        "color": "#0369a1",
    },
    "credit_officer": {
        "label":            "Кредитный офицер",
        "label_uz":         "Credit Officer",
        "data_scope":       "branch",
        "can_export":       False,
        "can_import":       False,
        "can_manage_users": False,
        "can_manage_limits": False,
        "modules": [
            "scoring",
        ],
        "color": "#059669",
    },
    "analyst": {
        "label":            "Аналитик",
        "label_uz":         "Analyst",
        "data_scope":       "all",
        "can_export":       True,
        "can_import":       False,
        "can_manage_users": False,
        "can_manage_limits": False,
        "modules": [
            "dashboard", "portfolio", "alerts", "export", "risk-intelligence",
        ],
        "color": "#7c3aed",
    },
    # Legacy roles (kept for backward compatibility)
    "branch_employee": {
        "label":            "Сотрудник",
        "label_uz":         "Employee",
        "data_scope":       "branch",
        "can_export":       False,
        "can_import":       False,
        "can_manage_users": False,
        "can_manage_limits": False,
        "modules": ["scoring"],
        "color": "#64748b",
    },
}


# ═══════════════════════════════════════════════════════════════
#  BRANCH ISOLATION HELPERS
# ═══════════════════════════════════════════════════════════════

def get_branch_filter(user: dict) -> str | None:
    """
    Return branch filter for DB queries based on user role.
    Returns None if user can see all branches.
    """
    role = user.get('role', 'branch_employee')
    role_cfg = OPERATIONAL_ROLES.get(role, {})

    if role_cfg.get('data_scope') == 'all':
        return None
    return user.get('branch')


def can_access_module(user: dict, module: str) -> bool:
    """Check if user role has access to a module."""
    role = user.get('role', 'branch_employee')
    role_cfg = OPERATIONAL_ROLES.get(role, {})
    return module in role_cfg.get('modules', [])


def can_export(user: dict) -> bool:
    role_cfg = OPERATIONAL_ROLES.get(user.get('role', ''), {})
    return role_cfg.get('can_export', False)


def can_import(user: dict) -> bool:
    role_cfg = OPERATIONAL_ROLES.get(user.get('role', ''), {})
    return role_cfg.get('can_import', False)


def can_manage_users(user: dict) -> bool:
    role_cfg = OPERATIONAL_ROLES.get(user.get('role', ''), {})
    return role_cfg.get('can_manage_users', False)


# ═══════════════════════════════════════════════════════════════
#  DECORATORS
# ═══════════════════════════════════════════════════════════════

def require_login(f):
    """Redirect to login if not authenticated."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user' not in session:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def require_role(*allowed_roles):
    """Allow only specified roles."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if 'user' not in session:
                return redirect(url_for('login'))
            if session['user'].get('role') not in allowed_roles:
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({"error": "forbidden", "required_roles": list(allowed_roles)}), 403
                return _forbidden_page(session['user'], allowed_roles)
            return f(*args, **kwargs)
        return wrapper
    return decorator


def require_module(module_name: str):
    """Allow only roles that have access to this module."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if 'user' not in session:
                return redirect(url_for('login'))
            user = session['user']
            if not can_access_module(user, module_name):
                return _forbidden_page(user, [])
            return f(*args, **kwargs)
        return wrapper
    return decorator


def require_scope_all(f):
    """Require data_scope='all' (no branch restriction)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        user = session['user']
        role_cfg = OPERATIONAL_ROLES.get(user.get('role', ''), {})
        if role_cfg.get('data_scope') != 'all':
            return _forbidden_page(user, ['executive','risk_director','head_analyst'])
        return f(*args, **kwargs)
    return wrapper


def _forbidden_page(user: dict, required: list) -> tuple:
    role_cfg = OPERATIONAL_ROLES.get(user.get('role',''), {})
    html = f"""
    <!DOCTYPE html><html><head><meta charset="UTF-8">
    <style>body{{background:#0f172a;color:#e2e8f0;font-family:sans-serif;
      display:flex;align-items:center;justify-content:center;height:100vh;}}
    .box{{text-align:center;padding:40px;background:#1e293b;border-radius:16px;
          border:1px solid #334155;max-width:400px}}
    h2{{color:#ef4444;margin-bottom:12px}} p{{color:#94a3b8;margin:8px 0}}
    a{{color:#3b82f6;text-decoration:none}}</style></head><body>
    <div class="box">
      <h2>🔒 Нет доступа</h2>
      <p>Ваша роль: <strong>{role_cfg.get('label','?')}</strong></p>
      <p>Эта страница недоступна для вашей роли.</p>
      <br><a href="/">← На главную</a>
    </div></body></html>
    """
    return html, 403


# ═══════════════════════════════════════════════════════════════
#  SETTINGS PATCH
# ═══════════════════════════════════════════════════════════════

def get_extended_roles_for_settings() -> dict:
    """
    Единый источник ролей для config.settings.ROLES (навигация, модули доступа).
    Алиасы для обратной совместимости: director→executive, risk_manager→risk_director, scoring_officer→credit_officer.
    """
    out = {
        role_id: {"label": cfg["label"], "modules": cfg["modules"]}
        for role_id, cfg in OPERATIONAL_ROLES.items()
    }
    out["director"] = out["executive"]
    out["risk_manager"] = out["risk_director"]
    out["scoring_officer"] = out["credit_officer"]
    return out


def create_default_users_for_all_roles(branches: dict) -> dict:
    """
    Generate default users for all 5 operational roles across all branches.
    Returns dict compatible with DEFAULT_USERS in settings.py.
    """
    import hashlib
    def _hash(pw):
        return hashlib.sha256(pw.encode()).hexdigest()

    users = {
        "super_admin": {
            "password_hash": _hash("SuperAdmin@2026!"),
            "role": "super_admin", "branch": None,
            "name": "Супер Администратор"
        },
        "risk_director": {
            "password_hash": _hash("RiskDir@2026!"),
            "role": "risk_director", "branch": None,
            "name": "Директор по рискам"
        },
        "analyst_main": {
            "password_hash": _hash("Analyst@2026!"),
            "role": "analyst", "branch": None,
            "name": "Аналитик"
        },
    }

    for code, name in branches.items():
        i = code[1:]  # "1" from "Ф1"
        users[f"manager_{code.lower()}"] = {
            "password_hash": _hash(f"Manager{code}@26!"),
            "role": "branch_manager", "branch": code,
            "name": f"Менеджер {name}"
        }
        users[f"officer_{code.lower()}"] = {
            "password_hash": _hash(f"Officer{code}@26!"),
            "role": "credit_officer", "branch": code,
            "name": f"Офицер {name}"
        }

    return users
