"""Transactional dispatch port for the media worker's library checks and measurements
(media variants are prepared by `PrepareMedia` on the job runtime)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import procrastinate
import psycopg
from procrastinate import sql

from central.db import with_application_name

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
    """`due_at` (epoch seconds, the database's clock) is when a check runs: a due check is
    deferred for its time, never found by a poll (decision 0034, S3a)."""

    def enqueue_connection_check_in(self, conn: Any, connection_ref: str, *,
                                    due_at: float) -> "QueueReceipt": ...
    def enqueue_pool_check_in(self, conn: Any, pool_key: str, *,
                              due_at: float) -> "QueueReceipt": ...
    def enqueue_measure_in(self, conn: Any, asset_id: str) -> "QueueReceipt": ...


@dataclass(frozen=True, slots=True)
class QueueReceipt:
    coalesced: bool


class ProcrastinateMediaQueue:
    """Defer library checks and measurements through the caller's psycopg transaction."""

    def __init__(self, dsn: str):
        connector = procrastinate.SyncPsycopgConnector(
            conninfo=with_application_name(dsn, "photo-wall-media-defer"))
        self.app = procrastinate.App(connector=connector)

    def enqueue_connection_check_in(self, conn: Any, connection_ref: str, *,
                                    due_at: float) -> QueueReceipt:
        """Queue CHECK_CONNECTION_TASK(connection_ref=...) on MEDIA_QUEUE for `due_at` in the
        caller's transaction, lock and queueing lock LIBRARY_CONNECTION_LOCK_PREFIX + ref: one
        check queued and one running per connection (`_defer_check`)."""
        return self._defer_check(conn, CHECK_CONNECTION_TASK,
                                 LIBRARY_CONNECTION_LOCK_PREFIX + connection_ref, due_at,
                                 connection_ref=connection_ref)

    def enqueue_pool_check_in(self, conn: Any, pool_key: str, *, due_at: float) -> QueueReceipt:
        """Queue CHECK_POOL_TASK(pool_key=...) as `enqueue_connection_check_in`, with
        LIBRARY_POOL_LOCK_PREFIX + pool key: one step of a pool runs at a time, and a step
        already waiting absorbs a second request."""
        return self._defer_check(conn, CHECK_POOL_TASK, LIBRARY_POOL_LOCK_PREFIX + pool_key,
                                 due_at, pool_key=pool_key)

    def _defer_check(self, conn: Any, task: str, lock: str, due_at: float,
                     **arguments: str) -> QueueReceipt:
        """Defer one check for `due_at` (`scheduled_at`). A queued check of the same lock due no
        later absorbs it (QueueReceipt(coalesced=True)), as a re-arm finding any queued check.
        A queued check due LATER is replaced: cancelled with its row deleted through
        Procrastinate's own queries on the caller's connection, then deferred anew, so "Check
        now" or a change is never held back to a check queued for its due time (a coalesced
        defer would keep the later time). Earlier always wins, so the replacement only ever
        brings a check forward."""
        at = datetime.fromtimestamp(due_at, UTC)
        receipt = self._defer_coalescing(conn, task, lock, schedule_at=at, **arguments)
        if not receipt.coalesced:
            return receipt
        connector = self.app.connector.get_sync_connector()
        queued = connector.execute_query_all_with_connection(
            conn, query=sql.queries["list_jobs"], id=None, queue_name=MEDIA_QUEUE,
            task_name=task, status="todo", lock=None, queueing_lock=lock, worker_id=None)
        later = [row["id"] for row in queued
                 if row["scheduled_at"] is not None and row["scheduled_at"] > at]
        if not later:
            return receipt
        for job_id in later:
            connector.execute_query_all_with_connection(
                conn, query=sql.queries["cancel_job"], job_id=job_id, abort=False,
                delete_job=True)
        return self._defer_coalescing(conn, task, lock, schedule_at=at, **arguments)

    def enqueue_measure_in(self, conn: Any, asset_id: str) -> QueueReceipt:
        """Queue MEASURE_KEY_CONTENT_TASK(asset_id=...) on MEDIA_QUEUE in the caller's
        transaction, queueing lock KEY_CONTENT_QUEUE_PREFIX + asset id and no lock (slice M1); an
        already queued measurement coalesces."""
        return self._defer_coalescing(conn, MEASURE_KEY_CONTENT_TASK,
                                      KEY_CONTENT_QUEUE_PREFIX + asset_id, exclusive=False,
                                      asset_id=asset_id)

    def _defer_coalescing(self, conn: Any, task: str, lock: str, *, exclusive: bool = True,
                          schedule_at: datetime | None = None,
                          **arguments: str) -> QueueReceipt:
        """Defer `task` once per `lock`: a queued (not yet running) duplicate coalesces; with
        `exclusive` it also runs one at a time per `lock`. The savepoint keeps the caller's
        transaction usable after Procrastinate's refusal."""
        try:
            with conn.transaction():
                self.app.configure_task(task, queue=MEDIA_QUEUE,
                                        lock=lock if exclusive else None, queueing_lock=lock,
                                        schedule_at=schedule_at,
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
        with psycopg.connect(with_application_name(dsn, "photo-wall-schema"),
                             autocommit=True) as lock:
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
