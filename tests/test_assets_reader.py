"""`AssetReader.read` and `WaiterSlots`, with `RecordingPublisher` settling via `record_outcome`.

The Asset records are the real table (PostgreSQL, the `registry` fixture); `WaiterSlots` needs none.
The substitute-serve tests (issue #24) publish through `ProcrastinatePublisher` onto the real queue.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
from datetime import timedelta

import pytest
from content_db import Reads, RecordingTransactions, put_file
from fakes.publisher import RecordingPublisher
from runtime_fakes import apply_procrastinate_schema

from central.assets.layout import CacheLayout
from central.assets.reader import AssetReader, Opened, SlotsFull, Unavailable, WaiterSlots
from central.assets.store import CacheStore
from central.infra.asset_records import PgAssetRecords
from central.infra.job_queue import decode
from central.infra.outcomes import JobOutcomes
from central.infra.publisher import ProcrastinatePublisher
from central.kernel.assets import AssetReady, AssetReference, OriginLocator
from central.kernel.job_types import FetchOsImage
from central.kernel.jobs import asset_key
from central.kernel.ports import Candidates
from central.kernel.publishing import Failed, Ready
from contracts.time import ManualClock

DATA = b"a squashfs " * 100
FACTS = AssetReady(size=len(DATA), sha256=hashlib.sha256(DATA).hexdigest())
NEW = FetchOsImage(tag="v2.0.0")
OLD = FetchOsImage(tag="v1.0.0")
LOCATOR = OriginLocator("https://example.test/base.tar.gz", sha256=None, size=None)


def fd_closed(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


class World:
    def __init__(self, registry, tmp_path, *, capacity: int = 4, wait: float = 5.0,
                 records: PgAssetRecords | None = None,
                 store: CacheStore | None = None,
                 queue: type[ProcrastinatePublisher] | None = None) -> None:
        self.db = registry.db
        self.clock = ManualClock(1000.0)
        self.store = store or CacheStore(CacheLayout(tmp_path))
        self.records = records or PgAssetRecords(self.clock)
        self.transactions = RecordingTransactions(registry.db)
        self.reads = Reads(RecordingTransactions(registry.db))
        if queue is None:
            self.publisher = RecordingPublisher(self.clock, assets=self.records,
                                                transactions=self.reads.transactions)
        else:  # the real queue; nothing here waits on a handle, so no OutcomeFeed
            apply_procrastinate_schema(registry.db.dsn)
            self.publisher = queue(registry.db.dsn, transactions=self.reads.transactions,
                                   outcomes=JobOutcomes(), assets=self.records,
                                   clock=self.clock, feed=None)
        self.slots = WaiterSlots(capacity)
        self.reader = AssetReader(store=self.store, records=self.records,
                                  transactions=self.transactions, publisher=self.publisher,
                                  slots=self.slots, clock=self.clock,
                                  wait_timeout=timedelta(seconds=wait))

    def record(self, job, produced: AssetReady | None = None) -> None:
        key = asset_key(job)
        with self.reads.transactions.begin() as tx:
            self.records.reference(tx, key, AssetReference(job.tag, LOCATOR, None, None))
            if produced is not None:
                self.records.record_produced(tx, key, produced)

    def put(self, job, data: bytes = DATA) -> None:
        put_file(self.store, asset_key(job), data)

    def pending(self) -> list:
        """The queue's `todo` jobs, decoded (real queue only)."""
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT task_name, args FROM procrastinate_jobs "
                                "WHERE status = 'todo' ORDER BY id").fetchall()
        return [decode(row["task_name"], row["args"])[0] for row in rows]


@pytest.fixture
def world_at(registry, tmp_path):
    """`World(...)` over this test's schema and cache directory."""
    return lambda **options: World(registry, tmp_path, **options)


def served(result) -> bytes:
    assert isinstance(result, Opened), result
    try:
        return os.read(result.fd, result.size + 1)
    finally:
        os.close(result.fd)


async def until(predicate) -> None:
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition never held")


