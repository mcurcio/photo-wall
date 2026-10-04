"""The ONLY mapping between kernel jobs and procrastinate.

A job type becomes one procrastinate task named `TASK_PREFIX + job_name`, on its delivery's queue
and priority, with `retry=None`: procrastinate never retries in place, because the runtime
retries by re-publishing (design §10.1). The locks come from `job_keys` and the job's type;
callers never write them. `queueing_lock` (one pending copy) is taken by every job.
`lock` (one running copy) is taken only by asset fetches, where it saves a second download.
Every other job may overlap itself, so a stalled rescue never blocks the next tick (idempotent
jobs §6). The key itself is still the `job_outcomes` key and the NOTIFY payload. A periodic job
type is additionally registered with `app.periodic`, carrying the constant locks of its
field-less job, so a tick and an on-demand publish merge (PB9).

Task kwargs are the job's `model_dump(mode="json")` plus the attempt under `ATTEMPT_KWARG`.
procrastinate passes kwargs through verbatim (worker.py:301), and pydantic forbids fields that
start with `_`, so the attempt can never collide with a job field. A periodic tick adds a
`timestamp` kwarg, which `decode` drops (the kernel reserves that field name).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

import procrastinate
from procrastinate.exceptions import AlreadyEnqueued

from central.kernel.jobs import PERIODIC_CADENCES, Job, job_keys, registered_job_type

TASK_PREFIX: Final = "photo_wall."
ATTEMPT_KWARG: Final = "_attempt"
_TIMESTAMP_KWARG: Final = "timestamp"  # added by procrastinate's periodic deferrer

TaskBody = Callable[[Job[Any], int], Awaitable[None]]


def task_name(job_type: type[Job[Any]]) -> str:
    return TASK_PREFIX + job_type.job_name


def _require_attempt(attempt: object) -> int:
    if type(attempt) is not int or attempt < 0:
        raise ValueError("invalid_attempt")
    return attempt


def job_kwargs(job: Job[Any], *, attempt: int) -> dict[str, Any]:
    """The task kwargs of one delivery of `job`."""
    job_keys(job)  # TypeError for an unregistered job type
    return {**job.model_dump(mode="json"), ATTEMPT_KWARG: _require_attempt(attempt)}


def decode(task_name: str, kwargs: Mapping[str, Any]) -> tuple[Job[Any], int]:
    """The job and attempt a task row carries; `KeyError` for a task that is not ours."""
    if not task_name.startswith(TASK_PREFIX):
        raise KeyError(task_name)
    job_type = registered_job_type(task_name[len(TASK_PREFIX):])
    fields = dict(kwargs)
    fields.pop(_TIMESTAMP_KWARG, None)
    attempt = _require_attempt(fields.pop(ATTEMPT_KWARG, 0))
    return job_type.model_validate(fields), attempt


def periodic_cron(every: timedelta) -> str:
    """A six-field croniter expression with SECONDS LAST (the `media/task_queue.py` convention).

    croniter reads a sixth field as seconds; a seconds-first expression would silently mean a
    different schedule, so the trailing `0` pins each fire to second 0.
    """
    if every not in PERIODIC_CADENCES:
        raise ValueError("invalid_every")
    minutes = int(every.total_seconds()) // 60
    if minutes < 60:
        return f"*/{minutes} * * * * 0"
    hours = minutes // 60
    if hours < 24:
        return f"0 */{hours} * * * 0"
    return "0 0 * * * 0"


def _publish_only(job_type: type[Job[Any]]) -> TaskBody:
    async def refuse(job: Job[Any], attempt: int) -> None:
        raise RuntimeError(f"publish-only app cannot run {job_type.job_name}")

    return refuse


def _lock(job: Job[Any]) -> str | None:
    """The one-running-copy lock: an asset fetch's key, None for every other job type."""
    return job_keys(job).lock if type(job).asset_kind is not None else None


