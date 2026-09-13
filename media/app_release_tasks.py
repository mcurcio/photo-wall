"""Procrastinate task definitions for GitHub release sourcing (0010, bead 3).

Registered onto the same worker app as the media tasks (`create_worker_app`),
so one worker process consumes both the media queue and the release queue. The
periodic poll self-coalesces on a queueing lock; the mirror is deferred by the
API's promote route (bead 4) keyed on the tag.

The tasks are thin: all logic lives in `AppReleaseService`
(`central/app_release_service.py`), injected via `additional_context`. This
module owns only the queue placement, the periodic cadence, and the retry
policy -- mirroring how `media/task_queue.py` wraps `MediaWorker`.
"""

from __future__ import annotations

from typing import Any

import procrastinate

from central.app_release_queue import (
    APP_RELEASE_QUEUE,
    MIRROR_RELEASE_TASK,
    POLL_QUEUEING_LOCK,
    POLL_RELEASES_TASK,
    POLL_SECONDS,
    poll_cron,
)


class AppReleaseTaskFailed(RuntimeError):
    """A bounded release-task failure safe to expose to the queue journal."""


class RetryableAppReleaseTask(AppReleaseTaskFailed):
    """A release-task failure Procrastinate may retry (transient network fault)."""


class AppReleaseRetryStrategy(procrastinate.BaseRetryStrategy):
    delays = (5, 15, 60)

    def get_retry_decision(self, *, exception: BaseException, job: Any):
        attempt = max(1, job.attempts)
        if not isinstance(exception, RetryableAppReleaseTask) or attempt > len(self.delays):
            return None
        return procrastinate.RetryDecision(retry_in={"seconds": self.delays[attempt - 1]})


def register_app_release_tasks(app: procrastinate.App, *, poll_seconds: int = POLL_SECONDS) -> None:
    """Register the poll (periodic) and mirror tasks on an existing worker app.

    Called after `create_worker_app` so both media and release tasks share one
    app and one `run_worker_async` call. `poll_seconds` sets the periodic cadence
    (0010 gate #3 placeholder, named constant, deployment-overridable).
    """

    # croniter reads a six-field expression seconds-last (see media/task_queue.py);
    # poll_cron encodes the ~POLL_SECONDS cadence in that form.
    @app.periodic(cron=poll_cron(poll_seconds))
    @app.task(
        name=POLL_RELEASES_TASK,
        queue=APP_RELEASE_QUEUE,
        pass_context=True,
        queueing_lock=POLL_QUEUEING_LOCK,
    )
    async def poll_releases(context, timestamp: int):
        del timestamp
        await context.additional_context["app_release_service"].poll()

    @app.task(
        name=MIRROR_RELEASE_TASK,
        queue=APP_RELEASE_QUEUE,
        pass_context=True,
        retry=AppReleaseRetryStrategy(),
    )
    async def mirror_release(context, tag: str):
        result = await context.additional_context["app_release_service"].mirror(tag)
        # Terminal faults (corrupt/oversize/missing asset) are recorded as
        # `mirror_failed` and the task completes; only a transient fault raises so
        # the promote heals automatically once connectivity returns.
        if result.get("retryable"):
            raise RetryableAppReleaseTask(result.get("reason", "mirror_failed"))
