"""Migrate a fresh test database through a historical prefix of `central/migrations`, so a test
can seed rows as an older Central wrote them and then run the next migrations on them."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from central.db import Database

MIGRATIONS = Path(__file__).parents[2] / "central/migrations"


def migrate_before(db: Database, stop: str) -> None:
    """Apply, in order, every migration not yet applied whose file name sorts before `stop`,
    recording each as `central.db`'s runner does (so `db.migrate()` later applies the rest)."""
    with db.transaction() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, "
                     "sha256 TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        applied = {row["name"] for row in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name >= stop:
                break
            if path.name in applied:
                continue
            text = path.read_text()
            conn.execute(text)
            conn.execute("INSERT INTO schema_migrations(name,sha256) VALUES(%s,%s)",
                         (path.name, sha256(text.encode()).hexdigest()))
