"""PostgreSQL transaction boundary and forward-only, checksum-verified migrations."""

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from contracts.time import Clock

MEDIA_LOCK = 734118325


class TransactionClock(Protocol):
    """The time of a fact written or compared by more than one process (console DDD G11).

    Read on the caller's held connection, never on one of its own: there is no
    connection-free form, so a read inside a transaction cannot take a second pooled
    connection. Clocks compare only to themselves (R10), so every process that
    writes or compares such a time uses the same one.
    """

    def now_in(self, conn: Any) -> float: ...


class DatabaseTransactionClock:
    """PostgreSQL's clock, read on the held connection: Central and every worker process.

    `clock_timestamp()` is the statement's own time, not the transaction's start
    (`now()`), so a long transaction neither stamps nor compares a stale time. It is
    a plain SELECT, valid inside a REPEATABLE READ READ ONLY transaction.
    """

    __slots__ = ()

    def now_in(self, conn: Any) -> float:
        return float(conn.execute(
            "SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS now").fetchone()["now"])


class ProcessTransactionClock:
    """A process clock behind the same port: tests (ManualClock) only, never composed in production."""

    __slots__ = ("clock",)

    def __init__(self, clock: Clock):
        self.clock = clock

    def now_in(self, conn: Any) -> float:
        if conn is None:
            raise TypeError("now_in requires the held connection")
        return self.clock.utc()


class Database:
    """One process's PostgreSQL pool and transaction boundary.

    The pool recovers by itself once the database is back, whatever the outage's length: it
    sets no `max_waiting`. psycopg_pool takes a timed-out waiter out of its queue only when a
    connection is added or returned, and refuses a request (`TooManyRequests`) before asking
    the pool to grow once the queue holds `max_waiting`. With a cap, an outage longer than
    `reconnect_timeout` left the queue full of timed-out waiters and the pool empty: every
    request was refused, none grew the pool, and Central needed a restart (2026-10-28). Without
    one, every request waits at most `timeout` (5 s) and asks the pool to grow, so the first
    connection after the outage clears the queue. Load stays bounded by that wait and by the
    caller's own thread limit. `reconnect_timeout` is psycopg_pool's own setting (how long one
    growth attempt retries before it gives up); only tests lower it.
    """

    def __init__(self, dsn: str, *, pool_size: int = 10, reconnect_timeout: float = 300.0):
        if type(pool_size) is not int or not 1 <= pool_size <= 100:
            raise ValueError("pool_size must be between 1 and 100")
        self.dsn = dsn
        self._pool = ConnectionPool(
            dsn,
            kwargs={"row_factory": dict_row, "connect_timeout": 5},
            min_size=0,
            max_size=pool_size,
            timeout=5,
            reconnect_timeout=reconnect_timeout,
            open=False,
            name="photo-wall-central",
        )
        self._pool_lock = Lock()
        self._pool_open = False

    def open(self) -> None:
        if self._pool_open:
            return
        with self._pool_lock:
            if not self._pool_open:
                self._pool.open(wait=True, timeout=5)
                self._pool_open = True

    def close(self) -> None:
        with self._pool_lock:
            if self._pool_open:
                self._pool.close(timeout=5)
                self._pool_open = False

    @contextmanager
    def transaction(self):
        self.open()
        with self._pool.connection(timeout=5) as conn:
            conn.execute("SET LOCAL lock_timeout='5s'")
            conn.execute("SET LOCAL statement_timeout='10s'")
            yield conn

    @contextmanager
    def read_snapshot(self):
        """One READ ONLY, REPEATABLE READ transaction: every statement reads the same committed
        state and none takes a lock. `SET TRANSACTION` precedes the first data query; the
        `SET LOCAL` timeouts do not establish the snapshot."""
        with self.transaction() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield conn

    def pool_stats(self) -> dict[str, int]:
        """Return the pool's numeric counters without exposing its connection string."""

        return {key: value for key, value in self._pool.get_stats().items()
                if isinstance(key, str) and type(value) is int}

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
