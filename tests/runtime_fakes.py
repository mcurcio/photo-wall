"""Lane A's private test support: an in-memory `job_outcomes`, stub handlers for the whole
catalog, and the procrastinate schema installer the DB tests share.

Handler classes import their job types at runtime (never under TYPE_CHECKING): the runtime reads
`handle`'s annotations to dispatch.
"""

from __future__ import annotations

import threading
from collections.abc import Collection
from typing import Any

import procrastinate

from central.infra.outcomes import OutcomeRow, OutcomeStatus
from central.kernel.assets import AssetReady
from central.kernel.job_types import (
    FetchOsImage,
    FetchPackage,
    Prefetch,
    PurgeFinishedJobs,
    RescueStalledJobs,
    SyncReleases,
)
from central.kernel.jobs import Job, job_keys
from central.kernel.transactions import Transaction

FACTS = AssetReady(size=7, sha256="ef" * 32)


def _require_open(tx: Transaction) -> None:
    if tx.state != "open":
        raise AssertionError(f"outcomes used outside an open transaction ({tx.state})")


class InMemoryOutcomes:
    """`JobOutcomes` semantics in memory (writes apply immediately; there is no rollback).

    `log` lists every record call as (lock, status, reason, retry_not_before, now), and `events`
    (shared with a test) receives ("outcome", status) so ordering against redeliveries can be
    asserted.
    """

    def __init__(self, events: list[tuple[str, Any]] | None = None) -> None:
        self.rows: dict[str, OutcomeRow] = {}
        self.log: list[tuple[str, str, str | None, float | None, float]] = []
        self.events = events if events is not None else []
        self._seq = 0
        self._mutex = threading.Lock()

    def get(self, tx: Transaction, lock_key: str) -> OutcomeRow | None:
        _require_open(tx)
        return self.rows.get(lock_key)

    def get_many(self, tx: Transaction, lock_keys: Collection[str]) -> dict[str, OutcomeRow]:
        _require_open(tx)
        return {key: self.rows[key] for key in lock_keys if key in self.rows}

    def record(self, tx: Transaction, job: Job[Any], *, status: OutcomeStatus,
               reason: str | None, retry_not_before: float | None, now: float) -> OutcomeRow:
        _require_open(tx)
        lock = job_keys(job).lock
        with self._mutex:
            self._seq += 1
            old = self.rows.get(lock)
            failing_since = None if status == "ok" else (
                old.failing_since if old is not None and old.failing_since is not None else now)
            row = OutcomeRow(lock, type(job).job_name, status, reason, retry_not_before,
                             failing_since, self._seq, now)
            self.rows[lock] = row
            self.log.append((lock, status, reason, retry_not_before, now))
            self.events.append(("outcome", status))
        return row

    def purge(self, tx: Transaction, *, older_than: float) -> int:
        _require_open(tx)
        stale = [key for key, row in self.rows.items() if row.updated_at < older_than]
        for key in stale:
            del self.rows[key]
        return len(stale)


class FetchOsImageStub:
    async def handle(self, job: FetchOsImage) -> AssetReady:
        return FACTS


class FetchPackageStub:
    async def handle(self, job: FetchPackage) -> AssetReady:
        return FACTS


class SyncReleasesStub:
    async def handle(self, job: SyncReleases) -> None:
        return None


class PrefetchStub:
    async def handle(self, job: Prefetch) -> None:
        return None


class RescueStub:
    async def handle(self, job: RescueStalledJobs) -> None:
        return None


class PurgeStub:
    async def handle(self, job: PurgeFinishedJobs) -> None:
        return None


def catalog_stubs() -> list[Any]:
    return [FetchOsImageStub(), FetchPackageStub(), SyncReleasesStub(), PrefetchStub(),
            RescueStub(), PurgeStub()]


def apply_procrastinate_schema(dsn: str) -> None:
    """Install procrastinate's schema into the (test-private) schema `dsn` searches."""
    app = procrastinate.App(connector=procrastinate.SyncPsycopgConnector(conninfo=dsn))
    app.open()
    try:
        app.schema_manager.apply_schema()
    finally:
        app.close()
