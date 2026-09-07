"""Procrastinate task definitions for the central media worker."""

from __future__ import annotations

from typing import Any

import procrastinate

from central.media_queue import MEDIA_QUEUE, MEDIA_STORAGE_LOCK, PREPARE_MEDIA_TASK

REFRESH_CRON = "* * * * * */30"
MAINTENANCE_CRON = "*/5 * * * * 0"


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


def create_worker_app(dsn: str) -> procrastinate.App:
    """Build the async worker app; runtime collaborators arrive via context."""
    app = procrastinate.App(
        connector=procrastinate.PsycopgConnector(conninfo=dsn),
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
    async def refresh_media(context, timestamp: int):
        del timestamp
        await context.additional_context["media_worker"].refresh_once()

    @app.periodic(cron=MAINTENANCE_CRON)
    @app.task(name="photo_wall.media.maintenance", queue=MEDIA_QUEUE, pass_context=True,
              lock=MEDIA_STORAGE_LOCK, queueing_lock="media-maintenance")
    async def maintain_media(context, timestamp: int):
        del timestamp
        await context.additional_context["media_worker"].maintain()

    return app