# -- fast path (flow b) ---------------------------------------------------------------------------


def test_first_present_candidate_is_opened_in_one_read(world_at):
    world = world_at()
    world.record(NEW)  # recorded but never produced
    world.record(OLD, FACTS)
    world.put(OLD)

    async def run():
        result = await world.reader.read(Candidates((NEW, OLD), pinned=False))
        await until(lambda: world.publisher.calls)  # the substitute's background publish
        return result

    result = asyncio.run(run())
    assert isinstance(result, Opened)
    assert (result.job, result.size, result.sha256) == (OLD, FACTS.size, FACTS.sha256)
    assert served(result) == DATA
    assert world.transactions.begun[0].state == "committed"
    [call] = world.publisher.calls  # the wanted candidate, as a request (decision 3)
    assert (call.job, call.retry_terminal, call.within) == (NEW, True, None)


def test_the_wanted_candidate_on_disk_publishes_nothing(world_at):
    world = world_at()
    world.record(NEW, FACTS)
    world.put(NEW)
    world.record(OLD, FACTS)
    world.put(OLD)

    async def run():
        result = await world.reader.read(Candidates((NEW, OLD), pinned=False))
        assert asyncio.all_tasks() == {asyncio.current_task()}  # nothing left running
        return result

    result = asyncio.run(run())
    assert result.job == NEW
    assert served(result) == DATA
    assert world.publisher.calls == []


def test_a_candidate_whose_file_is_absent_or_wrong_is_skipped(world_at):
    world = world_at()
    world.record(NEW, FACTS)
    world.put(NEW, DATA[:-1])  # wrong size: never served
    world.record(OLD, FACTS)
    world.put(OLD)
    result = asyncio.run(world.reader.read(Candidates((NEW, OLD), pinned=False)))
    assert result.job == OLD
    served(result)


# -- miss: publish and wait (flow c) --------------------------------------------------------------


def test_miss_publishes_the_first_candidate_with_retry_terminal_and_serves_when_ready(world_at):
    world = world_at()
    world.record(NEW)
    world.record(OLD)

    async def run():
        task = asyncio.create_task(world.reader.read(Candidates((NEW, OLD), pinned=False)))
        await until(lambda: world.publisher.calls)
        assert world.slots.in_use == 1
        world.put(NEW)
        world.publisher.record_outcome(NEW, Ready(FACTS))
        return await task

    result = asyncio.run(run())
    assert result.job == NEW and result.sha256 == FACTS.sha256
    assert served(result) == DATA
    [call] = world.publisher.calls
    assert (call.job, call.retry_terminal) == (NEW, True)
    assert world.slots.in_use == 0


def test_ready_but_absent_file_is_unavailable(world_at):
    world = world_at()
    world.record(NEW)

    async def run():
        task = asyncio.create_task(world.reader.read(Candidates((NEW,), pinned=True)))
        await until(lambda: world.publisher.calls)
        world.publisher.record_outcome(NEW, Ready(FACTS))  # e.g. wiped right after
        return await task

    assert asyncio.run(run()) == Unavailable("absent_after_ready", 1)


@pytest.mark.parametrize(("outcome", "expected"), [
    (Failed(False, "http_503", timedelta(seconds=2.3)), Unavailable("http_503", 3)),
    (Failed(False, "http_503", timedelta(0)), Unavailable("http_503", 1)),
    (Failed(True, "base_digest_mismatch", None), Unavailable("base_digest_mismatch", 30)),
])
def test_failed_outcomes_map_to_unavailable(world_at, outcome, expected):
    world = world_at()
    world.record(NEW)

    async def run():
        task = asyncio.create_task(world.reader.read(Candidates((NEW,), pinned=True)))
        await until(lambda: world.publisher.calls)
        world.publisher.record_outcome(NEW, outcome)
        return await task

    assert asyncio.run(run()) == expected
    assert world.slots.in_use == 0


