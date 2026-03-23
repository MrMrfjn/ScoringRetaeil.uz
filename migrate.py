"""
CCC FINAL — Migration Script
Extracts proper client records from denormalized contracts table.
Builds geo hierarchy. Computes initial analytics snapshots.
"""
import sqlite3, os, sys

def migrate(source_db, target_db):
    """
    Migrate from old CCC (denormalized) to new CCC (normalized).
    1. Copy contracts
    2. Extract unique clients
    3. Build geo tables
    4. Import limits
    5. Import users
    6. Compute snapshots
    """
    print("═══ CCC Migration ═══")

    src = sqlite3.connect(source_db)
    src.row_factory = sqlite3.Row

    # Init target
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.database import init_db, get_db
    init_db()

    tgt = get_db()

    # 1. Extract unique clients
    print("1. Extracting clients...")
    rows = src.execute("""
        SELECT client_id, full_name, gender, MAX(age) as age,
            MAX(income_source) as income_source,
            MAX(region) as region, MAX(mfy) as mfy,
            MAX(workplace) as workplace, MAX(position) as position,
            MAX(has_car) as has_car, MAX(has_card) as has_card
        FROM contracts
        WHERE client_id IS NOT NULL
        GROUP BY client_id, full_name
    """).fetchall()

    client_map = {}  # old_client_id → new_client_id
    for r in rows:
        tgt.execute("""INSERT INTO clients
            (external_id, full_name, gender, age, income_source, region, mfy,
             workplace, position, has_car, has_card, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r['client_id'], r['full_name'], r['gender'], r['age'],
             r['income_source'], r['region'], r['mfy'],
             r['workplace'], r['position'],
             1 if r['has_car'] else 0, 1 if r['has_card'] else 0,
             '2026-03-01'))
        client_map[r['client_id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]

    print(f"   {len(client_map):,} clients created")

    # 2. Copy contracts with proper client_id
    print("2. Migrating contracts...")
    contracts = src.execute("SELECT * FROM contracts").fetchall()
    count = 0
    for c in contracts:
        new_cid = client_map.get(c['client_id'])
        if not new_cid:
            continue
        tgt.execute("""INSERT INTO contracts
            (client_id, external_id, branch, contract_date, contract_end_date,
             contract_term, interest_rate, status, status_detail,
             product_amount, advance_payment, monthly_payment,
             paid_amount, debt_amount, overdue_amount,
             late_count, total_late_days, last_payment_date,
             responsible_person, imported_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_cid, c['contract_id'], c['branch'],
             c['contract_date'], c['contract_end_date'],
             c['contract_term'], c['interest_rate'],
             c['status'], c['status_detail'],
             c['product_amount'], c['advance_payment'], c['monthly_payment'],
             c['paid_amount'], c['debt_amount'], c['overdue_amount'],
             c['late_count'], c['total_late_days'], c['last_payment_date'],
             c['responsible_person'], c['imported_at']))
        count += 1
        if count % 10000 == 0:
            tgt.commit()
            print(f"   ... {count:,}")

    tgt.commit()
    print(f"   {count:,} contracts migrated")

    # 3. Build geo tables
    print("3. Building geo hierarchy...")
    regions = src.execute("""
        SELECT DISTINCT region FROM contracts
        WHERE region IS NOT NULL AND region != ''
        ORDER BY region
    """).fetchall()

    for r in regions:
        tgt.execute("INSERT OR IGNORE INTO geo_regions (name) VALUES (?)", (r['region'],))

    tgt.commit()
    region_map = {}
    for r in tgt.execute("SELECT id, name FROM geo_regions").fetchall():
        region_map[r['name']] = r['id']

    # MFY per region
    mfy_data = src.execute("""
        SELECT DISTINCT region, mfy FROM contracts
        WHERE region IS NOT NULL AND mfy IS NOT NULL
        AND region != '' AND mfy != ''
    """).fetchall()

    for m in mfy_data:
        rid = region_map.get(m['region'])
        if rid:
            tgt.execute("INSERT OR IGNORE INTO geo_mfy (region_id, name) VALUES (?, ?)",
                (rid, m['mfy']))

    tgt.commit()
    mfy_count = tgt.execute("SELECT COUNT(*) FROM geo_mfy").fetchone()[0]
    print(f"   {len(region_map)} regions, {mfy_count:,} MFY")

    # 4. Copy users
    print("4. Migrating users...")
    try:
        users = src.execute("SELECT * FROM users WHERE active=1").fetchall()
        for u in users:
            tgt.execute("INSERT OR IGNORE INTO users (username,password_hash,name,role,branch,is_active,created_at) VALUES (?,?,?,?,?,1,?)",
                (u['username'], u['password_hash'], u['name'], u['role'], u['branch'], '2026-03-01'))
        tgt.commit()
        print(f"   {len(users)} users")
    except Exception as e:
        print(f"   Users skip: {e}")

    # 5. Copy limits
    print("5. Migrating limits...")
    try:
        limits = src.execute("SELECT * FROM max_limits").fetchall()
        for l in limits:
            tgt.execute("INSERT INTO max_limits (client_type,income_source,contracts_count,max_product_amount,max_total_debt,max_overdue_days) VALUES (?,?,?,?,?,?)",
                (l['client_type'], l['income_source'], l['contracts_count'],
                 l['max_product_amount'], l['max_total_debt'], l['max_overdue_days']))
        tgt.commit()
        print(f"   {len(limits)} limit rules")
    except Exception as e:
        print(f"   Limits skip: {e}")

    # 6. Compute initial snapshot
    print("6. Computing analytics snapshot...")
    from engines.analytics_engine import compute_daily_snapshot
    compute_daily_snapshot()

    # Stats
    final_clients = tgt.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    final_contracts = tgt.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
    final_regions = tgt.execute("SELECT COUNT(*) FROM geo_regions").fetchone()[0]

    src.close()
    tgt.close()

    print(f"\n✅ Миграция завершена!")
    print(f"   Клиентов: {final_clients:,}")
    print(f"   Договоров: {final_contracts:,}")
    print(f"   Регионов: {final_regions}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True, help="Old CCC credit_control.db")
    p.add_argument("--target", default=None, help="New DB path (default: from config)")
    args = p.parse_args()

    if args.target:
        from config import settings
        settings.DB_PATH = args.target

    migrate(args.source, settings.DB_PATH if not args.target else args.target)
