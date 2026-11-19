"""Central's migrations run without a request's limits (PR 156's end review).

A request's transaction cancels a statement after 10 s and gives up a lock after 5 s
(`central/db.py` `REQUEST_STATEMENT_TIMEOUT`, `REQUEST_LOCK_TIMEOUT`). Migration 119 copies the
whole bus record into `events` in one statement, which takes longer than 10 s at production's
size: under the request limit the whole chain rolled back and Central could not start. And the
web process and the worker both migrate at start: the second waits on the migration lock for as
long as the first migrates, past 5 s. Here the request limits shrink to 1 s, so a migration
slower than that proves the run is not bound by them without a slow test. A migration that sets
its own limit (113 ends with `SET LOCAL statement_timeout = '10s'`) bounds only itself, never the
migrations after it in the same run.

Mutation probes: drop the migration's `SET LOCAL statement_timeout` -> the slow migration is
cancelled; set the limits once per run instead of before each migration -> the migration after
the one that sets its own limit is cancelled; drop its `SET LOCAL lock_timeout` (or set it after the advisory lock) -> the second
starter's wait is refused.
"""
from __future__ import annotations

import threading

import psycopg
import pytest

import central.db as central_db
from central.db import Database

pytestmark = pytest.mark.db

MIGRATION_LOCK = 734118321


@pytest.fixture
def short_request_limits(database_provisioner, monkeypatch, tmp_path):
    """A migrated database's conninfo, then the request limits at 1 s and the runner reading
    `tmp_path`'s migrations. The clone is made first: the shared template is migrated by the real
    runner, never by the patched one."""
    with database_provisioner.migrated() as conninfo:
        monkeypatch.setattr(central_db, "REQUEST_STATEMENT_TIMEOUT", "1s")
        monkeypatch.setattr(central_db, "REQUEST_LOCK_TIMEOUT", "1s")
        monkeypatch.setattr(central_db, "MIGRATIONS", tmp_path)
        yield conninfo, tmp_path


def _applied(db: Database) -> set[str]:
    with db.transaction() as conn:
        return {row["name"] for row in conn.execute("SELECT name FROM schema_migrations")}


def test_a_migration_slower_than_a_request_still_applies(short_request_limits):
    conninfo, migrations = short_request_limits
    (migrations / "998_own_limit.sql").write_text("SET LOCAL statement_timeout = '1s';\n")
    (migrations / "999_slow.sql").write_text("SELECT pg_sleep(1.5);\nCREATE TABLE slow_done (id INT);\n")
    db = Database(conninfo, application_name="photo-wall-test")
    try:
        db.migrate()
        applied = _applied(db)
        with pytest.raises(psycopg.errors.QueryCanceled), db.transaction() as conn:
            conn.execute("SELECT pg_sleep(1.5)")   # a request is still bound by its limit
    finally:
        db.close()

    assert {"998_own_limit.sql", "999_slow.sql"} <= applied


def test_a_second_starter_waits_for_the_first_ones_migration(short_request_limits):
    conninfo, migrations = short_request_limits
    (migrations / "999_after_wait.sql").write_text("CREATE TABLE after_wait (id INT);\n")
    db = Database(conninfo, application_name="photo-wall-test")
    first = psycopg.connect(conninfo, autocommit=True)
    try:
        first.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK,))   # the first starter migrates
        threading.Timer(1.5, lambda: first.execute(
            "SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))).start()
        db.migrate()
        applied = _applied(db)
    finally:
        first.close()
        db.close()

    assert "999_after_wait.sql" in applied
