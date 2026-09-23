"""A4: PB1-PB9 over BOTH publishers.

`RecordingPublisher` runs locally; `ProcrastinatePublisher` needs PostgreSQL
(`PHOTO_WALL_TEST_DATABASE_URL`, via the shared `registry` fixture) and skips otherwise.

A harness gives each case the same verbs: `record_outcome` stands in for the job runtime (the
outcome, the produced facts, and the end of the queued copy), `start_running` moves the pending
copy to `doing`, and `inserted` lists what the queue received.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import procrastinate
import pytest
from fakes.asset_records import InMemoryAssetRecords
from fakes.publisher import RecordingPublisher
from fakes.transactions import FakeTransactions
from procrastinate.periodic import PeriodicDeferrer
from runtime_fakes import apply_procrastinate_schema

from central.infra.job_queue import build_app, decode
from central.infra.outcome_feed import OutcomeFeed
from central.infra.outcomes import JobOutcomes
from central.infra.publisher import ProcrastinatePublisher
from central.infra.transactions import PgTransactions, pg_connection
from central.kernel.assets import AssetReady, AssetReference, OriginLocator
from central.kernel.job_types import CATALOG, FetchOsImage, SyncReleases
from central.kernel.jobs import Delivery, Job, QueueName, asset_key, job_keys
from central.kernel.publishing import NOT_PUBLISHED, Failed, Pending, Ready, SettledHandle
from contracts.time import ManualClock

FACTS = AssetReady(size=42, sha256="cd" * 32)
NOW = timedelta(0)
SOON = timedelta(seconds=5)


class Pair(Job[None], name="test.conformance_pair", subject=("a",),
           delivery=Delivery(queue=QueueName.UPKEEP)):
    a: str
    b: str


def _status(outcome: Ready[Any] | Failed) -> tuple[str, str | None]:
    if isinstance(outcome, Ready):
        return "ok", None
    return ("terminal" if outcome.terminal else "transient"), outcome.reason


class RecordingHarness:
    def __init__(self) -> None:
        self.clock = ManualClock(1000.0)
        self.assets = InMemoryAssetRecords()
        self.transactions = FakeTransactions()
        self.publisher = RecordingPublisher(self.clock, self.assets)
        self._writes: list[Any] = []

    @asynccontextmanager
    async def running(self):
        yield

    def record_outcome(self, job, outcome, *, retry_not_before=None):
        self.publisher.record_outcome(job, outcome, retry_not_before=retry_not_before)

    def start_running(self, job) -> None:
        pass  # the fake has no running state; its pending copy is consumed by the outcome

    def inserted(self) -> list[Job[Any]]:
        return self.publisher.inserted

    def waiters(self) -> int:
        return len(self.publisher._waiters)

    def publish_racing_a_finished_run(self, job, outcome):
        # The fake reads `since` and defers atomically; an outcome right after is observably
        # the same as one landing between the two statements.
        with self.transactions.begin() as tx:
            handle = self.publisher.publish(job, within=tx)
            self.record_outcome(job, outcome)
        return handle

    async def insert_periodic_tick(self, job_type) -> None:
        with self.transactions.begin() as tx:
            self.publisher.publish(job_type(), within=tx)

    def caller_write(self, tx) -> None:
        self._writes.append(tx)

    def caller_writes(self) -> int:
        return sum(tx.state == "committed" for tx in self._writes)


class RacingOutcomes(JobOutcomes):
    """`JobOutcomes` whose `get` can run a hook right after it reads (the since-read)."""

    after_get = None

    def get(self, tx, lock_key):
        row = super().get(tx, lock_key)
        if self.after_get is not None:
            self.after_get()
        return row


class ProcrastinateHarness:
    def __init__(self, registry) -> None:
        self.dsn = registry.db.dsn
        apply_procrastinate_schema(self.dsn)
        self.db = registry.db
        with self.db.transaction() as conn:
            conn.execute("CREATE TABLE pb5_probe (x INT)")
        self.clock = ManualClock(1000.0)
        self.assets = InMemoryAssetRecords()
        self.transactions = PgTransactions(self.db)
        self.outcomes = RacingOutcomes()
        self.feed = OutcomeFeed(self.dsn, transactions=self.transactions, outcomes=self.outcomes,
                                recheck=timedelta(milliseconds=200))
        self.publisher = ProcrastinatePublisher(
            self.dsn, transactions=self.transactions, outcomes=self.outcomes, assets=self.assets,
            clock=self.clock, feed=self.feed, job_types=(*CATALOG, Pair))

    @asynccontextmanager
    async def running(self):
        await self.feed.start()
        try:
            yield
        finally:
            await self.feed.stop()

    def record_outcome(self, job, outcome, *, retry_not_before=None):
        status, reason = _status(outcome)
        if status == "transient" and retry_not_before is None:
            retry_not_before = self.clock.utc() + outcome.retry_after.total_seconds()
        with self.transactions.begin() as tx:
            if status == "ok" and type(job).asset_kind is not None:
                self.assets.record_produced(tx, asset_key(job), outcome.result)
            self.outcomes.record(tx, job, status=status, reason=reason,
                                 retry_not_before=retry_not_before, now=self.clock.utc())
            pg_connection(tx).execute(
                """UPDATE procrastinate_jobs SET status = %s
                   WHERE id = (SELECT id FROM procrastinate_jobs
                               WHERE queueing_lock = %s AND status IN ('todo', 'doing')
                               ORDER BY status = 'doing' DESC, id LIMIT 1)""",
                ("succeeded" if status == "ok" else "failed", job_keys(job).queueing_lock))

    def start_running(self, job) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE procrastinate_jobs SET status = 'doing' "
                         "WHERE status = 'todo' AND queueing_lock = %s",
                         (job_keys(job).queueing_lock,))

    def inserted(self) -> list[Job[Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT task_name, args FROM procrastinate_jobs "
                                "ORDER BY id").fetchall()
        return [decode(row["task_name"], row["args"])[0] for row in rows]

    def waiters(self) -> int:
        return len(self.feed._entries)

    def publish_racing_a_finished_run(self, job, outcome):
        def finish_between_since_and_defer():
            self.outcomes.after_get = None
            self.record_outcome(job, outcome)  # its own transaction, committed

        self.outcomes.after_get = finish_between_since_and_defer
        with self.transactions.begin() as tx:
            handle = self.publisher.publish(job, within=tx)
        return handle

    async def insert_periodic_tick(self, job_type) -> None:
        app = build_app(procrastinate.PsycopgConnector(conninfo=self.dsn), (job_type,), None)
        async with app.open_async():
            (tick,) = app.periodic_registry.periodic_tasks.values()
            await PeriodicDeferrer(registry=app.periodic_registry).defer_jobs([(tick, 1000)])

    def caller_write(self, tx) -> None:
        pg_connection(tx).execute("INSERT INTO pb5_probe VALUES (1)")

    def caller_writes(self) -> int:
        with self.db.transaction() as conn:
            return conn.execute("SELECT count(*) AS n FROM pb5_probe").fetchone()["n"]


@pytest.fixture(params=["recording", "procrastinate"])
def h(request):
    if request.param == "recording":
        return RecordingHarness()
    return ProcrastinateHarness(request.getfixturevalue("registry"))  # skips without a DB


def run(h, scenario):
    async def main():
        async with h.running():
            return await scenario()

    return asyncio.run(main())


def publish_committed(h, job, **kwargs):
    with h.transactions.begin() as tx:
        return h.publisher.publish(job, within=tx, **kwargs)


def reference(h, job) -> None:
    with h.transactions.begin() as tx:
        h.assets.reference(tx, asset_key(job), AssetReference(
            owner=job.tag, locator=OriginLocator("https://x.test/t", None, None),
            expected_size=None, expected_sha256=None))


async def until(predicate) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


# -- PB1 ----------------------------------------------------------------------------------------


def test_pb1_unregistered_job_type_and_closed_transaction(h):
    with h.transactions.begin() as tx:
        with pytest.raises(TypeError):
            h.publisher.publish(Job[None](), within=tx)
    with pytest.raises(RuntimeError, match="transaction_not_open"):
        h.publisher.publish(SyncReleases(), within=tx)
    with pytest.raises(TypeError):
        run(h, lambda: h.publisher.publish_now(Job[None]()))
    assert h.inserted() == []


# -- PB4 / PB7: equivalent handles ----------------------------------------------------------------


def test_enqueued_merged_and_joined_handles_are_equivalent(h):
    job = FetchOsImage(tag="v1.0.0")
    reference(h, job)
    enqueued = publish_committed(h, job)
    merged = publish_committed(h, job)
    assert h.inserted() == [job]

    async def scenario():
        waits = [asyncio.create_task(x.wait(timeout=SOON)) for x in (enqueued, merged)]
        await asyncio.sleep(0.05)
        h.start_running(job)
        joined = await h.publisher.publish_now(job)  # the run is under way: joined
        waits.append(asyncio.create_task(joined.wait(timeout=SOON)))
        await asyncio.sleep(0.05)
        await asyncio.to_thread(h.record_outcome, job, Ready(FACTS))
        return await asyncio.gather(*waits)

    assert run(h, scenario) == [Ready(FACTS)] * 3


def test_since_ignores_an_older_outcome(h):
    job = SyncReleases()
    h.record_outcome(job, Ready(None))
    handle = publish_committed(h, job)

    async def scenario():
        assert await handle.wait(timeout=NOW) == Pending()
        await asyncio.to_thread(h.record_outcome, job, Ready(None))
        return await handle.wait(timeout=SOON)

    assert run(h, scenario) == Ready(None)


def test_a_run_finishing_between_the_since_read_and_the_defer_resolves_the_handle(h):
    job = SyncReleases()
    handle = h.publish_racing_a_finished_run(job, Ready(None))
    assert run(h, lambda: handle.wait(timeout=NOW)) == Ready(None)


def test_another_payload_under_the_same_subject_resolves_the_waiter(h):
    handle = publish_committed(h, Pair(a="x", b="1"))

    async def scenario():
        wait = asyncio.create_task(handle.wait(timeout=SOON))
        await until(lambda: h.waiters() == 1)
        await asyncio.to_thread(h.record_outcome, Pair(a="x", b="2"), Ready(None))
        return await wait

    assert run(h, scenario) == Ready(None)


# -- PB6: rollback and the open transaction -------------------------------------------------------


def test_rollback_returns_not_published_and_inserts_nothing(h):
    with pytest.raises(RuntimeError, match="caller"):
        with h.transactions.begin() as tx:
            handle = h.publisher.publish(SyncReleases(), within=tx)
            raise RuntimeError("caller")
    assert run(h, lambda: handle.wait(timeout=SOON)) is NOT_PUBLISHED
    assert h.inserted() == []
    publish_committed(h, SyncReleases())
    assert h.inserted() == [SyncReleases()]


def test_waiting_inside_the_open_transaction_raises(h):
    with h.transactions.begin() as tx:
        handle = h.publisher.publish(SyncReleases(), within=tx)
        with pytest.raises(RuntimeError, match="await_after_commit"):
            run(h, lambda: handle.wait(timeout=NOW))


# -- PB7: timeout and cancellation ----------------------------------------------------------------


def test_timeout_returns_pending_and_does_not_cancel(h):
    job = SyncReleases()
    handle = publish_committed(h, job)

    async def scenario():
        assert await handle.wait(timeout=timedelta(milliseconds=50)) == Pending()
        assert h.waiters() == 0
        assert h.inserted() == [job]
        await asyncio.to_thread(h.record_outcome, job, Ready(None))
        return await handle.wait(timeout=SOON)

    assert run(h, scenario) == Ready(None)


def test_cancel_unregisters_and_the_job_still_completes(h):
    job = FetchOsImage(tag="v2.0.0")
    reference(h, job)
    handle = publish_committed(h, job)

    async def scenario():
        wait = asyncio.create_task(handle.wait(timeout=SOON))
        await until(lambda: h.waiters() == 1)
        wait.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wait
        assert h.waiters() == 0
        assert h.inserted() == [job]
        await asyncio.to_thread(h.record_outcome, job, Ready(FACTS))
        return await handle.wait(timeout=SOON)

    assert run(h, scenario) == Ready(FACTS)


# -- PB2 / PB3: suppression -----------------------------------------------------------------------


def test_a_retry_window_suppresses_publishing(h):
    job = FetchOsImage(tag="v3.0.0")
    reference(h, job)
    handle = publish_committed(h, job)
    h.record_outcome(job, Failed(False, "origin_down", timedelta(seconds=30)))
    assert run(h, lambda: handle.wait(timeout=NOW)) == Failed(
        False, "origin_down", timedelta(seconds=30))
    h.clock.advance(10)
    inside = publish_committed(h, job)
    assert isinstance(inside, SettledHandle)
    assert run(h, lambda: inside.wait(timeout=NOW)) == Failed(
        False, "origin_down", timedelta(seconds=20))
    assert isinstance(publish_committed(h, job, retry_terminal=True), SettledHandle)
    assert h.inserted() == [job]
    h.clock.advance(40)  # past the window: max(0, retry_not_before - now), and a new insert
    assert run(h, lambda: handle.wait(timeout=NOW)) == Failed(False, "origin_down", NOW)
    assert not isinstance(publish_committed(h, job), SettledHandle)
    assert h.inserted() == [job, job]


def test_a_terminal_outcome_suppresses_unless_retry_terminal(h):
    job = FetchOsImage(tag="v4.0.0")
    reference(h, job)
    h.record_outcome(job, Failed(True, "not_found", None))
    suppressed = publish_committed(h, job)
    assert run(h, lambda: suppressed.wait(timeout=NOW)) == Failed(True, "not_found", None)
    assert h.inserted() == []

    async def scenario():
        retried = await h.publisher.publish_now(job, retry_terminal=True)
        assert h.inserted() == [job]
        assert await retried.wait(timeout=NOW) == Pending()  # the old terminal does not count
        await asyncio.to_thread(h.record_outcome, job, Failed(True, "not_found", None))
        return await retried.wait(timeout=SOON)

    assert run(h, scenario) == Failed(True, "not_found", None)


# -- PB9: periodic types --------------------------------------------------------------------------


def test_a_periodic_type_merges_into_its_pending_tick(h):
    async def scenario():
        await h.insert_periodic_tick(SyncReleases)
        first = await asyncio.to_thread(publish_committed, h, SyncReleases())
        second = await h.publisher.publish_now(SyncReleases())
        assert h.inserted() == [SyncReleases()]
        await asyncio.to_thread(h.record_outcome, SyncReleases(), Ready(None))
        return [await first.wait(timeout=SOON), await second.wait(timeout=SOON)]

    assert run(h, scenario) == [Ready(None), Ready(None)]
    publish_committed(h, SyncReleases())
    assert h.inserted() == [SyncReleases(), SyncReleases()]  # the tick ran; a new copy


# -- PB5 / PB8: transactions ----------------------------------------------------------------------


def test_a_merge_inside_within_leaves_the_caller_transaction_working(h):
    with h.transactions.begin() as tx:
        h.publisher.publish(Pair(a="x", b="1"), within=tx)
        h.publisher.publish(Pair(a="x", b="1"), within=tx)  # a merge
        assert tx.state == "open"
        h.caller_write(tx)  # the caller's own write after the merge still succeeds
        h.publisher.publish(Pair(a="x", b="2"), within=tx)  # another payload: its own copy
    assert tx.state == "committed"
    assert h.caller_writes() == 1
    assert h.inserted() == [Pair(a="x", b="1"), Pair(a="x", b="2")]


def test_publish_now_commits_its_own_transaction(h):
    async def scenario():
        handle = await h.publisher.publish_now(SyncReleases())
        return await handle.wait(timeout=NOW)  # no await_after_commit

    assert run(h, scenario) == Pending()
    assert h.inserted() == [SyncReleases()]


# -- adapter-only behaviour (PostgreSQL; CI) --------------------------------------------------------


def test_adapter_refuses_a_registered_type_it_does_not_publish(registry):
    h = ProcrastinateHarness(registry)
    publisher = ProcrastinatePublisher(h.dsn, transactions=h.transactions, outcomes=h.outcomes,
                                       assets=h.assets, clock=h.clock, feed=None,
                                       job_types=(SyncReleases,))
    with h.transactions.begin() as tx:
        with pytest.raises(TypeError):
            publisher.publish(Pair(a="x", b="y"), within=tx)


def test_the_worker_publisher_has_no_feed_to_wait_on(registry):
    h = ProcrastinateHarness(registry)
    publisher = ProcrastinatePublisher(h.dsn, transactions=h.transactions, outcomes=h.outcomes,
                                       assets=h.assets, clock=h.clock, feed=None)
    handle = publish_committed(type("W", (), {"transactions": h.transactions,
                                              "publisher": publisher})(), SyncReleases())
    with pytest.raises(RuntimeError, match="no_outcome_feed"):
        asyncio.run(handle.wait(timeout=NOW))


def test_an_ok_asset_outcome_without_an_asset_record_is_not_ready(registry):
    h = ProcrastinateHarness(registry)
    job = FetchOsImage(tag="v9.0.0")  # never referenced: record_produced is a no-op
    handle = publish_committed(h, job)
    h.record_outcome(job, Ready(FACTS))
    assert run(h, lambda: handle.wait(timeout=NOW)) == Failed(False, "asset_not_recorded", NOW)
