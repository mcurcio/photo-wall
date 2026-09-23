"""One process-wide signal for job outcomes (design §10.2 "Signal").

Each process holds ONE `OutcomeFeed` with ONE `LISTEN job_outcome` connection. Waiters for the
same lock key share one entry. The NOTIFY payload is only the lock key, so the feed reads the
stored rows for the keys it was told about. Every `recheck` it also reads the rows of EVERY key
that has waiters, so a lost NOTIFY (the LISTEN connection dropped, or the outcome committed
before the waiter registered) costs at most one recheck, never a missed outcome.

A waiter registers BEFORE it reads the stored row, so an outcome committed at any moment is seen
either by that read or by a later NOTIFY/recheck. A timeout returns None and a cancellation only
unregisters; neither touches the job.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

import psycopg

from central.infra.outcomes import OUTCOME_CHANNEL, JobOutcomes, OutcomeRow
from central.kernel.transactions import Transactions

logger = logging.getLogger(__name__)

APPLICATION_NAME: Final = "photo-wall-outcome-feed"


@dataclass(eq=False)
class _Entry:
    waiters: int = 0
    row: OutcomeRow | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)


class OutcomeFeed:
    """Resolves waiters on committed outcomes; start it once per process, on its event loop."""

    def __init__(self, dsn: str, *, transactions: Transactions, outcomes: JobOutcomes,
                 recheck: timedelta = timedelta(seconds=1)) -> None:
        if not isinstance(recheck, timedelta) or recheck <= timedelta(0):
            raise ValueError("invalid_recheck")
        self._dsn = dsn
        self._transactions = transactions
        self._outcomes = outcomes
        self._recheck = recheck.total_seconds()
        self._entries: dict[str, _Entry] = {}
        self._dirty: set[str] = set()
        self._poke: asyncio.Event | None = None
        self._next_full = 0.0
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        if self._tasks:
            raise RuntimeError("outcome_feed_started")
        self._poke = asyncio.Event()
        self._tasks = [asyncio.create_task(self._listen(), name="outcome feed listen"),
                       asyncio.create_task(self._refresh(), name="outcome feed recheck")]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_for(self, lock_key: str, *, since: int,
                       timeout: timedelta) -> OutcomeRow | None:
        """The stored outcome once its sequence exceeds `since`; None on timeout."""
        if not self._tasks:
            raise RuntimeError("outcome_feed_not_started")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout.total_seconds())
        entry = self._entries.get(lock_key)
        if entry is None:
            entry = self._entries[lock_key] = _Entry()
        entry.waiters += 1  # registered before the read: no outcome can slip between them
        try:
            if entry.row is None or entry.row.seq <= since:
                self._offer(await asyncio.to_thread(self._read, (lock_key,)))
            while True:
                row = entry.row
                if row is not None and row.seq > since:
                    return row
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                changed = entry.changed
                try:
                    await asyncio.wait_for(changed.wait(), remaining)
                except TimeoutError:
                    pass  # check the row once more, then time out
        finally:
            entry.waiters -= 1
            if entry.waiters == 0 and self._entries.get(lock_key) is entry:
                del self._entries[lock_key]

    # -- internals (all on the feed's event loop) --------------------------------------------------

    def _read(self, lock_keys: Collection[str]) -> dict[str, OutcomeRow]:
        with self._transactions.begin() as tx:
            return self._outcomes.get_many(tx, lock_keys)

    def _offer(self, rows: dict[str, OutcomeRow]) -> None:
        for key, row in rows.items():
            entry = self._entries.get(key)
            if entry is None or (entry.row is not None and entry.row.seq >= row.seq):
                continue
            entry.row = row
            changed, entry.changed = entry.changed, asyncio.Event()
            changed.set()

    def _notified(self, lock_key: str) -> None:
        if lock_key in self._entries and self._poke is not None:
            self._dirty.add(lock_key)
            self._poke.set()

    def _full_recheck_now(self) -> None:
        self._next_full = 0.0
        if self._poke is not None:
            self._poke.set()

    async def _listen(self) -> None:
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._dsn, autocommit=True, application_name=APPLICATION_NAME,
                ) as conn:
                    await conn.execute(f"LISTEN {OUTCOME_CHANNEL}")
                    self._full_recheck_now()  # whatever was missed while not listening
                    async for notify in conn.notifies():
                        self._notified(notify.payload)
            except Exception as error:  # the recheck keeps waiters resolving meanwhile
                logger.warning("outcome feed LISTEN connection lost: %s", error)
            await asyncio.sleep(self._recheck)

    async def _refresh(self) -> None:
        assert self._poke is not None
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.wait_for(self._poke.wait(), self._recheck)
            except TimeoutError:
                pass
            self._poke.clear()
            if loop.time() >= self._next_full:
                self._next_full = loop.time() + self._recheck
                keys = set(self._entries)
            else:
                keys = self._dirty & self._entries.keys()
            self._dirty.clear()
            if not keys:
                continue
            try:
                rows = await asyncio.to_thread(self._read, keys)
            except Exception as error:
                logger.warning("outcome feed recheck failed: %s", error)
                continue
            self._offer(rows)
