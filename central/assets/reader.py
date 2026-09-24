"""The read-through path every asset request takes (design §1 rule 2, §6(b)-(c), §10.2).

Open the first candidate on disk. When that is the first (wanted) candidate, publish nothing;
when it is a substitute, publish the wanted candidate's fetch job in the same transaction as the
open (PB5's savepoint keeps a failed publish from aborting it; the failure is logged, never
served). Otherwise take a waiter slot, publish the first candidate's fetch job and await its
handle up to `wait_timeout`, then open it or report why not. Every request publish sets
`retry_terminal` (decision 3). The route cancels `read` when the client disconnects;
cancellation releases the slot, closes any fd opened but not returned, and never cancels a job
or the open's transaction (the open is shielded, so its publish still commits).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from central.assets.store import CacheStore
from central.kernel.assets import AssetKey, AssetReady
from central.kernel.job_types import AssetJob
from central.kernel.jobs import asset_key
from central.kernel.ports import AssetRecords, Candidates
from central.kernel.publishing import Failed, JobHandle, Pending, Publisher, Ready
from central.kernel.transactions import Transaction, Transactions
from contracts.time import Clock

LOG = logging.getLogger("central.assets.reader")

_BUSY_RETRY_SECONDS = 5
_TIMEOUT_RETRY_SECONDS = 5
_ABSENT_AFTER_READY_RETRY_SECONDS = 1
_TERMINAL_RETRY_SECONDS = 30


@dataclass(frozen=True, slots=True)
class Opened:
    job: AssetJob
    fd: int  # the caller owns (closes) it
    size: int
    sha256: str  # -> the Digest header


@dataclass(frozen=True, slots=True)
class Unavailable:
    reason: str
    retry_after_seconds: int  # -> 503 + Retry-After


class SlotsFull(Exception):
    """Every waiter slot is taken."""


class WaiterSlots:
    """A non-blocking bulkhead on concurrent waiters (stdlib only; anyio is not a declared dep)."""

    def __init__(self, capacity: int) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("invalid_capacity")
        self._capacity = capacity
        self._in_use = 0
        self._lock = threading.Lock()

    def claim(self) -> AbstractContextManager[None]:
        """Hold one slot for the `with` block; entering raises `SlotsFull` at capacity."""
        return self._claim()

    @contextmanager
    def _claim(self) -> Iterator[None]:
        with self._lock:
            if self._in_use >= self._capacity:
                raise SlotsFull
            self._in_use += 1
        try:
            yield
        finally:
            with self._lock:
                self._in_use -= 1

    @property
    def in_use(self) -> int:
        return self._in_use


def _close_abandoned(future: asyncio.Future[Opened | None]) -> None:
    """Done-callback for an open whose caller was cancelled: close the fd nobody will own."""
    if future.cancelled() or future.exception() is not None:
        return
    opened = future.result()
    if opened is not None:
        os.close(opened.fd)


async def _open_in_thread(fn: Callable[..., Opened | None], *args: Any) -> Opened | None:
    """Run a blocking open in a thread; if the caller is cancelled, the fd is closed, not leaked."""
    future = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        future.add_done_callback(_close_abandoned)
        raise


class AssetReader:
    def __init__(self, *, store: CacheStore, records: AssetRecords, transactions: Transactions,
                 publisher: Publisher, slots: WaiterSlots, clock: Clock,
                 wait_timeout: timedelta = timedelta(seconds=30),
                 touch_interval: timedelta = timedelta(minutes=5)) -> None:
        self._store = store
        self._records = records
        self._transactions = transactions
        self._publisher = publisher
        self._slots = slots
        self._clock = clock
        self._wait_timeout = wait_timeout
        self._touch_interval = touch_interval.total_seconds()
        self._touched: dict[AssetKey, float] = {}  # per process: key -> last touch attempt

    async def read(self, candidates: Candidates) -> Opened | Unavailable:
        wanted = candidates.jobs[0]
        opened = await _open_in_thread(self._open_first, candidates.jobs)
        if opened is None:
            try:
                with self._slots.claim():
                    result = await self._produce_and_open(wanted)
            except SlotsFull:
                return Unavailable("busy", _BUSY_RETRY_SECONDS)
            if isinstance(result, Unavailable):
                return result
            opened = result
        try:
            await self._touch(opened.job)
        except BaseException:  # cancelled after the open: the caller will never own the fd
            os.close(opened.fd)
            raise
        return opened

    async def _publish_request(self, job: AssetJob) -> JobHandle[AssetReady]:
        """A miss's publish: a request may retry a terminal (decision 3)."""
        return await self._publisher.publish_now(job, retry_terminal=True)

    async def _produce_and_open(self, job: AssetJob) -> Opened | Unavailable:
        handle = await self._publish_request(job)
        outcome = await handle.wait(timeout=self._wait_timeout)
        if isinstance(outcome, Ready):
            opened = await _open_in_thread(self._open, job, outcome.result)
            if opened is None:
                return Unavailable("absent_after_ready", _ABSENT_AFTER_READY_RETRY_SECONDS)
            return opened
        if isinstance(outcome, Failed):
            if outcome.terminal:
                return Unavailable(outcome.reason, _TERMINAL_RETRY_SECONDS)
            assert outcome.retry_after is not None  # Failed guarantees it when transient
            return Unavailable(outcome.reason,
                               max(1, math.ceil(outcome.retry_after.total_seconds())))
        assert isinstance(outcome, Pending)
        return Unavailable("timeout", _TIMEOUT_RETRY_SECONDS)

    def _open_first(self, jobs: tuple[AssetJob, ...]) -> Opened | None:
        """One transaction: the first candidate with produced facts whose file opens; serving a
        substitute also publishes the wanted (first) candidate's fetch, committed with it."""
        opened: Opened | None = None
        try:
            with self._transactions.begin() as tx:
                for job in jobs:
                    asset = self._records.get(tx, asset_key(job))
                    if asset is None or asset.produced is None:
                        continue
                    opened = self._open(job, asset.produced)
                    if opened is not None:
                        break
                if opened is not None and opened.job != jobs[0]:
                    self._publish_wanted(jobs[0], tx)
        except BaseException:  # e.g. the commit failed after an open: nobody owns the fd
            if opened is not None:
                os.close(opened.fd)
            raise
        return opened

    def _publish_wanted(self, job: AssetJob, tx: Transaction) -> None:
        """A substitute serve's request publish (decision 3); a failure is logged, never served.

        PB5 runs the publish in a savepoint of `tx`, so a failed statement leaves `tx` usable.
        """
        try:
            self._publisher.publish(job, within=tx, retry_terminal=True)
        except Exception:
            LOG.warning("fetch publish for %s failed while serving a substitute", asset_key(job),
                        exc_info=True)

    def _open(self, job: AssetJob, facts: AssetReady) -> Opened | None:
        file = self._store.open(asset_key(job), facts)
        if file is None:
            return None
        return Opened(job=job, fd=file.fd, size=file.size, sha256=facts.sha256)

    async def _touch(self, job: AssetJob) -> None:
        """Record `last_served_at` at most once per interval per key; failures never fail a serve."""
        key = asset_key(job)
        now = self._clock.utc()
        last = self._touched.get(key)
        if last is not None and now - last < self._touch_interval:
            return
        self._touched[key] = now
        try:
            await asyncio.to_thread(self._touch_record, key, now)
        except Exception:
            LOG.warning("last_served_at touch failed for %s", key, exc_info=True)

    def _touch_record(self, key: AssetKey, at: float) -> None:
        with self._transactions.begin() as tx:
            self._records.touch_served(tx, key, at)
