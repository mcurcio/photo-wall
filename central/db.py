"""PostgreSQL transaction boundary and forward-only, checksum-verified migrations."""

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

MEDIA_LOCK = 734118325


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn

    @contextmanager
    def transaction(self):
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5) as conn:
            conn.execute("SET LOCAL lock_timeout='5s'")
            conn.execute("SET LOCAL statement_timeout='10s'")
            yield conn

    def migrate(self) -> None:
        with self.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(734118321)")
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            for path in sorted(Path(__file__).with_name("migrations").glob("*.sql")):
                sql = path.read_text()
                digest = sha256(sql.encode()).hexdigest()
                old = conn.execute(
                    "SELECT sha256 FROM schema_migrations WHERE name=%s", (path.name,)
                ).fetchone()
                if old:
                    if old["sha256"] != digest:
                        raise RuntimeError(f"migration checksum changed: {path.name}")
                    continue
                conn.execute(sql)
                conn.execute("INSERT INTO schema_migrations(name,sha256) VALUES(%s,%s)",
                             (path.name, digest))

    def healthy(self) -> bool:
        with self.transaction() as conn:
            return conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
