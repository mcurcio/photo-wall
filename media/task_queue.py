"""Procrastinate task definitions for the central media worker.

No poll and no rescue of its own (decision 0034, S3a): a library check is deferred for its due
time by the check before it (central/infra/photo_library_store.py), and a dead worker's jobs, on
this queue as on the job runtime's, are rescued by the one rescue
(central/infra/queue_ops.py `QueueRescue`)."""

from __future__ import annotations

import logging
from typing import Any, Final

import procrastinate

from central.infra.job_queue import async_connector
from central.media_queue import (
    CHECK_CONNECTION_TASK,
    CHECK_POOL_TASK,
    MEASURE_KEY_CONTENT_TASK,
    MEDIA_QUEUE,
    MEDIA_STORAGE_LOCK,
)

# Seconds last (croniter reads a sixth field as seconds).
MAINTENANCE_MINUTES: Final = 5
MAINTENANCE_CRON = f"*/{MAINTENANCE_MINUTES} * * * * 0"

logger = logging.getLogger(__name__)


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


class CheckRetryStrategy(MediaRetryStrategy):
    """A pool check step that raises is retried (any error, MediaRetryStrategy's delays); once
    the retries run out the task's failure path queues the pool's next check at its due time."""

    def get_retry_decision(self, *, exception: BaseException, job: Any):
        if final_attempt(job):
            return None
        return procrastinate.RetryDecision(retry_in={"seconds": self.delays[max(1, job.attempts) - 1]})


def final_attempt(job: Any) -> bool:
    """Whether this run of `job` is its last: the queue retries it no more."""
    return max(1, job.attempts) > len(MediaRetryStrategy.delays)


def create_worker_app(dsn: str) -> procrastinate.App:
    """Build the async worker app; runtime collaborators arrive via context."""
    app = procrastinate.App(
        connector=async_connector(dsn, application_name="photo-wall-media-jobs"),
    )

    # Roadmap 5a: one photo's key content. No lock: measurements run beside the
    # library checks; the queueing lock (central/media_queue.py) keeps one queued per photo.
    @app.task(name=MEASURE_KEY_CONTENT_TASK, queue=MEDIA_QUEUE, pass_context=True,
              retry=MediaRetryStrategy())
    async def measure_key_content(context, asset_id: str):
        await context.additional_context["media_worker"].measure(
            asset_id, final_attempt=final_attempt(context.job))

    # One connection check, or one bounded step of a pool's check (the step queues the next).
    # The lock (central/media_queue.py) keeps one step per connection or pool running at a time.
    # A contact queues its next check as it begins, so a contact that raises is not retried.
    @app.task(name=CHECK_CONNECTION_TASK, queue=MEDIA_QUEUE, pass_context=True)
    async def check_connection(context, connection_ref: str):
        await context.additional_context["library_checks"].contact(connection_ref)

    @app.task(name=CHECK_POOL_TASK, queue=MEDIA_QUEUE, pass_context=True,
              retry=CheckRetryStrategy())
    async def check_pool(context, pool_key: str):
        checks = context.additional_context["library_checks"]
        try:
            await checks.check_pool(pool_key)
        except Exception:
            if final_attempt(context.job):
                await checks.check_step_failed(pool_key)
            raise

    @app.periodic(cron=MAINTENANCE_CRON)
    @app.task(name="photo_wall.media.maintenance", queue=MEDIA_QUEUE, pass_context=True,
              lock=MEDIA_STORAGE_LOCK, queueing_lock="media-maintenance")
    async def maintain_media(context, timestamp: int):
        del timestamp
        await context.additional_context["media_worker"].maintain()

    return app
