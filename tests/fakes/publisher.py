"""An in-memory `Publisher` honouring PB1-PB9 (`central/kernel/publishing.py`).

`record_outcome` stands in for the job runtime: it writes the key's latest outcome, consumes the
job's pending copy, writes an asset job's produced facts to `assets` (when given, through
`transactions`), and wakes waiters. Waiters may live on any event loop; `record_outcome` may be
called from any thread.

It is the `Publisher` port's test double: it models the PB1-PB9 contract, not the SQL, and the
shared conformance suite (`test_publisher_conformance.py`) runs it beside `ProcrastinatePublisher`
on PostgreSQL, so the two cannot drift.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from central.kernel.jobs import Job, R, asset_key, job_keys
from central.kernel.ports import AssetRecords
from central.kernel.publishing import (
    ASSET_NOT_RECORDED,
    NOT_PUBLISHED,
    Failed,
    JobHandle,
    Pending,
    Ready,
    SettledHandle,
)
from central.kernel.transactions import Transaction, Transactions
from contracts.time import Clock
from fakes.transactions import FakeTransaction


@dataclass(frozen=True)
class PublishedCall:
    job: Job[Any]
    retry_terminal: bool
    within: Transaction | None  # None for publish_now


@dataclass(frozen=True)
class _Outcome:
    seq: int
    status: Literal["ok", "transient", "terminal"]
    reason: str | None
    retry_not_before: float | None
    result: Any


@dataclass(frozen=True, eq=False)
class _Deferred:
    job: Job[Any]
    within: Transaction


class RecordingPublisher:
    """Implements `Publisher` in memory.

    `calls` records every accepted call, including those PB2/PB3 suppress. `inserted` is what a
    real queue would hold: deferrals that were neither suppressed, merged into a pending copy, nor
    rolled back with their transaction.
    """

    def __init__(self, clock: Clock, assets: AssetRecords | None = None,
                 transactions: Transactions | None = None) -> None:
        if (assets is None) != (transactions is None):
            raise ValueError("assets and transactions go together")
        self.clock = clock
        self.assets = assets
        self.transactions = transactions
        self.calls: list[PublishedCall] = []
        self._deferred: list[_Deferred] = []
        self._pending: dict[str, _Deferred] = {}  # queueing lock -> the pending copy
        self._outcomes: dict[str, _Outcome] = {}  # lock -> latest outcome
        self._seq = 0
        self._waiters: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]]] = {}
        self._mutex = threading.Lock()

    @property
    def inserted(self) -> list[Job[Any]]:
        return [d.job for d in self._deferred if d.within.state != "rolled_back"]

    def publish(self, job: Job[R], *, within: Transaction,
                retry_terminal: bool = False) -> JobHandle[R]:
        job_keys(job)  # PB1: TypeError for an unregistered job type
        if within.state != "open":
            raise RuntimeError("transaction_not_open")
        self.calls.append(PublishedCall(job, retry_terminal, within))
        return self._publish(job, within, retry_terminal)

    async def publish_now(self, job: Job[R], *, retry_terminal: bool = False) -> JobHandle[R]:
        job_keys(job)  # PB1
        self.calls.append(PublishedCall(job, retry_terminal, None))
        tx = FakeTransaction()  # PB8: its own transaction, committed before returning
        handle = self._publish(job, tx, retry_terminal)
        tx.state = "committed"
        return handle

    def _publish(self, job: Job[R], within: Transaction, retry_terminal: bool) -> JobHandle[R]:
        keys = job_keys(job)
        now = self.clock.utc()
        with self._mutex:
            latest = self._outcomes.get(keys.lock)
            if (latest is not None and latest.status == "transient"
                    and latest.retry_not_before is not None and latest.retry_not_before > now):
                return SettledHandle(Failed(  # PB2
                    False, latest.reason or "", timedelta(seconds=latest.retry_not_before - now)))
            if latest is not None and latest.status == "terminal" and not retry_terminal:
                return SettledHandle(Failed(True, latest.reason or "", None))  # PB3
            since = latest.seq if latest is not None else 0  # PB4: read since, then defer
            pending = self._pending.get(keys.queueing_lock)
            if pending is None or pending.within.state == "rolled_back":
                deferred = _Deferred(job, within)
                self._pending[keys.queueing_lock] = deferred
                self._deferred.append(deferred)
            # else: merged into the pending copy (PB4, PB9), which counts as success
        return _RecordingHandle(self, job, keys.lock, since, within)

    def record_outcome(self, job: Job[Any], outcome: Ready[Any] | Failed,
                       *, retry_not_before: float | None = None) -> None:
        """Simulate the runtime finishing a run of `job`; wakes every waiter on its lock.

        A transient `Failed` without `retry_not_before` gets `now + retry_after`.
        """
        keys = job_keys(job)
        if isinstance(outcome, Ready):
            if retry_not_before is not None:
                raise ValueError("retry_not_before is only for transient outcomes")
            status: Literal["ok", "transient", "terminal"] = "ok"
            if self.assets is not None and type(job).asset_kind is not None:
                with self.transactions.begin() as tx:  # type: ignore[union-attr]
                    self.assets.record_produced(tx, asset_key(job), outcome.result)
        elif isinstance(outcome, Failed) and outcome.terminal:
            if retry_not_before is not None:
                raise ValueError("retry_not_before is only for transient outcomes")
            status = "terminal"
        elif isinstance(outcome, Failed):
            status = "transient"
            if retry_not_before is None:
                assert outcome.retry_after is not None  # Failed guarantees it when transient
                retry_not_before = self.clock.utc() + outcome.retry_after.total_seconds()
        else:
            raise TypeError(f"not an outcome: {outcome!r}")
        with self._mutex:
            self._seq += 1
            self._outcomes[keys.lock] = _Outcome(
                seq=self._seq, status=status,
                reason=None if isinstance(outcome, Ready) else outcome.reason,
                retry_not_before=retry_not_before,
                result=outcome.result if isinstance(outcome, Ready) else None,
            )
            self._pending.pop(keys.queueing_lock, None)
            waiters = list(self._waiters.get(keys.lock, ()))
        for loop, future in waiters:
            try:
                loop.call_soon_threadsafe(_wake, future)
            except RuntimeError:  # that waiter's loop is closed
                pass

    # -- handle support ---------------------------------------------------------------------------

    def _register(self, lock: str,
                  entry: tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]) -> None:
        with self._mutex:
            self._waiters.setdefault(lock, set()).add(entry)

    def _unregister(self, lock: str,
                    entry: tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]) -> None:
        with self._mutex:
            entries = self._waiters.get(lock)
            if entries is not None:
                entries.discard(entry)
                if not entries:
                    del self._waiters[lock]

    def _resolve(self, job: Job[Any], lock: str, since: int) -> Ready[Any] | Failed | None:
        """PB7: the first outcome with a sequence greater than `since`, or None."""
        with self._mutex:
            latest = self._outcomes.get(lock)
        if latest is None or latest.seq <= since:
            return None
        if latest.status == "ok":
            return self._ok(job, latest.result)
        if latest.status == "terminal":
            return Failed(True, latest.reason or "", None)
        remaining = max(0.0, (latest.retry_not_before or 0.0) - self.clock.utc())
        return Failed(False, latest.reason or "", timedelta(seconds=remaining))

    def _ok(self, job: Job[Any], result: Any) -> Ready[Any] | Failed:
        """An asset job's `Ready.result` is read from the Asset record when records are given;
        an absent record (or one without produced facts) is `ASSET_NOT_RECORDED` (PB7)."""
        if self.assets is None or type(job).asset_kind is None:
            return Ready(result)
        with self.transactions.begin() as tx:  # type: ignore[union-attr]
            asset = self.assets.get(tx, asset_key(job))
        if asset is None or asset.produced is None:
            return Failed(False, ASSET_NOT_RECORDED, timedelta(0))
        return Ready(asset.produced)


def _wake(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


class _RecordingHandle:
    def __init__(self, publisher: RecordingPublisher, job: Job[Any], lock: str, since: int,
                 within: Transaction) -> None:
        self._publisher = publisher
        self._job = job
        self._lock = lock
        self._since = since
        self._within = within

    async def wait(self, *, timeout: timedelta) -> Ready[Any] | Failed | Pending:
        if self._within.state == "open":
            raise RuntimeError("await_after_commit")  # PB6
        if self._within.state == "rolled_back":
            return NOT_PUBLISHED
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout.total_seconds())
        while True:
            entry = (loop, loop.create_future())
            self._publisher._register(self._lock, entry)  # register before checking: no lost wake
            try:
                outcome = self._publisher._resolve(self._job, self._lock, self._since)
                if outcome is not None:
                    return outcome
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return Pending()  # the job is not cancelled
                try:
                    await asyncio.wait_for(entry[1], remaining)
                except TimeoutError:
                    pass  # re-check once more, then Pending
            finally:
                self._publisher._unregister(self._lock, entry)  # also on cancellation (PB7)