def test_a_terminal_outcome_is_retried_by_a_request(world_at):
    world = world_at(wait=0.01)
    world.record(NEW)
    world.publisher.record_outcome(NEW, Failed(True, "base_digest_mismatch", None))
    asyncio.run(world.reader.read(Candidates((NEW,), pinned=True)))
    assert world.publisher.inserted == [NEW]  # retry_terminal re-publishes past PB3


def test_timeout_is_unavailable_and_does_not_cancel_the_job(world_at):
    world = world_at(wait=0.01)
    world.record(NEW)
    assert asyncio.run(world.reader.read(Candidates((NEW,), pinned=True))) == \
        Unavailable("timeout", 5)
    assert world.publisher.inserted == [NEW]
    assert world.slots.in_use == 0


def test_full_slots_are_busy_and_publish_nothing(world_at):
    world = world_at(capacity=1)
    world.record(NEW)
    with world.slots.claim():
        result = asyncio.run(world.reader.read(Candidates((NEW,), pinned=True)))
    assert result == Unavailable("busy", 5)
    assert world.publisher.calls == []


def test_a_pinned_candidate_never_yields_another_job(world_at):
    world = world_at(wait=0.01)
    world.record(OLD, FACTS)
    world.put(OLD)
    world.record(NEW)
    result = asyncio.run(world.reader.read(Candidates((NEW,), pinned=True)))
    assert result == Unavailable("timeout", 5)
    assert [call.job for call in world.publisher.calls] == [NEW]


# -- cancellation (the route's disconnect watcher) ------------------------------------------------


def test_cancellation_while_waiting_releases_the_slot_and_keeps_the_job(world_at):
    world = world_at()
    world.record(NEW)

    async def run():
        task = asyncio.create_task(world.reader.read(Candidates((NEW,), pinned=True)))
        await until(lambda: world.publisher.calls)
        assert world.slots.in_use == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert world.slots.in_use == 0
    assert world.publisher.inserted == [NEW]  # the job is still queued


class BlockingStore(CacheStore):
    """`open` blocks in its thread until released; records every fd it returns."""

    def __init__(self, layout) -> None:
        super().__init__(layout)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fds: list[int] = []

    def open(self, key, facts):
        self.entered.set()
        assert self.release.wait(5)
        opened = super().open(key, facts)
        if opened is not None:
            self.fds.append(opened.fd)
        return opened


