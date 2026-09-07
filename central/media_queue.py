"""Transactional dispatch port for centrally owned media preparation."""

from __future__ import annotations

from typing import Any, Protocol

import procrastinate

PREPARE_MEDIA_TASK = "photo_wall.media.prepare"
MEDIA_QUEUE = "photo-wall-media"
MEDIA_STORAGE_LOCK = "photo-wall-media-storage"


class AcquisitionQueue(Protocol):
    def enqueue_in(self, conn: Any, job_id: str) -> int: ...


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

    @classmethod
    def apply_schema(cls, dsn: str) -> None:
        """Install/upgrade Procrastinate's versioned schema."""
        queue = cls(dsn)
        queue.app.open()
        try:
            queue.app.schema_manager.apply_schema()
        finally:
            queue.app.close()
