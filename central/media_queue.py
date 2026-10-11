"""Transactional dispatch port for centrally owned media preparation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import procrastinate
import psycopg

PREPARE_MEDIA_TASK = "photo_wall.media.prepare"
# Roadmap 4 (run ledger .claude/runs/photo-4.md): a connection check, and one bounded step of a
# pool's check (each step queues the next through the store, in the step's own transaction).
CHECK_CONNECTION_TASK = "photo_wall.library.check_connection"
CHECK_POOL_TASK = "photo_wall.library.check_pool"
LIBRARY_CONNECTION_LOCK_PREFIX = "photo-wall-library-connection:"
LIBRARY_POOL_LOCK_PREFIX = "photo-wall-library-pool:"
MEDIA_QUEUE = "photo-wall-media"
MEDIA_STORAGE_LOCK = "photo-wall-media-storage"
_SCHEMA_LOCK = 734118326


class MediaTaskQueue(Protocol):
    def enqueue_in(self, conn: Any, job_id: str) -> int: ...
    def enqueue_connection_check_in(self, conn: Any, connection_ref: str) -> "QueueReceipt": ...
    def enqueue_pool_check_in(self, conn: Any, pool_key: str) -> "QueueReceipt": ...


@dataclass(frozen=True, slots=True)
class QueueReceipt:
    coalesced: bool


class ProcrastinateMediaQueue:
    """Defer preparation through the caller's psycopg transaction."""

    def __init__(self, dsn: str):
        connector = procrastinate.SyncPsycopgConnector(conninfo=dsn)
        self.app = procrastinate.App(connector=connector)

    def enqueue_in(self, conn: Any, job_id: str) -> int:
        return self.app.configure_task(
            PREPARE_MEDIA_TASK,
            queue=MEDIA_QUEUE,
            lock=MEDIA_STORAGE_LOCK,
            queueing_lock=job_id,
            connection=conn,
        ).defer(job_id=job_id)

    def enqueue_connection_check_in(self, conn: Any, connection_ref: str) -> QueueReceipt:
        """Queue CHECK_CONNECTION_TASK(connection_ref=...) on MEDIA_QUEUE in the caller's
        transaction, lock and queueing lock LIBRARY_CONNECTION_LOCK_PREFIX + ref; an already
        queued check coalesces (QueueReceipt(coalesced=True))."""
        return self._defer_coalescing(conn, CHECK_CONNECTION_TASK,
                                      LIBRARY_CONNECTION_LOCK_PREFIX + connection_ref,
                                      connection_ref=connection_ref)

    def enqueue_pool_check_in(self, conn: Any, pool_key: str) -> QueueReceipt:
        """Queue CHECK_POOL_TASK(pool_key=...) as `enqueue_connection_check_in`, with
        LIBRARY_POOL_LOCK_PREFIX + pool key: one step of a pool runs at a time, and a step
        already waiting absorbs a second request."""
        return self._defer_coalescing(conn, CHECK_POOL_TASK, LIBRARY_POOL_LOCK_PREFIX + pool_key,
                                      pool_key=pool_key)

    def _defer_coalescing(self, conn: Any, task: str, lock: str, **arguments: str) -> QueueReceipt:
        """Defer `task` once per `lock`: a queued (not yet running) duplicate coalesces. The
        savepoint keeps the caller's transaction usable after Procrastinate's refusal."""
        try:
            with conn.transaction():
                self.app.configure_task(task, queue=MEDIA_QUEUE, lock=lock, queueing_lock=lock,
                                        connection=conn).defer(**arguments)
            return QueueReceipt(coalesced=False)
        except procrastinate.exceptions.AlreadyEnqueued:
            return QueueReceipt(coalesced=True)

    @classmethod
    def apply_schema(cls, dsn: str) -> None:
        """Install Procrastinate's schema once across concurrent central starts."""
        # Procrastinate 3.9's schema is an atomic, one-time installation, but its
        # CREATE statements are intentionally not idempotent. Central and the media
        # worker can start together, so serialize the existence check and install.
        with psycopg.connect(dsn, autocommit=True) as lock:
            lock.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_LOCK,))
            try:
                installed = lock.execute(
                    "SELECT to_regclass('procrastinate_jobs') IS NOT NULL"
                ).fetchone()[0]
                if installed:
                    return
                queue = cls(dsn)
                queue.app.open()
                try:
                    queue.app.schema_manager.apply_schema()
                finally:
                    queue.app.close()
            finally:
                lock.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_LOCK,))
