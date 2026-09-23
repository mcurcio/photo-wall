"""The one worker kind: one procrastinate App, one Worker loop per queue (design §10.3).

A `Worker`'s concurrency spans all of its queues (procrastinate worker.py:34-51), so each queue
gets its own loop with its own concurrency. Every boot check runs in `__init__`, so a wiring
mistake fails at worker start, before any job is claimed.

procrastinate never retries in place (`retry=None`; the runtime retries by re-publishing). A
delivery whose outcome is `transient` or `terminal` raises `RecordedFailure` so procrastinate
ends that row `failed`; `ok` and an early copy (None) end it `succeeded`.

**A loop that ends while not stopping is a failure** (`until_stopped`). procrastinate ends a
worker NORMALLY when its LISTEN, heartbeat or periodic side task fails (worker.py
`_monitor_side_tasks`), so a returned loop would leave its queue dead in a healthy-looking
process. Raising instead makes the process exit non-zero and be restarted.

**A completion write that cannot be persisted stops the runtime.** If procrastinate's
`finish_job` fails, the row stays `doing` under a LIVE worker: rescue (dead workers only) never
sees it and its lock blocks the key forever. `_CompletionGuard` retries the write
(`COMPLETION_RETRY_DELAYS`); if it still fails, `run()` raises `completion_not_recorded`. The
process exits, its worker row is unregistered (or its heartbeat lapses), and rescue re-publishes
the row exactly like any dead worker's.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Protocol

import procrastinate
from procrastinate.manager import JobManager

from central.infra.execution import JobExecutor, Redelivery
from central.infra.job_queue import build_app, carry_attempt_async, defer_async
from central.infra.outcomes import JobOutcomes
from central.infra.transactions import PgTransactions
from central.kernel.handling import Handler
from central.kernel.job_types import CATALOG
from central.kernel.jobs import Job, QueueName
from central.kernel.ports import AssetRecords
from contracts.time import Clock

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS: Final = 10.0
STALLED_WORKER_SECONDS: Final = 30.0
# Pauses between completion-write attempts: a blip is absorbed well inside one heartbeat.
COMPLETION_RETRY_DELAYS: Final = (0.5, 1.0, 2.0)
WORKER_EXITED: Final = "worker_exited"
COMPLETION_NOT_RECORDED: Final = "completion_not_recorded"


class RecordedFailure(Exception):
    """The outcome is already committed; this only ends the procrastinate row `failed`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


async def until_stopped(loop: Awaitable[None], stopping: Callable[[], bool]) -> None:
    """Await a loop that must run until asked to stop; returning while not stopping raises."""
    await loop
    if not stopping():
        raise RuntimeError(WORKER_EXITED)


class _CompletionGuard(JobManager):
    """procrastinate's `JobManager`, whose completion write retries and then fails the runtime."""

    def __init__(self, connector: procrastinate.BaseConnector,
                 on_lost: Callable[[], None]) -> None:
        super().__init__(connector)
        self._on_lost = on_lost

    async def finish_job(self, job: Any, status: Any, delete_job: bool) -> None:
        for delay in (*COMPLETION_RETRY_DELAYS, None):
            try:
                await super().finish_job(job, status, delete_job)
                return
            except Exception:
                if delay is None:
                    logger.exception("job %s: completion not recorded; stopping the runtime",
                                     job.id)
                    self._on_lost()
                    raise
                logger.warning("job %s: completion write failed; retrying", job.id,
                               exc_info=True)
                await asyncio.sleep(delay)


class JobRuntime:
    """Runs every handler for every queue of its catalog in this process.

    `owned` are resources the handlers use (the queue-ops pool); `run()` closes them on exit.
    """

    def __init__(self, dsn: str, handlers: Sequence[Handler[Any, Any]],
                 concurrency: Mapping[QueueName, int], *, transactions: PgTransactions,
                 assets: AssetRecords, clock: Clock,
                 catalog: Sequence[type[Job[Any]]] = CATALOG,
                 owned: Sequence[AsyncCloseable] = ()) -> None:
        self._executor = JobExecutor(handlers, transactions=transactions, outcomes=JobOutcomes(),
                                     assets=assets, clock=clock, redeliver=self._redeliver,
                                     catalog=catalog)
        if set(concurrency) != self._executor.queues:
            raise ValueError("concurrency must name exactly the catalog's queues")
        if any(type(n) is not int or n < 1 for n in concurrency.values()):
            raise ValueError("concurrency must be at least 1 per queue")
        self._concurrency = dict(concurrency)
        self._app = build_app(procrastinate.PsycopgConnector(conninfo=dsn), catalog, self._body)
        # The worker reads `app.job_manager` at every call (procrastinate worker.py), so the
        # guard sees every completion write.
        self._app.job_manager = _CompletionGuard(self._app.connector, self._completion_lost)
        self._owned = tuple(owned)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loops: list[asyncio.Task[None]] = []
        self._stopping = False
        self._lost = False

    async def run(self) -> None:
        """Run until `stop()`; a loop that fails or ends stops the others and `run` raises."""
        if self._stopping:
            return
        self._loop = asyncio.get_running_loop()
        try:
            # Not `async with open_async()`: a cancellation DURING the open (e.g. the worker's
            # other loop failed at boot) would skip the close and leave the pool reconnecting
            # forever, so the process could never exit.
            await self._app.open_async()
            async with asyncio.TaskGroup() as group:
                self._loops = [
                    group.create_task(until_stopped(self._app.run_worker_async(
                        queues=[queue.value], concurrency=count,
                        name=f"photo-wall-{queue.value}",
                        install_signal_handlers=False,
                        update_heartbeat_interval=HEARTBEAT_SECONDS,
                        stalled_worker_timeout=STALLED_WORKER_SECONDS,
                    ), lambda: self._stopping), name=f"worker {queue.value}")
                    for queue, count in sorted(self._concurrency.items())
                ]
                if self._stopping:  # stop() raced the start
                    self._cancel_loops()
        finally:
            self._loops = []
            self._loop = None
            await self._app.close_async()  # idempotent; also closes a half-opened pool
            for resource in self._owned:
                await resource.aclose()
        if self._lost:
            raise RuntimeError(COMPLETION_NOT_RECORDED)

    def stop(self) -> None:
        """Gracefully stop every loop: running jobs finish first. Idempotent; any thread."""
        self._stopping = True
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):  # the loop already closed
            loop.call_soon_threadsafe(self._cancel_loops)

    def _completion_lost(self) -> None:
        """Called on the runtime's loop by `_CompletionGuard`: stop gracefully, then raise."""
        self._lost = True
        if self._loop is not None:
            self._loop.call_soon(self._cancel_loops)

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
        inserted = await defer_async(self._app, redelivery.job, attempt=redelivery.attempt,
                                     schedule_at=datetime.fromtimestamp(redelivery.not_before,
                                                                        UTC))
        if not inserted:
            # Merged into the key's pending copy, which may carry attempt 0 (a request published
            # while this delivery ran): carry the backoff forward instead of resetting it.
            await carry_attempt_async(self._app, redelivery.job, attempt=redelivery.attempt)
