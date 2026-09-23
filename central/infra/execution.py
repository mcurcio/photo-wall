"""Per-job execution semantics, free of procrastinate so they are unit-testable (design §10.1, §10.3).

One delivery of a job runs its handler and ends in exactly one committed outcome:
- a returned `R` is `ok`; an asset job's `R` is written onto its Asset record in the SAME
  transaction as the outcome;
- `TerminalFailure` is `terminal`, never redelivered;
- `TransientFailure` or ANY other exception (logged as a handler bug) is `transient`, and while
  the delivery's backoff lasts the job is re-published for `retry_not_before`.

The outcome commits BEFORE the redelivery is requested, so a redelivered copy always sees it. A
copy that starts early inside a retry window re-publishes itself for the window's end without
running. `CancelledError` propagates untouched: no outcome, no redelivery.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final, Protocol

from central.infra.outcomes import OutcomeRow, OutcomeStatus
from central.kernel.assets import AssetReady
from central.kernel.handling import (
    Handler,
    ProducedFactsConflict,
    TerminalFailure,
    TransientFailure,
    handler_job_type,
)
from central.kernel.job_types import CATALOG
from central.kernel.jobs import Job, QueueName, asset_key, job_keys
from central.kernel.ports import AssetRecords
from central.kernel.transactions import Transaction, Transactions
from contracts.time import Clock

logger = logging.getLogger(__name__)

MAX_RETRY_DELAY: Final = timedelta(hours=1)
EARLY_COPY_TOLERANCE: Final = timedelta(seconds=1)
UNCLASSIFIED_ERROR: Final = "unclassified_error"
FACTS_CONFLICT: Final = "facts_conflict"


@dataclass(frozen=True, slots=True)
class Redelivery:
    job: Job[Any]
    attempt: int
    not_before: float  # epoch seconds


class OutcomeWriter(Protocol):
    """What execution needs of `job_outcomes`; `JobOutcomes` satisfies it."""

    def get(self, tx: Transaction, lock_key: str) -> OutcomeRow | None: ...

    def record(self, tx: Transaction, job: Job[Any], *, status: OutcomeStatus,
               reason: str | None, retry_not_before: float | None,
               now: float) -> OutcomeRow: ...


def _handler_table(handlers: Sequence[Handler[Any, Any]],
                   catalog: Sequence[type[Job[Any]]]) -> dict[type[Job[Any]], Handler[Any, Any]]:
    """Boot checks: every rule a wiring mistake can break fails here, at worker start."""
    table: dict[type[Job[Any]], Handler[Any, Any]] = {}
    for handler in handlers:
        job_type = handler_job_type(handler)  # TypeError: hints missing, wrong or unregistered
        if job_type in table:
            raise ValueError(f"two handlers for {job_type.job_name}")
        table[job_type] = handler
    known = set(catalog)
    missing = sorted(t.job_name for t in known - table.keys())
    if missing:
        raise ValueError(f"no handler for {missing}")
    stray = sorted(t.job_name for t in table.keys() - known)
    if stray:
        raise ValueError(f"handlers for job types outside the catalog: {stray}")
    return table


class JobExecutor:
    """Runs one delivery of a job and commits its outcome."""

    def __init__(self, handlers: Sequence[Handler[Any, Any]], *, transactions: Transactions,
                 outcomes: OutcomeWriter, assets: AssetRecords, clock: Clock,
                 redeliver: Callable[[Redelivery], Awaitable[None]],
                 catalog: Sequence[type[Job[Any]]] = CATALOG) -> None:
        self._handlers = _handler_table(handlers, catalog)
        self._transactions = transactions
        self._outcomes = outcomes
        self._assets = assets
        self._clock = clock
        self._redeliver = redeliver
        self._queues = frozenset(job_type.delivery.queue for job_type in catalog)

    @property
    def queues(self) -> frozenset[QueueName]:
        return self._queues

    async def execute(self, job: Job[Any], attempt: int) -> OutcomeStatus | None:
        """The committed outcome's status, or None for an early copy deferred without running."""
        handler = self._handlers.get(type(job))
        if handler is None:
            raise TypeError(f"{type(job).__name__} is not in this executor's catalog")
        lock = job_keys(job).lock

        stored = await asyncio.to_thread(self._read, lock)
        if (stored is not None and stored.status == "transient"
                and stored.retry_not_before is not None
                and self._clock.utc() < stored.retry_not_before
                - EARLY_COPY_TOLERANCE.total_seconds()):
            await self._redeliver(Redelivery(job, attempt, stored.retry_not_before))
            return None

        try:
            result = await handler.handle(job)
            if type(job).asset_kind is not None and not isinstance(result, AssetReady):
                raise TypeError(f"{type(job).__name__} handler returned {result!r}")
        except TerminalFailure as failure:
            await self._record(job, "terminal", failure.reason, None)
            return "terminal"
        except TransientFailure as failure:
            return await self._transient(job, attempt, failure.reason, failure.retry_after)
        except Exception:
            logger.exception("handler bug: %s raised an unclassified exception",
                             type(job).job_name)
            return await self._transient(job, attempt, UNCLASSIFIED_ERROR, None)

        try:
            await asyncio.to_thread(self._record_ok, job, result, self._clock.utc())
        except ProducedFactsConflict:
            logger.exception("bug: %s produced facts that differ from the recorded facts",
                             type(job).job_name)
            await self._record(job, "terminal", FACTS_CONFLICT, None)
            return "terminal"
        return "ok"

    async def _transient(self, job: Job[Any], attempt: int, reason: str,
                         retry_after: timedelta | None) -> OutcomeStatus:
        retry = type(job).delivery.retry
        floor = retry_after if retry_after is not None else timedelta(0)
        now = self._clock.utc()
        if attempt < len(retry):
            delay = min(max(retry[attempt], floor), MAX_RETRY_DELAY)
            not_before = now + delay.total_seconds()
            await self._record(job, "transient", reason, not_before, now=now)
            await self._redeliver(Redelivery(job, attempt + 1, not_before))
        else:
            not_before = now + min(floor, MAX_RETRY_DELAY).total_seconds()
            await self._record(job, "transient", reason, not_before, now=now)
        return "transient"

    # -- blocking work, always on a worker thread ------------------------------------------------

    def _read(self, lock: str) -> OutcomeRow | None:
        with self._transactions.begin() as tx:
            return self._outcomes.get(tx, lock)

    def _record_ok(self, job: Job[Any], result: Any, now: float) -> None:
        with self._transactions.begin() as tx:
            if type(job).asset_kind is not None:
                self._assets.record_produced(tx, asset_key(job), result)
            self._outcomes.record(tx, job, status="ok", reason=None, retry_not_before=None,
                                  now=now)

    async def _record(self, job: Job[Any], status: OutcomeStatus, reason: str,
                      retry_not_before: float | None, *, now: float | None = None) -> None:
        at = self._clock.utc() if now is None else now

        def write() -> None:
            with self._transactions.begin() as tx:
                self._outcomes.record(tx, job, status=status, reason=reason,
                                      retry_not_before=retry_not_before, now=at)

        await asyncio.to_thread(write)
