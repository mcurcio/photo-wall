"""Procrastinate task definitions for the central media worker."""

from __future__ import annotations

from typing import Any, Final

import procrastinate

from central.infra.job_queue import async_connector
from central.media_queue import (
    CHECK_CONNECTION_TASK,
    CHECK_POOL_TASK,
    LIBRARY_CONNECTION_LOCK_PREFIX,
    LIBRARY_POOL_LOCK_PREFIX,
    MEDIA_QUEUE,
    MEDIA_STORAGE_LOCK,
    PREPARE_MEDIA_TASK,
)

# The library tick: due connection and pool checks are queued every 30 seconds (each is due once
# CHECK_EVERY_SECONDS have passed, central/photo_library/policy.py).
REFRESH_CRON = "* * * * * */30"
MAINTENANCE_MINUTES: Final = 5
MAINTENANCE_CRON = f"*/{MAINTENANCE_MINUTES} * * * * 0"
# The guaranteed check-in (`worker_status`) cadence: boot plus every maintenance pass.
WORKER_CHECK_IN_SECONDS: Final = MAINTENANCE_MINUTES * 60
# A check-in older than this marks the worker unhealthy (`media.healthcheck`). It is the
# console's worker-quiet window (central/console/src/mediaHealth.js WORKER_QUIET_AFTER: two
# check-in intervals plus a minute); tests/test_media_queue.py pins the two equal. Cost: a hung
# worker reads healthy for up to 11 minutes (Compose consumes worker health only for `--wait`).
WORKER_FRESH_SECONDS: Final = 2 * WORKER_CHECK_IN_SECONDS + 60


class MediaTaskFailed(RuntimeError):
    """A bounded media failure safe to expose to the queue journal."""


class RetryableMediaTask(MediaTaskFailed):
    """A media failure that Procrastinate may retry."""


class MediaRetryStrategy(procrastinate.BaseRetryStrategy):
    delays = (5, 15, 60)

    def get_retry_decision(self, *, exception: BaseException, job: Any):
        attempt = max(1, job.attempts)
        if not isinstance(exception, RetryableMediaTask) or attempt > len(self.delays):
            return None
        return procrastinate.RetryDecision(
            retry_in={"seconds": self.delays[attempt - 1]},
        )


class ProcrastinateCheckQueue:
    """`media.library_checks.LibraryCheckQueue` over this app: one queued check per connection
    or pool at a time (an already queued one coalesces); a running one holds the lock."""

    def __init__(self, app: procrastinate.App) -> None:
        self.app = app

    async def _defer(self, task: str, lock: str, **arguments: str) -> None:
        try:
            await self.app.configure_task(task, queue=MEDIA_QUEUE, lock=lock,
                                          queueing_lock=lock).defer_async(**arguments)
        except procrastinate.exceptions.AlreadyEnqueued:
            pass

    async def connection(self, connection_ref: str) -> None:
        await self._defer(CHECK_CONNECTION_TASK, LIBRARY_CONNECTION_LOCK_PREFIX + connection_ref,
                          connection_ref=connection_ref)

    async def pool(self, pool_key: str) -> None:
        await self._defer(CHECK_POOL_TASK, LIBRARY_POOL_LOCK_PREFIX + pool_key, pool_key=pool_key)


def create_worker_app(dsn: str) -> procrastinate.App:
    """Build the async worker app; runtime collaborators arrive via context."""
    app = procrastinate.App(
        connector=async_connector(dsn),
    )

    @app.task(
        name=PREPARE_MEDIA_TASK,
        queue=MEDIA_QUEUE,
        lock=MEDIA_STORAGE_LOCK,
        pass_context=True,
        retry=MediaRetryStrategy(),
    )
    async def prepare_media(context, job_id: str):
        worker = context.additional_context["media_worker"]
        await worker.process_job(job_id, attempt=max(1, context.job.attempts))

    # croniter places seconds last in a six-field expression.  Keeping that
    # explicit matters here: a conventional seconds-first expression silently
    # turns this into a 30-minute schedule.
    @app.periodic(cron=REFRESH_CRON)
    @app.task(name="photo_wall.media.refresh", queue=MEDIA_QUEUE, pass_context=True,
              queueing_lock="media-refresh")
    async def library_tick(context, timestamp: int):
        del timestamp
        await context.additional_context["library_checks"].tick()

    # One connection check, or one bounded step of a pool's check (the step queues the next).
    # The lock keeps one step per connection or pool running at a time.
    @app.task(name=CHECK_CONNECTION_TASK, queue=MEDIA_QUEUE, pass_context=True)
    async def check_connection(context, connection_ref: str):
        await context.additional_context["library_checks"].contact(connection_ref)

    @app.task(name=CHECK_POOL_TASK, queue=MEDIA_QUEUE, pass_context=True)
    async def check_pool(context, pool_key: str):
        await context.additional_context["library_checks"].check_pool(pool_key)

    @app.periodic(cron=MAINTENANCE_CRON)
    @app.task(name="photo_wall.media.maintenance", queue=MEDIA_QUEUE, pass_context=True,
              lock=MEDIA_STORAGE_LOCK, queueing_lock="media-maintenance")
    async def maintain_media(context, timestamp: int):
        del timestamp
        await context.additional_context["media_worker"].maintain()

    return app
