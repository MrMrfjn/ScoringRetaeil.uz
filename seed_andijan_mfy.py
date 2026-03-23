import sqlite3
import os


def main() -> None:
    db_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "credit_control.db"
    )
    print("Using DB:", db_path, "exists=", os.path.exists(db_path))
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Ensure region exists
    cur.execute(
        "INSERT INTO geo_regions (name) "
        "SELECT 'Андижон' "
        "WHERE NOT EXISTS (SELECT 1 FROM geo_regions WHERE TRIM(name) = 'Андижон')"
    )
    conn.commit()

    cur.execute("SELECT id FROM geo_regions WHERE TRIM(name) = 'Андижон'")
    reg_id_row = cur.fetchone()
    if not reg_id_row:
        print("Андижон region row not found after insert.")
    else:
        reg_id = reg_id_row[0]
        mfy_names = [
            "Қушчи",
            "Дурафшон",
            "Юқори Ровот",
            "Пахтакор",
            "Шўрқақир",
            "Ҳасанмерган",
            "Мингтепа",
            "Ўзбекистон",
            "Шомат",
            "Кончилар",
            "Полвонтош",
            "Марказий",
            "Тўлға",
            "Ровот",
            "Шўрқишлоқ",
            "Дўстлик",
            "Кўтарма",
            "Дукчи эшон",
            "Тош йўли",
            "Хўжаариқ",
            "Увайсий",
            "Миришкор",
            "Гар-гар",
            "Узумзор",
            "Ўқчи",
            "Оқбош",
            "Қовунчи",
            "Бозорбоши",
            "Бирлик",
            "Барҳаёт",
            "Йўламатол",
            "Ғофур Ғулом",
            "Қорақўрғон",
            "Ойбек",
            "Гулистон",
            "Эрши",
            "Найман",
            "Янги ҳаёт",
            "Бобохуросон",
            "Роҳат",
            "Шахристон",
            "Тошлоқ",
            "Наврўз",
            "Қорабоғич",
            "Қўрғонча",
            "Чилон",
            "Турон",
            "Алитепа",
        ]
        for name in mfy_names:
            cur.execute(
                "INSERT OR IGNORE INTO geo_mfy (region_id, name) VALUES (?, ?)",
                (reg_id, name),
            )
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM geo_mfy WHERE region_id = ?", (reg_id,))
        count = cur.fetchone()[0]
        print("Андижон region_id =", reg_id, "MFY count =", count)

    conn.close()


if __name__ == "__main__":
    main()

