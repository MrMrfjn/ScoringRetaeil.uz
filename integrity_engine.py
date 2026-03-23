"""
CCC — Data Integrity Engine
Validates data consistency. Run periodically or on demand.
"""
from core.database import get_db

def run_integrity_checks():
    """
    Full data integrity audit.
    Returns list of issues found.
    """
    db = get_db()
    issues = []

    # 1. Orphan contracts (no matching client)
    orphans = db.execute("""
        SELECT COUNT(*) as c FROM contracts
        WHERE client_id NOT IN (SELECT id FROM clients)
    """).fetchone()['c']
    if orphans > 0:
        issues.append({"severity": "critical", "check": "orphan_contracts",
            "message": f"{orphans} договоров без клиента", "count": orphans})

    # 2. Clients without contracts
    no_contracts = db.execute("""
        SELECT COUNT(*) as c FROM clients
        WHERE id NOT IN (SELECT DISTINCT client_id FROM contracts)
    """).fetchone()['c']
    if no_contracts > 0:
        issues.append({"severity": "warning", "check": "clients_no_contracts",
            "message": f"{no_contracts} клиентов без договоров", "count": no_contracts})

    # 3. Duplicate passports
    dup_passports = db.execute("""
        SELECT passport, COUNT(*) as c FROM clients
        WHERE passport IS NOT NULL AND passport != ''
        GROUP BY passport HAVING c > 1
    """).fetchall()
    if dup_passports:
        issues.append({"severity": "warning", "check": "duplicate_passports",
            "message": f"{len(dup_passports)} дублей паспортов", "count": len(dup_passports)})

    # 4. Duplicate phones
    dup_phones = db.execute("""
        SELECT phone, COUNT(*) as c FROM clients
        WHERE phone IS NOT NULL AND phone != ''
        GROUP BY phone HAVING c > 1
    """).fetchall()
    if dup_phones:
        issues.append({"severity": "info", "check": "duplicate_phones",
            "message": f"{len(dup_phones)} дублей телефонов", "count": len(dup_phones)})

    # 5. Contracts with negative amounts
    neg = db.execute("""
        SELECT COUNT(*) as c FROM contracts
        WHERE product_amount < 0 OR debt_amount < 0 OR paid_amount < 0
    """).fetchone()['c']
    if neg > 0:
        issues.append({"severity": "critical", "check": "negative_amounts",
            "message": f"{neg} договоров с отрицательными суммами", "count": neg})

    # 6. Contracts with paid > product (overpayment anomaly)
    overpaid = db.execute("""
        SELECT COUNT(*) as c FROM contracts
        WHERE paid_amount > product_amount * 1.5 AND product_amount > 0
    """).fetchone()['c']
    if overpaid > 0:
        issues.append({"severity": "info", "check": "overpayment",
            "message": f"{overpaid} договоров с переплатой >150%", "count": overpaid})

    # 7. NULL status_detail
    null_status = db.execute("""
        SELECT COUNT(*) as c FROM contracts
        WHERE status_detail IS NULL OR status_detail = ''
    """).fetchone()['c']
    if null_status > 0:
        issues.append({"severity": "warning", "check": "null_status",
            "message": f"{null_status} договоров без статуса", "count": null_status})

    # 8. Future contract dates
    future = db.execute("""
        SELECT COUNT(*) as c FROM contracts
        WHERE contract_date > date('now', '+30 days')
    """).fetchone()['c']
    if future > 0:
        issues.append({"severity": "info", "check": "future_dates",
            "message": f"{future} договоров с датой в будущем", "count": future})

    # Summary
    total = db.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
    clients = db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    db.close()

    return {
        "total_clients": clients,
        "total_contracts": total,
        "issues": issues,
        "issues_count": len(issues),
        "critical": sum(1 for i in issues if i['severity'] == 'critical'),
        "warnings": sum(1 for i in issues if i['severity'] == 'warning'),
        "status": "critical" if any(i['severity'] == 'critical' for i in issues) else
                  "warning" if any(i['severity'] == 'warning' for i in issues) else "ok",
    }