def test_cancellation_during_the_open_closes_the_fd_it_produced(world_at, tmp_path):
    store = BlockingStore(CacheLayout(tmp_path))
    world = world_at(store=store)
    world.record(NEW, FACTS)
    world.put(NEW)

    async def run():
        task = asyncio.create_task(world.reader.read(Candidates((NEW,), pinned=True)))
        await asyncio.to_thread(store.entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        store.release.set()
        await until(lambda: store.fds and fd_closed(store.fds[0]))

    asyncio.run(run())
    assert len(store.fds) == 1


# -- last_served_at -------------------------------------------------------------------------------


def test_touch_is_throttled_per_key(world_at):
    world = world_at()
    world.record(NEW, FACTS)
    world.put(NEW)
    candidates = Candidates((NEW,), pinned=True)
    key = asset_key(NEW)
    served(asyncio.run(world.reader.read(candidates)))
    assert world.reads.asset(key).last_served_at == 1000.0
    world.clock.advance(60)
    served(asyncio.run(world.reader.read(candidates)))
    assert world.reads.asset(key).last_served_at == 1000.0  # within 5 minutes
    world.clock.advance(300)
    served(asyncio.run(world.reader.read(candidates)))
    assert world.reads.asset(key).last_served_at == 1360.0


class FailingTouch(PgAssetRecords):
    def touch_served(self, tx, key, at):
        raise RuntimeError("db down")


def test_a_touch_failure_is_logged_and_never_fails_the_serve(world_at, caplog):
    world = world_at(records=FailingTouch(ManualClock(1000.0)))
    world.record(NEW, FACTS)
    world.put(NEW)
    with caplog.at_level(logging.WARNING, logger="central.assets.reader"):
        result = asyncio.run(world.reader.read(Candidates((NEW,), pinned=True)))
    assert served(result) == DATA
    assert "touch failed" in caplog.text


# -- a substitute serve publishes the wanted fetch (issue #24), on the real queue ----------------


async def eventually(predicate, seconds: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition never held"
        await asyncio.sleep(0.01)


def test_a_substitute_serve_queues_exactly_one_fetch_for_the_wanted_key(world_at):
    world = world_at(queue=ProcrastinatePublisher)
    world.record(NEW)  # wanted, never produced
    world.record(OLD, FACTS)
    world.put(OLD)
    candidates = Candidates((NEW, OLD), pinned=False)

    async def run():
        first = await world.reader.read(candidates)
        await eventually(lambda: world.pending())
        second = await world.reader.read(candidates)  # merges into the pending copy
        await eventually(lambda: not asyncio.all_tasks() - {asyncio.current_task()})
        return first, second

    first, second = asyncio.run(run())
    assert served(first) == DATA and served(second) == DATA
    assert (first.job, second.job) == (OLD, OLD)
    assert world.pending() == [NEW]


def test_a_wanted_serve_queues_nothing(world_at):
    world = world_at(queue=ProcrastinatePublisher)
    world.record(NEW, FACTS)
    world.put(NEW)
    world.record(OLD, FACTS)
    world.put(OLD)

    async def run():
        result = await world.reader.read(Candidates((NEW, OLD), pinned=False))
        assert asyncio.all_tasks() == {asyncio.current_task()}
        return result

    assert served(asyncio.run(run())) == DATA
    assert world.pending() == []


class HeldPublisher(ProcrastinatePublisher):
    """`publish_now` waits for `release` first: a serve that awaited it would never return."""

    release: asyncio.Event

    async def publish_now(self, job, *, retry_terminal=False):
        await self.release.wait()
        return await super().publish_now(job, retry_terminal=retry_terminal)


def test_a_substitute_serve_never_waits_on_its_publish(world_at):
    world = world_at(queue=HeldPublisher)
    world.record(NEW)
    world.record(OLD, FACTS)
    world.put(OLD)

    async def run():
        world.publisher.release = asyncio.Event()
        result = await asyncio.wait_for(
            world.reader.read(Candidates((NEW, OLD), pinned=False)), timeout=5)
        assert world.pending() == []  # served while the publish is still held
        world.publisher.release.set()
        await eventually(lambda: world.pending())
        return result

    result = asyncio.run(run())
    assert result.job == OLD and served(result) == DATA
    assert world.pending() == [NEW]


class RaisingPublisher(ProcrastinatePublisher):
    async def publish_now(self, job, *, retry_terminal=False):
        raise RuntimeError("queue down")


def test_a_publish_failure_is_logged_and_never_fails_the_substitute_serve(world_at, caplog):
    world = world_at(queue=RaisingPublisher)
    world.record(NEW)
    world.record(OLD, FACTS)
    world.put(OLD)

    async def run():
        result = await world.reader.read(Candidates((NEW, OLD), pinned=False))
        await eventually(lambda: "failed after serving a substitute" in caplog.text)
        return result

    with caplog.at_level(logging.WARNING, logger="central.assets.reader"):
        result = asyncio.run(run())
    assert result.job == OLD and served(result) == DATA
    assert "queue down" in caplog.text
    assert world.pending() == []


# -- WaiterSlots ----------------------------------------------------------------------------------


def test_waiter_slots_bound_and_release():
    slots = WaiterSlots(2)
    with slots.claim():
        with slots.claim():
            assert slots.in_use == 2
            with pytest.raises(SlotsFull), slots.claim():
                pass
            assert slots.in_use == 2
        with pytest.raises(RuntimeError), slots.claim():
            raise RuntimeError("released on error")
        assert slots.in_use == 1
    assert slots.in_use == 0
    with pytest.raises(ValueError):
        WaiterSlots(0)
