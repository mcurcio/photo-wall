"""Transactional dispatch port for centrally owned media preparation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import procrastinate
import psycopg

PREPARE_MEDIA_TASK = "photo_wall.media.prepare"
REFRESH_MEDIA_SOURCE_TASK = "photo_wall.media.refresh_source"
MEDIA_QUEUE = "photo-wall-media"
MEDIA_STORAGE_LOCK = "photo-wall-media-storage"
MEDIA_REFRESH_LOCK_PREFIX = "photo-wall-media-refresh:"
_SCHEMA_LOCK = 734118326


class MediaTaskQueue(Protocol):
    def enqueue_in(self, conn: Any, job_id: str) -> int: ...
    def enqueue_refresh_in(self, conn: Any, source_ref: str) -> "QueueReceipt": ...


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
