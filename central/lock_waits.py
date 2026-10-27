"""A lock that times out is a retryable refusal, and its holder is named in the log (issue #127).

On 2026-10-20 Central answered node requests with raw HTTP 500 for hours: every traceback was
`psycopg.errors.LockNotAvailable` from a `lock_timeout`, nothing mapped it to a status, and nobody
could tell who held the lock (PostgreSQL's own `log_lock_waits` is off in production and needs the
server's configuration, outside this repository).

Rules this module owns (one home each):
- **The refusal**: any request whose transaction hit `LockNotAvailable` is answered 503 with
  `{"error": "lock_busy"}` and `Retry-After: LOCK_RETRY_AFTER_SECONDS`, on every route of the app
  it is mounted on (operator and node alike), never a raw 500.
- **Naming the holder**: before answering, the holders of Central's own advisory locks
  (Coordination, Runtime and every Node lock, central/transaction_locks.py) and of every other
  advisory lock someone still waits for are read from `pg_locks` joined with `pg_stat_activity` on
  a connection of their own and logged once (pid, application, transaction age, state, the first
  LOCK_QUERY_CHARS of the query). The request that timed out has already rolled back, so it is no
  longer a waiter: a holder is named even when no other request waits. A row lock's holder is not
  named (PostgreSQL lists a row lock only while someone waits on it; `log_lock_waits` is the
  server's setting, outside this repository).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Final

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row

from central.transaction_locks import COORDINATION_LOCK, NODE_LOCK_CLASS, RUNTIME_LOCK

LOCK_RETRY_AFTER_SECONDS: Final = 1
LOCK_QUERY_CHARS: Final = 200
LOCK_BUSY: Final = "lock_busy"

log = logging.getLogger(__name__)

# The holders, in this database, of Central's own advisory locks (Coordination and Runtime, one-key
# form: classid 0, objsubid 1; a Node lock, two-key form: classid NODE_LOCK_CLASS, objsubid 2) and
# of any other advisory lock another backend waits for, the reading backend itself excluded.
_HOLDERS_SQL = (
    "SELECT DISTINCT h.pid, a.application_name, "
    "EXTRACT(EPOCH FROM (now() - a.xact_start))::float8 AS transaction_seconds, "
    "a.state, left(a.query, %(chars)s) AS query "
    "FROM pg_locks h JOIN pg_stat_activity a ON a.pid=h.pid "
    "WHERE h.locktype='advisory' AND h.granted AND h.pid<>pg_backend_pid() "
    "AND h.database=(SELECT oid FROM pg_database WHERE datname=current_database()) "
    "AND ((h.classid=0 AND h.objsubid=1 AND h.objid=ANY(%(central)s::oid[])) "
    "OR (h.classid=%(node)s::oid AND h.objsubid=2) "
    "OR EXISTS (SELECT 1 FROM pg_locks w WHERE w.locktype='advisory' AND NOT w.granted "
    "AND w.database=h.database AND w.classid=h.classid AND w.objid=h.objid "
    "AND w.objsubid=h.objsubid AND w.pid<>h.pid)) ORDER BY h.pid"
)


@dataclass(frozen=True)
class LockHolder:
    """One backend holding an advisory lock that another backend waits for: its pid, its
    application_name, how long its transaction has run (seconds, PostgreSQL's clock), its state and
    the start of its current query."""
    pid: int
    application: str
    transaction_seconds: float | None
    state: str | None
    query: str


def lock_holders_in(conn: Any) -> tuple[LockHolder, ...]:
    """Every other backend in this database holding Coordination, Runtime or a Node lock, or a
    granted advisory lock some other backend waits for (pg_locks granted rows matching an ungranted
    row's classid, objid and objsubid), with its pg_stat_activity facts; read in the caller's
    (fresh, autocommit) connection. Never raises for an empty answer."""
    rows = conn.execute(_HOLDERS_SQL, {"chars": LOCK_QUERY_CHARS,
                                       "central": [COORDINATION_LOCK, RUNTIME_LOCK],
                                       "node": NODE_LOCK_CLASS}).fetchall()
    return tuple(
        LockHolder(pid=row["pid"], application=row["application_name"] or "",
                   transaction_seconds=row["transaction_seconds"], state=row["state"],
                   query=row["query"] or "")
        for row in rows
    )


def _log_holders(db: Any) -> None:
    """Name the holders in the log, on a connection of its own (the failed transaction's is
    aborted). Reading them is best effort: a failure is logged and never hides the refusal."""
    try:
        with psycopg.connect(db.dsn, autocommit=True, row_factory=dict_row,
                             connect_timeout=5) as conn:
            holders = lock_holders_in(conn)
    except Exception:
        log.exception("lock_busy: could not read the lock holders")
        return
    log.warning("lock_busy: a lock wait timed out; holders: %s",
                "; ".join(f"pid={h.pid} application={h.application!r} "
                          f"transaction_seconds={h.transaction_seconds} state={h.state} "
                          f"query={h.query!r}" for h in holders) or "none still held")


def mount_lock_refusal(app: FastAPI, db: Any) -> None:
    """Register the one `LockNotAvailable` handler on `app`: log `lock_holders_in` (on a connection
    of its own from `db`; a failure to read them is logged, never raised), then answer 503
    `{"error": LOCK_BUSY}` with `Retry-After`. Mounted once in central/app.py `create_app`."""

    @app.exception_handler(psycopg.errors.LockNotAvailable)
    async def lock_busy(request, exc):
        await asyncio.to_thread(_log_holders, db)
        return JSONResponse({"error": LOCK_BUSY}, status_code=503,
                            headers={"Retry-After": str(LOCK_RETRY_AFTER_SECONDS)})


__all__ = ["LOCK_BUSY", "LOCK_QUERY_CHARS", "LOCK_RETRY_AFTER_SECONDS", "LockHolder", "lock_holders_in",
           "mount_lock_refusal"]
