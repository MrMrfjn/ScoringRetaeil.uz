# PostgreSQL Migration Guide

## Overview

The platform currently uses SQLite for simplicity and zero-config deployment.
For production scaling (multi-tenant SaaS, heavy analytics, concurrent writes),
PostgreSQL is the recommended target.

## Migration Strategy

### Phase 1: Compatibility Layer

All database access goes through `core/database.py:get_db()`. To switch to PostgreSQL:

1. Add `psycopg2-binary` to `requirements.txt`.
2. Modify `get_db()` to return a PostgreSQL connection when `DATABASE_URL` is set.
3. SQLite remains the default for local development.

```python
# core/database.py — proposed change
def get_db():
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if db_url.startswith("postgresql://"):
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(db_url)
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        return conn
    # fallback: SQLite
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn
```

### Phase 2: Schema Migration

Use `scripts/init_postgres.sql` (already present) as the base schema.
Key differences from SQLite:

| SQLite | PostgreSQL |
|--------|------------|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `SERIAL PRIMARY KEY` |
| `TEXT` for dates | `TIMESTAMP` or `DATE` |
| `strftime('%Y', col)` | `EXTRACT(YEAR FROM col)` |
| `LIKE` (case-sensitive) | `ILIKE` (case-insensitive) |
| `REPLACE()` | `REPLACE()` (same) |

### Phase 3: Query Compatibility

The main SQL patterns to update:
- `strftime()` calls in analytics queries → `TO_CHAR()` or `EXTRACT()`.
- `date('now')` → `CURRENT_DATE`.
- `PRAGMA` calls → remove or wrap in SQLite-only blocks.

### Phase 4: Connection Pooling

For production PostgreSQL, use a connection pool:

```python
from psycopg2 import pool
db_pool = pool.ThreadedConnectionPool(2, 10, os.environ["DATABASE_URL"])
```

### Environment Variables

```
DATABASE_URL=postgresql://user:password@host:5432/ccc_production
```

When `DATABASE_URL` is set, it takes precedence over `DB_PATH`.

### Testing

Run the full test suite against both backends:

```bash
# SQLite (default)
pytest tests/ -v

# PostgreSQL
DATABASE_URL=postgresql://test:test@localhost:5432/ccc_test pytest tests/ -v
```

### Docker Compose (PostgreSQL variant)

Add to `docker-compose.yml`:

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: ccc_production
      POSTGRES_USER: ccc
      POSTGRES_PASSWORD: ${PG_PASSWORD}
    volumes:
      - pg_data:/var/lib/postgresql/data
      - ./scripts/init_postgres.sql:/docker-entrypoint-initdb.d/01-schema.sql
    ports:
      - "5432:5432"

  backend:
    environment:
      DATABASE_URL: postgresql://ccc:${PG_PASSWORD}@postgres:5432/ccc_production
```

### Rollback

Keep `DB_PATH` support indefinitely. If PostgreSQL is unavailable, the app
falls back to SQLite automatically (useful for development and testing).
