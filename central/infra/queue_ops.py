"""Queue hygiene: rescue the jobs of dead workers, purge what has finished (design §4, §10.3).

Only OUR tasks are touched (`TASK_PREFIX` + a type this admin was built with): legacy
`photo_wall.media.*` rows and procrastinate's builtins are never rescued, and the purge filters
by our queues.

**Rescue re-publishes first, then closes** the stalled row. The pending (queueing-lock) index
covers only `todo`, so the new copy inserts while the stalled row is still `doing`. An asset
fetch's copy runs only after the close frees the lock (`procrastinate_fetch_job_v2` skips a
`todo` whose lock a `doing` row holds); any other job takes no running lock, so its copy may run
at once. Closing first would open a window with no copy at all.

Handler modules import their job types at runtime (never under TYPE_CHECKING): the runtime reads
`handle`'s annotations to dispatch.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import procrastinate
from procrastinate.jobs import Status

from central.infra.job_queue import build_app, decode, defer_async, task_name
from central.infra.outcomes import JobOutcomes
from central.kernel.handling import TransientFailure
from central.kernel.job_types import CATALOG, PurgeFinishedJobs, RescueStalledJobs
from central.kernel.jobs import Job
from central.kernel.transactions import Transactions
from contracts.time import Clock

logger = logging.getLogger(__name__)

RESCUE_INCOMPLETE: Final = "rescue_incomplete"


@dataclass(frozen=True, slots=True)
class StalledJob:
    id: int
    job: Job[Any]
    attempt: int


class QueueAdmin:
    """procrastinate's queue-maintenance calls, limited to our tasks.

    Its async pool opens on first use; `aclose()` releases it.
    """

    def __init__(self, dsn: str, *, job_types: Sequence[type[Job[Any]]] = CATALOG) -> None:
        self._app = build_app(procrastinate.PsycopgConnector(conninfo=dsn, min_size=0,
                                                             max_size=2),
                              job_types, None)
        self._names = frozenset(task_name(job_type) for job_type in job_types)
        self._queues = sorted({job_type.delivery.queue.value for job_type in job_types})
        self._opening = asyncio.Lock()

    async def _opened(self) -> procrastinate.App:
        async with self._opening:
            await self._app.open_async()  # idempotent
        return self._app

    async def aclose(self) -> None:
        await self._app.close_async()

    async def stalled(self, *, heartbeat_timeout: timedelta) -> list[StalledJob]:
        """`doing` rows of ours whose worker has not beaten for `heartbeat_timeout`."""
        app = await self._opened()
        rows = await app.job_manager.get_stalled_jobs(
            seconds_since_heartbeat=heartbeat_timeout.total_seconds())
        found = []
        for row in rows:
            if row.task_name not in self._names or row.id is None:
                continue
            try:
                job, attempt = decode(row.task_name, row.task_kwargs)
            except (KeyError, ValueError) as error:  # a row our code cannot have written
                logger.error("stalled job %s is undecodable: %s", row.id, error)
                continue
            found.append(StalledJob(row.id, job, attempt))
        return found

    async def republish(self, stalled: StalledJob) -> None:
        """A new copy at the SAME attempt: a dead worker is not the job's failure."""
        await defer_async(await self._opened(), stalled.job, attempt=stalled.attempt)

    async def close(self, stalled: StalledJob) -> None:
        """End the stalled row `failed`; an asset fetch's row frees its lock for the new copy."""
        app = await self._opened()
        await app.job_manager.finish_job_by_id_async(job_id=stalled.id, status=Status.FAILED,
                                                     delete_job=False)

    async def delete_finished(self, *, older_than: timedelta) -> None:
        """Delete our queues' succeeded, failed, cancelled and aborted rows older than that."""
        # procrastinate counts whole hours; rounding up never deletes a row that is too young.
        hours = max(1, math.ceil(older_than.total_seconds() / 3600))
        app = await self._opened()
        for queue in self._queues:
            await app.job_manager.delete_old_jobs(
                nb_hours=hours, queue=queue, include_failed=True, include_cancelled=True,
                include_aborted=True)


class RescueStalledJobsHandler:
    def __init__(self, admin: QueueAdmin, *,
                 heartbeat_timeout: timedelta = timedelta(seconds=30)) -> None:
        self._admin = admin
        self._heartbeat_timeout = heartbeat_timeout

    async def handle(self, job: RescueStalledJobs) -> None:
        """Rescue each stalled row on its own: one row's failure never skips the others.

        A row can fail, e.g. `close` finds it no longer `doing` because its worker came back and
        finished it. Any failure makes this run transient, so the next run looks again.
        """
        failed = 0
        for stalled in await self._admin.stalled(heartbeat_timeout=self._heartbeat_timeout):
            logger.warning("rescuing stalled job %s (%s, attempt %s)", stalled.id,
                           type(stalled.job).job_name, stalled.attempt)
            try:
                await self._admin.republish(stalled)
                await self._admin.close(stalled)
            except Exception:
                failed += 1
                logger.exception("rescue of stalled job %s failed", stalled.id)
        if failed:
            raise TransientFailure(RESCUE_INCOMPLETE)


class PurgeFinishedJobsHandler:
    def __init__(self, admin: QueueAdmin, *, transactions: Transactions, outcomes: JobOutcomes,
                 clock: Clock, keep_jobs: timedelta = timedelta(days=7),
                 keep_outcomes: timedelta = timedelta(days=30)) -> None:
        self._admin = admin
        self._transactions = transactions
        self._outcomes = outcomes
        self._clock = clock
        self._keep_jobs = keep_jobs
        self._keep_outcomes = keep_outcomes

    async def handle(self, job: PurgeFinishedJobs) -> None:
        await self._admin.delete_finished(older_than=self._keep_jobs)
        older_than = self._clock.utc() - self._keep_outcomes.total_seconds()

        def purge() -> int:
            with self._transactions.begin() as tx:
                return self._outcomes.purge(tx, older_than=older_than)

        purged = await asyncio.to_thread(purge)
        logger.info("purged %s job outcomes not written since %s", purged, older_than)
