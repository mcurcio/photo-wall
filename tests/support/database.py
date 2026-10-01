"""Disposable PostgreSQL databases for tests: one migrated template, one clone per test.

The prior art is pgtestdb and integresql. A template named by the hash of everything that
shapes the migrated schema (`central/migrations/*.sql` and the runner in `central/db.py`) is
built once per server, by whichever worker gets there first; every test then clones it with
`CREATE DATABASE ... TEMPLATE` (milliseconds) instead of running the migrations itself, and
drops its clone after. So:

* a stale template cannot be used: a changed migration or runner names a different template;
* a half-built template cannot be used: the build runs under `<name>_building` and is renamed
  only once complete, then marked `IS_TEMPLATE` and closed to connections, so no client can
  hold a connection that blocks a clone;
* tests cannot see each other: each owns a whole database, so its advisory locks, its
  `public` schema and its Procrastinate install are its own.

`PHOTO_WALL_TEST_DATABASE_URL` names the server (any database on it with `CREATEDB`). Without
it a database test skips, unless `PHOTO_WALL_TEST_REQUIRE_DATABASE=1` (every CI job), where it
fails: a test that reaches a database where none is provided must never read as a pass.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

import central.db
from central.db import Database

DSN_VARIABLE = "PHOTO_WALL_TEST_DATABASE_URL"
REQUIRE_VARIABLE = "PHOTO_WALL_TEST_REQUIRE_DATABASE"

# Serializes template builds across workers and concurrent runs on one server (advisory locks
# are per database: every run takes it in the database its DSN names). Distinct from the
# production advisory locks (7341183xx).
TEMPLATE_BUILD_LOCK = 734118390

# Captured at import: a test may point `central.db.__file__` at a historical migration set.
_RUNNER = Path(central.db.__file__)
CLONE_PREFIX = "pw_t_"
# Another run's clone that nobody is connected to is a crash leftover once it is this old;
# a younger one may belong to a concurrent run between its CREATE and its first connection.
ABANDONED_AFTER_SECONDS = 3600
_CLONE = re.compile(r"pw_t_([0-9a-f]{12})_([0-9a-f]{8})_[0-9a-f]{12}")


def server_dsn() -> str:
    """The test server's DSN; skips without one, or fails where one is required."""
    dsn = os.environ.get(DSN_VARIABLE)
    if dsn:
        return dsn
    if os.environ.get(REQUIRE_VARIABLE) == "1":
        pytest.fail(f"this test needs PostgreSQL and {REQUIRE_VARIABLE}=1, but {DSN_VARIABLE} "
                    "is unset", pytrace=False)
    pytest.skip(f"set {DSN_VARIABLE} for real PostgreSQL integration")


def template_name() -> str:
    """`pw_tmpl_<hash>` of every input that shapes the migrated schema."""
    digest = sha256(_RUNNER.read_bytes())
    for path in sorted(_RUNNER.with_name("migrations").glob("*.sql")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return "pw_tmpl_" + digest.hexdigest()[:16]


class TestDatabases:
    """One run's (one xdist worker's) access to the test server."""

    __test__ = False  # not a test class, despite the name

    def __init__(self, dsn: str, run_id: str) -> None:
        self.dsn = dsn
        self.run = run_id[:12]
        self._admin = psycopg.connect(dsn, autocommit=True)
        self._template: str | None = None
        self._sweep()

    def close(self) -> None:
        self._admin.close()

    @contextmanager
    def migrated(self) -> Iterator[str]:
        """The conninfo of a fresh database holding every migration, dropped after."""
        with self._created(self._ensure_template()) as conninfo:
            yield conninfo

    @contextmanager
    def empty(self) -> Iterator[str]:
        """The conninfo of a fresh database with no migration applied, dropped after."""
        with self._created("template0") as conninfo:
            yield conninfo

    @contextmanager
    def _created(self, template: str) -> Iterator[str]:
        name = f"{CLONE_PREFIX}{self.run}_{int(time.time()):08x}_{uuid.uuid4().hex[:12]}"
        self._admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(name), sql.Identifier(template)))
        try:
            yield make_conninfo(self.dsn, dbname=name)
        finally:
            # FORCE: a connection the test leaked (a pool, a server thread) must not keep it.
            self._admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)))

    def _ensure_template(self) -> str:
        if self._template is None:
            name = template_name()
            if not self._published(name):
                self._admin.execute("SELECT pg_advisory_lock(%s)", (TEMPLATE_BUILD_LOCK,))
                try:
                    if not self._published(name):
                        self._build(name)
                finally:
                    self._admin.execute("SELECT pg_advisory_unlock(%s)", (TEMPLATE_BUILD_LOCK,))
            self._template = name
        return self._template

    def _published(self, name: str) -> bool:
        return self._admin.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s AND datistemplate AND NOT datallowconn",
            (name,)).fetchone() is not None

    def _build(self, name: str) -> None:
        """Under the build lock: migrate `<name>_building`, rename it, then publish it."""
        exists = self._admin.execute("SELECT 1 FROM pg_database WHERE datname=%s",
                                     (name,)).fetchone() is not None
        if not exists:  # else a build finished its rename and stopped before publishing
            building = name + "_building"
            self._admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(building)))
            self._admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(building)))
            database = Database(make_conninfo(self.dsn, dbname=building))
            try:
                database.migrate()
            finally:
                database.close()
            self._admin.execute(sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                sql.Identifier(building), sql.Identifier(name)))
        self._admin.execute(sql.SQL(
            "ALTER DATABASE {} WITH IS_TEMPLATE true ALLOW_CONNECTIONS false").format(
            sql.Identifier(name)))

    def _sweep(self) -> None:
        """Drop crash leftovers: other runs' clones, long abandoned, that nobody is using."""
        idle = self._admin.execute(
            "SELECT datname FROM pg_database d WHERE datname LIKE 'pw\\_t\\_%' AND NOT EXISTS "
            "(SELECT 1 FROM pg_stat_activity a WHERE a.datname = d.datname)").fetchall()
        for (name,) in idle:
            match = _CLONE.fullmatch(name)
            if (match is None or match[1] == self.run
                    or time.time() - int(match[2], 16) < ABANDONED_AFTER_SECONDS):
                continue
            try:  # without FORCE: a run that just connected keeps its database
                self._admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(name)))
            except psycopg.errors.ObjectInUse:
                pass



def waiting_backends(conn, *, advisory_lock: int | None = None) -> int:
    """How many backends of THIS test's database wait on a lock (on `advisory_lock` only, when
    given): the one way a test observes a lock wait. `pg_locks` is cluster-wide, so a raw count
    also sees other tests' waiters (other xdist workers' databases) and passes although this
    test's session never waited. tests/test_lock_observation_scope.py forbids raw queries."""
    query = ("SELECT count(*) AS n FROM pg_locks WHERE NOT granted AND pid IN "
             "(SELECT pid FROM pg_stat_activity WHERE datname = current_database())")
    params: tuple[int, ...] = ()
    if advisory_lock is not None:
        query += " AND locktype = 'advisory' AND objid = %s"
        params = (advisory_lock,)
    row = conn.execute(query, params).fetchone()
    return row["n"] if isinstance(row, dict) else row[0]
