"""The procrastinate `Publisher` (kernel contract PB1-PB9, `central/kernel/publishing.py`).

`publish` runs in a SAVEPOINT of the caller's transaction (PB5): it reads the key's latest
outcome (`since`), then defers through `job_queue.defer`, whose own savepoint turns a merge into
success. A failed statement in either rolls back only the savepoint, so a caller that catches a
publish failure keeps a working transaction.
Inside a retry window, or after a terminal outcome without `retry_terminal`, it inserts nothing
and returns a `SettledHandle`.

`publish_now` is `publish` in its own transaction on a worker thread (a short DB write, not a
wait). DEVIATION from the design's "small async psycopg pool" (§10.2): no async pool is needed,
because the only async caller does one short write through the existing sync pool.

A handle waits on the process's `OutcomeFeed`. The worker has none (`feed=None`); waiting there
raises `RuntimeError("no_outcome_feed")`. An asset job's `Ready.result` is read from its Asset
record, never from the outcome row.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import procrastinate

from central.infra.job_queue import build_app, defer
from central.infra.outcome_feed import OutcomeFeed
from central.infra.outcomes import JobOutcomes, OutcomeRow
from central.infra.transactions import PgTransactions, pg_connection
from central.kernel.job_types import CATALOG
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
from central.kernel.transactions import Transaction
from contracts.time import Clock


class ProcrastinatePublisher:
    """Implements `Publisher` over procrastinate and `job_outcomes`."""

    def __init__(self, dsn: str, *, transactions: PgTransactions, outcomes: JobOutcomes,
                 assets: AssetRecords, clock: Clock, feed: OutcomeFeed | None,
                 job_types: Sequence[type[Job[Any]]] = CATALOG) -> None:
        # Publish-only: every insert goes through the caller's connection, so this connector's
        # own pool is never opened.
        self._app = build_app(procrastinate.SyncPsycopgConnector(conninfo=dsn), job_types, None)
        self._job_types = frozenset(job_types)
        self._transactions = transactions
        self._outcomes = outcomes
        self._assets = assets
        self._clock = clock
        self._feed = feed

    def publish(self, job: Job[R], *, within: Transaction,
                retry_terminal: bool = False) -> JobHandle[R]:
        keys = job_keys(job)  # PB1: TypeError for an unregistered job type
        if type(job) not in self._job_types:
            raise TypeError(f"{type(job).__name__} is not published by this publisher")
        if within.state != "open":
            raise RuntimeError("transaction_not_open")
        connection = pg_connection(within)
        with connection.transaction():  # PB5: the whole publish, outcome read included
            latest = self._outcomes.get(within, keys.lock)
            now = self._clock.utc()
            if (latest is not None and latest.status == "transient"
                    and latest.retry_not_before is not None and latest.retry_not_before > now):
                return SettledHandle(Failed(  # PB2
                    False, latest.reason or "", timedelta(seconds=latest.retry_not_before - now)))
            if latest is not None and latest.status == "terminal" and not retry_terminal:
                return SettledHandle(Failed(True, latest.reason or "", None))  # PB3
            since = latest.seq if latest is not None else 0  # PB4: since first, then defer
            defer(self._app, job, attempt=0, connection=connection)
        return _OutcomeHandle(self, job, keys.lock, since, within)

    async def publish_now(self, job: Job[R], *, retry_terminal: bool = False) -> JobHandle[R]:
        job_keys(job)  # PB1, before any transaction

        def publish_committed() -> JobHandle[R]:
            with self._transactions.begin() as tx:  # PB8: committed before returning
                return self.publish(job, within=tx, retry_terminal=retry_terminal)

        return await asyncio.to_thread(publish_committed)

    async def _wait(self, job: Job[Any], lock: str, since: int,
                    timeout: timedelta) -> Ready[Any] | Failed | Pending:
        if self._feed is None:
            raise RuntimeError("no_outcome_feed")
        row = await self._feed.wait_for(lock, since=since, timeout=timeout)
        if row is None:
            return Pending()  # the job is not cancelled
        return await self._outcome(job, row)

    async def _outcome(self, job: Job[Any], row: OutcomeRow) -> Ready[Any] | Failed:
        """PB7: the stored row as the kernel's outcome value."""
        if row.status == "terminal":
            return Failed(True, row.reason or "", None)
        if row.status == "transient":
            remaining = (row.retry_not_before or 0.0) - self._clock.utc()
            return Failed(False, row.reason or "", timedelta(seconds=max(0.0, remaining)))
        if type(job).asset_kind is None:
            return Ready(None)
        facts = await asyncio.to_thread(self._produced, job)
        if facts is None:  # the asset row is gone (or never referenced): nothing to serve
            return Failed(False, ASSET_NOT_RECORDED, timedelta(0))
        return Ready(facts)

    def _produced(self, job: Job[Any]) -> Any:
        with self._transactions.begin() as tx:
            asset = self._assets.get(tx, asset_key(job))
        return None if asset is None else asset.produced


class _OutcomeHandle:
    """A cheap value: (job, lock key, since) plus the transaction it was published in."""

    __slots__ = ("_job", "_lock", "_publisher", "_since", "_within")

    def __init__(self, publisher: ProcrastinatePublisher, job: Job[Any], lock: str, since: int,
                 within: Transaction) -> None:
        self._publisher = publisher
        self._job = job
        self._lock = lock
        self._since = since
        self._within = within

    async def wait(self, *, timeout: timedelta) -> Ready[Any] | Failed | Pending:
        state = self._within.state
        if state == "open":
            raise RuntimeError("await_after_commit")  # PB6
        if state == "rolled_back":
            return NOT_PUBLISHED
        return await self._publisher._wait(self._job, self._lock, self._since, timeout)
