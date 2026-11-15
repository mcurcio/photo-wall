"""Queue hygiene: rescue the jobs of dead workers, purge what has finished (design §4, §10.3).

The rescue touches only OUR tasks (`TASK_PREFIX` + a type this admin was built with): legacy
`photo_wall.media.*` rows and procrastinate's builtins are never rescued. The purge
(`FinishedJobs`) takes every queue's finished jobs, legacy ones included (decision 0033).

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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import procrastinate
from procrastinate.jobs import Status

from central.infra.batched_retention import Begin, drain
from central.infra.job_queue import async_connector, build_app, decode, defer_async, task_name
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
        self._app = build_app(async_connector(dsn, min_size=0, max_size=2), job_types, None)
        self._names = frozenset(task_name(job_type) for job_type in job_types)
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


# The oldest finished jobs by id (the primary key's index), each judged by its newest event as
# procrastinate's own `delete_old_jobs` judges it; `procrastinate_events` goes by its ON DELETE
# CASCADE. A job with no event is never judged old, as in procrastinate's query.
_DELETE_FINISHED = (
    "DELETE FROM procrastinate_jobs WHERE id IN ("
    "SELECT j.id FROM procrastinate_jobs j "
    "WHERE j.status IN ('succeeded', 'failed', 'cancelled', 'aborted') "
    "AND (SELECT max(e.at) FROM procrastinate_events e WHERE e.job_id = j.id) "
    "< now() - %(older_than)s::interval "
    "ORDER BY j.id LIMIT %(batch_rows)s)"
)


class FinishedJobs:
    """Every queue's finished jobs, deleted in bounded batches (decision 0033): replaces
    `QueueAdmin.delete_finished`, whose call into procrastinate (`delete_old_jobs`) is one
    unbounded DELETE per queue and covered the upkeep queues only. The media queue was never
    purged: 392,000 of the 410,000 job rows on 2026-11-15.

    `begin` opens one transaction per batch (`central.db.Database.transaction`); the loop is
    `central.infra.batched_retention.drain`. `ValueError` when `batch_rows` or `batches` is
    under 1.
    """

    def __init__(self, begin: Begin, *, batch_rows: int = 1000, batches: int = 20) -> None:
        if batch_rows < 1 or batches < 1:
            raise ValueError("batch_rows and batches must be >= 1")
        self._begin, self._batch_rows, self._batches = begin, batch_rows, batches

    def delete(self, *, older_than: timedelta) -> int:
        """Delete up to `batches` batches of at most `batch_rows` jobs, oldest first, whose
        status is succeeded, failed, cancelled or aborted and whose newest event is older than
        `older_than` on the database's clock (the age procrastinate's own purge judges by);
        each job's events go with it. Every queue, every task name. Returns the jobs deleted.
        Blocks: call it from a worker thread.

        One call is one hourly `PurgeFinishedJobs` run, so a short last batch is deleted too.
        The defaults drain a backlog at 20,000 jobs an hour.

        Never deletes a `todo` or `doing` job, a job younger than `older_than`, or more than
        `batch_rows` jobs in one statement (each job's delete also removes about three events
        and runs procrastinate's own per-row bookkeeping, under a 10 s statement timeout).

        Errors: a failed batch raises the driver's error with the earlier batches committed;
        the handler's run fails and the next hourly run goes on.
        """
        return drain(self._begin, _DELETE_FINISHED, {"older_than": older_than},
                     batch_rows=self._batch_rows, batches=self._batches)


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
    def __init__(self, finished: FinishedJobs, *, transactions: Transactions,
                 outcomes: JobOutcomes, clock: Clock, keep_jobs: timedelta = timedelta(days=7),
                 keep_outcomes: timedelta = timedelta(days=30)) -> None:
        self._finished = finished
        self._transactions = transactions
        self._outcomes = outcomes
        self._clock = clock
        self._keep_jobs = keep_jobs
        self._keep_outcomes = keep_outcomes

    async def handle(self, job: PurgeFinishedJobs) -> None:
        jobs = await asyncio.to_thread(self._finished.delete, older_than=self._keep_jobs)
        logger.info("purged %s finished jobs older than %s", jobs, self._keep_jobs)
        older_than = self._clock.utc() - self._keep_outcomes.total_seconds()

        def purge() -> int:
            with self._transactions.begin() as tx:
                return self._outcomes.purge(tx, older_than=older_than)

        purged = await asyncio.to_thread(purge)
        logger.info("purged %s job outcomes not written since %s", purged, older_than)
