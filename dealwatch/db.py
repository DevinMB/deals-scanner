"""SQLite connection and migrations.

Migrations are numbered .sql files in dealwatch/migrations, applied in order
and recorded in schema_migrations.
"""

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Autocommit mode; callers open transactions explicitly.
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations; return the names applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " name TEXT PRIMARY KEY,"
        " applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))"
    )
    done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if f.name in done:
            continue
        # executescript() commits on its own, so run statements one by one in one transaction.
        conn.execute("BEGIN")
        try:
            for stmt in _statements(f.read_text()):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (f.name,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(f.name)
    return applied


def _statements(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
