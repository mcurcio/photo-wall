"""The one worker kind: one procrastinate App, one Worker loop per queue (design §10.3).

A `Worker`'s concurrency spans all of its queues (procrastinate worker.py:34-51), so each queue
gets its own loop with its own concurrency. Every boot check runs in `__init__`, so a wiring
mistake fails at worker start, before any job is claimed.

procrastinate never retries in place (`retry=None`; the runtime retries by re-publishing). A
delivery whose outcome is `transient` or `terminal` raises `RecordedFailure` so procrastinate
ends that row `failed`; `ok` and an early copy (None) end it `succeeded`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

import procrastinate

from central.infra.execution import JobExecutor, Redelivery
from central.infra.job_queue import build_app, defer_async
from central.infra.outcomes import JobOutcomes
from central.infra.transactions import PgTransactions
from central.kernel.handling import Handler
from central.kernel.job_types import CATALOG
from central.kernel.jobs import Job, QueueName
from central.kernel.ports import AssetRecords
from contracts.time import Clock

HEARTBEAT_SECONDS: Final = 10.0
STALLED_WORKER_SECONDS: Final = 30.0


class RecordedFailure(Exception):
    """The outcome is already committed; this only ends the procrastinate row `failed`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class JobRuntime:
    """Runs every handler for every queue of its catalog in this process."""

    def __init__(self, dsn: str, handlers: Sequence[Handler[Any, Any]],
                 concurrency: Mapping[QueueName, int], *, transactions: PgTransactions,
                 assets: AssetRecords, clock: Clock,
                 catalog: Sequence[type[Job[Any]]] = CATALOG) -> None:
        self._executor = JobExecutor(handlers, transactions=transactions, outcomes=JobOutcomes(),
                                     assets=assets, clock=clock, redeliver=self._redeliver,
                                     catalog=catalog)
        if set(concurrency) != self._executor.queues:
            raise ValueError("concurrency must name exactly the catalog's queues")
        if any(type(n) is not int or n < 1 for n in concurrency.values()):
            raise ValueError("concurrency must be at least 1 per queue")
        self._concurrency = dict(concurrency)
        self._app = build_app(procrastinate.PsycopgConnector(conninfo=dsn), catalog, self._body)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loops: list[asyncio.Task[None]] = []
        self._stopping = False

    async def run(self) -> None:
        """Run until `stop()`; a loop that fails stops the others (TaskGroup)."""
        if self._stopping:
            return
        self._loop = asyncio.get_running_loop()
        async with self._app.open_async():
            try:
                async with asyncio.TaskGroup() as group:
                    self._loops = [
                        group.create_task(self._app.run_worker_async(
                            queues=[queue.value], concurrency=count,
                            name=f"photo-wall-{queue.value}",
                            install_signal_handlers=False,
                            update_heartbeat_interval=HEARTBEAT_SECONDS,
                            stalled_worker_timeout=STALLED_WORKER_SECONDS,
                        ), name=f"worker {queue.value}")
                        for queue, count in sorted(self._concurrency.items())
                    ]
                    if self._stopping:  # stop() raced the start
                        self._cancel_loops()
            finally:
                self._loops = []
                self._loop = None

    def stop(self) -> None:
        """Gracefully stop every loop: running jobs finish first. Idempotent; any thread."""
        self._stopping = True
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):  # the loop already closed
            loop.call_soon_threadsafe(self._cancel_loops)

    def _cancel_loops(self) -> None:
        # Cancelling `run_worker_async` makes procrastinate's Worker.run stop gracefully: it
        # stops fetching, waits for running jobs, unregisters, then re-raises (worker.py run).
        for task in self._loops:
            task.cancel()

    async def _body(self, job: Job[Any], attempt: int) -> None:
        status = await self._executor.execute(job, attempt)
        if status in ("transient", "terminal"):
            raise RecordedFailure(status)

    async def _redeliver(self, redelivery: Redelivery) -> None:
        await defer_async(self._app, redelivery.job, attempt=redelivery.attempt,
                          schedule_at=datetime.fromtimestamp(redelivery.not_before, UTC))
