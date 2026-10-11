"""Transactional dispatch port for centrally owned media preparation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import procrastinate
import psycopg

PREPARE_MEDIA_TASK = "photo_wall.media.prepare"
REFRESH_MEDIA_SOURCE_TASK = "photo_wall.media.refresh_source"
PREVIEW_SOURCE_TASK = "photo_wall.media.preview_source"
# Roadmap 4 (run ledger .claude/runs/photo-4.md): a connection check, and one bounded step of a
# pool's check (each step queues the next through the store, in the step's own transaction).
CHECK_CONNECTION_TASK = "photo_wall.library.check_connection"
CHECK_POOL_TASK = "photo_wall.library.check_pool"
LIBRARY_CONNECTION_LOCK_PREFIX = "photo-wall-library-connection:"
LIBRARY_POOL_LOCK_PREFIX = "photo-wall-library-pool:"
MEDIA_PREVIEW_LOCK_PREFIX = "photo-wall-media-preview:"
MEDIA_QUEUE = "photo-wall-media"
MEDIA_STORAGE_LOCK = "photo-wall-media-storage"
MEDIA_REFRESH_LOCK_PREFIX = "photo-wall-media-refresh:"
_SCHEMA_LOCK = 734118326


class MediaTaskQueue(Protocol):
    def enqueue_in(self, conn: Any, job_id: str) -> int: ...
    def enqueue_refresh_in(self, conn: Any, source_ref: str) -> "QueueReceipt": ...
    def enqueue_preview_in(self, conn: Any, request_id: str) -> int: ...
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

    def enqueue_refresh_in(self, conn: Any, source_ref: str) -> QueueReceipt:
        queueing_lock = "media-refresh:" + source_ref
        try:
            # The repository holds MEDIA_LOCK until commit. A conflicting todo
            # job cannot capture a refresh revision before this request becomes
            # visible, even if Procrastinate has already marked it doing.
            with conn.transaction():
                self.app.configure_task(
                    REFRESH_MEDIA_SOURCE_TASK,
                    queue=MEDIA_QUEUE,
                    lock=MEDIA_REFRESH_LOCK_PREFIX + source_ref,
                    queueing_lock=queueing_lock,
                    connection=conn,
                ).defer(source_ref=source_ref)
            return QueueReceipt(coalesced=False)
        except procrastinate.exceptions.AlreadyEnqueued:
            return QueueReceipt(coalesced=True)

    def enqueue_preview_in(self, conn: Any, request_id: str) -> int:
        return self.app.configure_task(
            PREVIEW_SOURCE_TASK,
            queue=MEDIA_QUEUE,
            lock=MEDIA_PREVIEW_LOCK_PREFIX + request_id,
            queueing_lock=MEDIA_PREVIEW_LOCK_PREFIX + request_id,
            connection=conn,
        ).defer(request_id=request_id)

    def enqueue_connection_check_in(self, conn: Any, connection_ref: str) -> QueueReceipt:
        """Queue CHECK_CONNECTION_TASK(connection_ref=...) on MEDIA_QUEUE in the caller's
        transaction, lock and queueing lock LIBRARY_CONNECTION_LOCK_PREFIX + ref; an already
        queued check coalesces (QueueReceipt(coalesced=True)). Slice C1."""
        raise NotImplementedError("slice C1")

    def enqueue_pool_check_in(self, conn: Any, pool_key: str) -> QueueReceipt:
        """Queue CHECK_POOL_TASK(pool_key=...) as `enqueue_connection_check_in`, with
        LIBRARY_POOL_LOCK_PREFIX + pool key. Slice C2."""
        raise NotImplementedError("slice C2")

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