def _register(app: procrastinate.App, job_type: type[Job[Any]], body: TaskBody) -> None:
    name = task_name(job_type)
    delivery = job_type.delivery

    async def run(context: Any, **kwargs: Any) -> None:
        job, attempt = decode(context.job.task_name, kwargs)
        await body(job, attempt)

    run.__name__ = run.__qualname__ = name
    task = app.task(name=name, queue=delivery.queue.value, priority=delivery.priority,
                    retry=None, pass_context=True)(run)
    if delivery.every is not None:
        tick = job_type()
        app.periodic(cron=periodic_cron(delivery.every), periodic_id=job_type.job_name,
                     lock=_lock(tick), queueing_lock=job_keys(tick).queueing_lock)(task)


def build_app(connector: procrastinate.BaseConnector, job_types: Sequence[type[Job[Any]]],
              body: TaskBody | None) -> procrastinate.App:
    """One task per job type; `body` None builds a publish-only app whose tasks refuse to run."""
    if len(set(job_types)) != len(job_types):
        raise ValueError("duplicate_job_type")
    app = procrastinate.App(connector=connector)
    for job_type in job_types:
        registered_job_type(job_type.job_name)  # KeyError for an unregistered type
        _register(app, job_type, body if body is not None else _publish_only(job_type))
    return app


def _deferrer(app: procrastinate.App, job: Job[Any], *, connection: Any,
              schedule_at: datetime | None) -> Any:
    # allow_unknown=False: a type this app was not built with must never land on the
    # procrastinate "default" queue.
    return app.configure_task(task_name(type(job)), allow_unknown=False, lock=_lock(job),
                              queueing_lock=job_keys(job).queueing_lock, connection=connection,
                              schedule_at=schedule_at)


def defer(app: procrastinate.App, job: Job[Any], *, attempt: int, connection: Any,
          schedule_at: datetime | None = None) -> bool:
    """Insert one delivery through the caller's connection; True inserted, False merged.

    The insert runs in a SAVEPOINT (`connection.transaction()`, the `media_queue.py` pattern) and
    `AlreadyEnqueued` is caught OUTSIDE it, so a merge rolls back only the savepoint and never
    aborts the caller's transaction.
    """
    kwargs = job_kwargs(job, attempt=attempt)
    deferrer = _deferrer(app, job, connection=connection, schedule_at=schedule_at)
    try:
        with connection.transaction():
            deferrer.defer(**kwargs)
    except AlreadyEnqueued:
        return False
    return True


async def defer_async(app: procrastinate.App, job: Job[Any], *, attempt: int,
                      schedule_at: datetime | None = None) -> bool:
    """Insert one delivery through the app's own (opened) async pool; True inserted, False merged."""
    kwargs = job_kwargs(job, attempt=attempt)
    deferrer = _deferrer(app, job, connection=None, schedule_at=schedule_at)
    try:
        await deferrer.defer_async(**kwargs)
    except AlreadyEnqueued:
        return False
    return True


_CARRY_ATTEMPT: Final = (
    "UPDATE procrastinate_jobs SET args = jsonb_set(args, '{" + ATTEMPT_KWARG + "}', "
    "to_jsonb(%(attempt)s::int)) "
    "WHERE status = 'todo' AND queueing_lock = %(queueing_lock)s AND task_name = %(task_name)s "
    "AND COALESCE((args ->> '" + ATTEMPT_KWARG + "')::int, 0) < %(attempt)s")


async def carry_attempt_async(app: procrastinate.App, job: Job[Any], *, attempt: int) -> None:
    """Raise the key's pending (`todo`) copy to at least `attempt`, through the app's pool.

    A redelivery that merged into a pending copy must not reset the backoff: that copy may carry
    a lower attempt (a request's attempt 0). Called while the redelivering copy is still `doing`;
    an asset job's lock keeps the pending copy from being fetched in between. A non-asset pending
    copy fetched first is no longer `todo`, so it is left as it is.
    """
    await app.connector.execute_query_async(
        _CARRY_ATTEMPT, attempt=_require_attempt(attempt),
        queueing_lock=job_keys(job).queueing_lock, task_name=task_name(type(job)))
