"""Readiness is the data; an outcome is a note (`docs/central-idempotent-jobs.md` rule 3, §11).

One `FetchPackage` key on PostgreSQL: the real `AssetRecords`, `job_outcomes`, procrastinate queue
and `OutcomeFeed`. State is seeded through `AssetRecords`, `JobOutcomes` and the cache disk only.
`CallRecordingPublisher` only notes each call before the real publish runs; it models nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from datetime import timedelta

from content_db import put_file
from fakes.catalog import StaticContentCatalog
from runtime_fakes import apply_procrastinate_schema

from central.assets.handlers import PrefetchHandler
from central.assets.layout import CacheLayout
from central.assets.reader import AssetReader, Opened, Unavailable, WaiterSlots
from central.assets.store import CacheStore
from central.infra.asset_records import PgAssetRecords
from central.infra.job_queue import decode
from central.infra.outcome_feed import OutcomeFeed
from central.infra.outcomes import JobOutcomes, OutcomeStatus
from central.infra.publisher import ProcrastinatePublisher
from central.infra.transactions import PgTransactions
from central.kernel.assets import AssetReady, AssetReference, OriginLocator
from central.kernel.job_types import FetchPackage, Prefetch
from central.kernel.jobs import asset_key, job_keys
from central.kernel.ports import Candidates
from contracts.time import ManualClock

DEB = b"a player package " * 100
SHA = hashlib.sha256(DEB).hexdigest()
FACTS = AssetReady(size=len(DEB), sha256=SHA)
JOB = FetchPackage(sha256=SHA)
KEY = asset_key(JOB)
TAG = "v1.0.0"
LOCATOR = OriginLocator(f"https://example.test/{SHA}.deb", SHA, len(DEB))


@dataclass(frozen=True)
class Call:
    job: object
    retry_terminal: bool


class CallRecordingPublisher(ProcrastinatePublisher):
    """The real publisher; `calls` lists every publish (and so every `publish_now`) it received."""

    def __init__(self, dsn, **options) -> None:
        super().__init__(dsn, **options)
        self.calls: list[Call] = []

    def publish(self, job, *, within, retry_terminal=False):
        self.calls.append(Call(job, retry_terminal))
        return super().publish(job, within=within, retry_terminal=retry_terminal)


class World:
    def __init__(self, registry, tmp_path, *, wait: float) -> None:
        dsn = registry.db.dsn
        apply_procrastinate_schema(dsn)
        self.db = registry.db
        self.clock = ManualClock(1000.0)
        self.transactions = PgTransactions(registry.db)
        self.records = PgAssetRecords(self.clock)
        self.outcomes = JobOutcomes()
        self.store = CacheStore(CacheLayout(tmp_path))
        self.feed = OutcomeFeed(dsn, transactions=self.transactions, outcomes=self.outcomes,
                                recheck=timedelta(milliseconds=50))
        self.publisher = CallRecordingPublisher(
            dsn, transactions=self.transactions, outcomes=self.outcomes, assets=self.records,
            clock=self.clock, feed=self.feed)
        self.reader = AssetReader(store=self.store, records=self.records,
                                  transactions=self.transactions, publisher=self.publisher,
                                  slots=WaiterSlots(4), clock=self.clock,
                                  wait_timeout=timedelta(seconds=wait))
        self.prefetch_publisher = CallRecordingPublisher(
            dsn, transactions=self.transactions, outcomes=self.outcomes, assets=self.records,
            clock=self.clock, feed=None)
        self.prefetch = PrefetchHandler(
            catalog=StaticContentCatalog({}, desired=frozenset({JOB})), records=self.records,
            store=self.store, transactions=self.transactions, publisher=self.prefetch_publisher)
        with self.transactions.begin() as tx:
            self.records.reference(tx, KEY, AssetReference(TAG, LOCATOR, len(DEB), SHA))

    def produce(self, *, ok: bool = True) -> None:
        """The data a copy leaves: the file, then its facts with (by default) `ok` beside them."""
        put_file(self.store, KEY, DEB)
        with self.transactions.begin() as tx:
            self.records.record_produced(tx, KEY, FACTS)
            if ok:
                self.note(tx, "ok")

    def note(self, tx, status: OutcomeStatus) -> None:
        reason = "zombie_failure" if status == "terminal" else None
        self.outcomes.record(tx, JOB, status=status, reason=reason, retry_not_before=None,
                             now=self.clock.utc())

    def late_terminal(self) -> None:
        with self.transactions.begin() as tx:
            self.note(tx, "terminal")

    def status(self) -> OutcomeStatus | None:
        with self.transactions.begin() as tx:
            row = self.outcomes.get(tx, job_keys(JOB).lock)
        return None if row is None else row.status

    def pending(self) -> list:
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT task_name, args FROM procrastinate_jobs "
                                "WHERE status = 'todo' ORDER BY id").fetchall()
        return [decode(row["task_name"], row["args"])[0] for row in rows]

    def run(self, scenario):
        async def main():
            await self.feed.start()
            try:
                return await scenario()
            finally:
                await self.feed.stop()

        return asyncio.run(main())

    def read(self):
        return self.run(lambda: self.reader.read(Candidates((JOB,), pinned=True)))


def served(result) -> bytes:
    assert isinstance(result, Opened), result
    try:
        return os.read(result.fd, result.size + 1)
    finally:
        os.close(result.fd)


async def until(predicate) -> None:
    async with asyncio.timeout(5):
        while not await asyncio.to_thread(predicate):
            await asyncio.sleep(0.02)


def test_d1_a_late_terminal_over_present_data_changes_nothing_a_pi_sees(registry, tmp_path):
    world = World(registry, tmp_path, wait=0.2)
    world.produce()
    world.late_terminal()  # a zombie's note lands after the copy's `ok`
    assert world.status() == "terminal"

    result = world.read()
    assert served(result) == DEB and result.sha256 == SHA
    assert world.publisher.calls == []
    assert world.pending() == []

    assert asyncio.run(world.prefetch.handle(Prefetch())) is None
    assert world.prefetch_publisher.calls == []  # its missing set is empty
    assert world.pending() == []
    assert world.status() == "terminal"  # the note is left alone: readiness never read it


def test_d2_data_that_appears_during_the_wait_is_served_over_a_terminal(registry, tmp_path):
    world = World(registry, tmp_path, wait=5.0)

    async def scenario():
        task = asyncio.create_task(world.reader.read(Candidates((JOB,), pinned=True)))
        await until(lambda: world.pending() == [JOB])  # published; now waiting
        # No `ok` of its own (a lost result write, N3), so the only note the waiter can wake
        # to is the zombie's `terminal`: the outcome it gets is `terminal`, deterministically.
        await asyncio.to_thread(world.produce, ok=False)
        await asyncio.to_thread(world.late_terminal)
        return await task

    result = world.run(scenario)
    assert served(result) == DEB
    assert world.publisher.calls == [Call(JOB, True)]
    assert world.status() == "terminal"


def test_d3_n1_a_terminal_over_wiped_data_waits_for_a_request(registry, tmp_path):
    world = World(registry, tmp_path, wait=0.2)
    world.produce()
    os.remove(world.store.layout.path(KEY))  # the cache is wiped; the facts stay
    world.late_terminal()

    assert asyncio.run(world.prefetch.handle(Prefetch())) is None
    assert world.prefetch_publisher.calls == [Call(JOB, False)]  # a tick never retries (N1)
    assert world.pending() == []  # ... so the terminal note suppresses it

    assert world.read() == Unavailable("timeout", 5)  # no worker runs here
    assert world.publisher.calls == [Call(JOB, True)]
    assert world.pending() == [JOB]  # the request retries it: one run enqueued
