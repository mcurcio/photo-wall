"""Transactional dispatch port for the media worker's library checks and measurements
(media variants are prepared by `PrepareMedia` on the job runtime)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import procrastinate
import psycopg

# Roadmap 4 (run ledger .claude/runs/photo-4.md): a connection check, and one bounded step of a
# pool's check (each step queues the next through the store, in the step's own transaction).
CHECK_CONNECTION_TASK = "photo_wall.library.check_connection"
CHECK_POOL_TASK = "photo_wall.library.check_pool"
LIBRARY_CONNECTION_LOCK_PREFIX = "photo-wall-library-connection:"
LIBRARY_POOL_LOCK_PREFIX = "photo-wall-library-pool:"
# Roadmap 5a (run ledger .claude/runs/photo-5a.md, slice M1): measure one photo's key content. Its
# queueing lock (prefix + asset id) keeps one queued measurement per photo; it takes no lock, so
# it runs beside preparation and the library checks.
MEASURE_KEY_CONTENT_TASK = "photo_wall.media.measure"
KEY_CONTENT_QUEUE_PREFIX = "photo-wall-measure:"
MEDIA_QUEUE = "photo-wall-media"
MEDIA_STORAGE_LOCK = "photo-wall-media-storage"
_SCHEMA_LOCK = 734118326


class MediaTaskQueue(Protocol):
    def enqueue_connection_check_in(self, conn: Any, connection_ref: str) -> "QueueReceipt": ...
    def enqueue_pool_check_in(self, conn: Any, pool_key: str) -> "QueueReceipt": ...
    def enqueue_measure_in(self, conn: Any, asset_id: str) -> "QueueReceipt": ...


@dataclass(frozen=True, slots=True)
class QueueReceipt:
    coalesced: bool


class ProcrastinateMediaQueue:
    """Defer library checks and measurements through the caller's psycopg transaction."""

    def __init__(self, dsn: str):
        connector = procrastinate.SyncPsycopgConnector(conninfo=dsn)
        self.app = procrastinate.App(connector=connector)

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

    def enqueue_measure_in(self, conn: Any, asset_id: str) -> QueueReceipt:
        """Queue MEASURE_KEY_CONTENT_TASK(asset_id=...) on MEDIA_QUEUE in the caller's
        transaction, queueing lock KEY_CONTENT_QUEUE_PREFIX + asset id and no lock (slice M1); an
        already queued measurement coalesces."""
        return self._defer_coalescing(conn, MEASURE_KEY_CONTENT_TASK,
                                      KEY_CONTENT_QUEUE_PREFIX + asset_id, exclusive=False,
                                      asset_id=asset_id)

    def _defer_coalescing(self, conn: Any, task: str, lock: str, *, exclusive: bool = True,
                          **arguments: str) -> QueueReceipt:
        """Defer `task` once per `lock`: a queued (not yet running) duplicate coalesces; with
        `exclusive` it also runs one at a time per `lock`. The savepoint keeps the caller's
        transaction usable after Procrastinate's refusal."""
        try:
            with conn.transaction():
                self.app.configure_task(task, queue=MEDIA_QUEUE,
                                        lock=lock if exclusive else None, queueing_lock=lock,
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
