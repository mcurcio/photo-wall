"""A lock that times out is a retryable refusal, and its holder is named in the log (issue #127).

On 2026-10-20 Central answered node requests with raw HTTP 500 for hours: every traceback was
`psycopg.errors.LockNotAvailable` from a `lock_timeout`, nothing mapped it to a status, and nobody
could tell who held the lock (PostgreSQL's own `log_lock_waits` is off in production and needs the
server's configuration, outside this repository).

Rules this module owns (one home each):
- **The refusal**: any request whose transaction hit `LockNotAvailable` is answered 503 with
  `{"error": "lock_busy"}` and `Retry-After: LOCK_RETRY_AFTER_SECONDS`, on every route of the app
  it is mounted on (operator and node alike), never a raw 500.
- **Naming the holder**: before answering, the holders of every advisory lock someone waits for
  are read from `pg_locks` joined with `pg_stat_activity` on a connection of their own and logged
  once (pid, application, transaction age, state, the first LOCK_QUERY_CHARS of the query).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from fastapi import FastAPI

LOCK_RETRY_AFTER_SECONDS: Final = 1
LOCK_QUERY_CHARS: Final = 200
LOCK_BUSY: Final = "lock_busy"


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
    """Every backend holding a granted advisory lock that some other backend waits for (pg_locks
    granted rows matching an ungranted row's locktype, classid, objid and objsubid), with its
    pg_stat_activity facts; read in the caller's (fresh, autocommit) connection. Never raises for
    an empty answer."""
    ...


def mount_lock_refusal(app: FastAPI, db: Any) -> None:
    """Register the one `LockNotAvailable` handler on `app`: log `lock_holders_in` (on a connection
    of its own from `db`; a failure to read them is logged, never raised), then answer 503
    `{"error": LOCK_BUSY}` with `Retry-After`. Mounted once in central/app.py `create_app`."""
    ...


__all__ = ["LOCK_BUSY", "LOCK_QUERY_CHARS", "LOCK_RETRY_AFTER_SECONDS", "LockHolder", "lock_holders_in",
           "mount_lock_refusal"]
