"""PB1-PB9 against `RecordingPublisher`: the fake half of Lane A's publisher conformance suite."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fakes.asset_records import InMemoryAssetRecords
from fakes.publisher import PublishedCall, RecordingPublisher
from fakes.transactions import FakeTransactions

from central.kernel.assets import AssetKey, AssetKind, AssetReady, AssetReference, OriginLocator
from central.kernel.job_types import FetchOsImage, SyncReleases
from central.kernel.jobs import Delivery, Job, QueueName
from central.kernel.publishing import NOT_PUBLISHED, Failed, Pending, Ready, SettledHandle
from contracts.time import ManualClock

SHA = "cd" * 32
FACTS = AssetReady(size=42, sha256=SHA)
NOW = timedelta(0)
SOON = timedelta(seconds=2)


class Pair(Job[None], name="test.publisher_pair", subject=("a",),
           delivery=Delivery(queue=QueueName.UPKEEP)):
    a: str
    b: str


def make() -> tuple[RecordingPublisher, FakeTransactions, ManualClock]:
    clock = ManualClock(1000.0)
    return RecordingPublisher(clock), FakeTransactions(), clock


def publish_committed(publisher, transactions, job, **kwargs):
    with transactions.begin() as tx:
        handle = publisher.publish(job, within=tx, **kwargs)
    return handle


def test_pb1_unregistered_job_type_and_closed_transaction():
    publisher, transactions, _ = make()
    with transactions.begin() as tx:
        with pytest.raises(TypeError):
            publisher.publish(Job[None](), within=tx)
    with pytest.raises(RuntimeError, match="transaction_not_open"):
        publisher.publish(SyncReleases(), within=tx)
    with pytest.raises(TypeError):
        asyncio.run(publisher.publish_now(Job[None]()))
    assert publisher.inserted == [] and publisher.calls == []


def test_enqueued_merged_and_joined_handles_are_equivalent():
    publisher, transactions, _ = make()
    job = FetchOsImage(tag="v1.0.0")
    enqueued = publish_committed(publisher, transactions, job)
    merged = publish_committed(publisher, transactions, job)
    assert publisher.inserted == [job]

    async def scenario():
        waits = [asyncio.create_task(h.wait(timeout=SOON)) for h in (enqueued, merged)]
        await asyncio.sleep(0)
        joined = await publisher.publish_now(job)  # the run is under way: joined, not duplicated
        waits.append(asyncio.create_task(joined.wait(timeout=SOON)))
        await asyncio.sleep(0)
        publisher.record_outcome(job, Ready(FACTS))
        return await asyncio.gather(*waits)

    assert asyncio.run(scenario()) == [Ready(FACTS)] * 3
    assert publisher.inserted == [job]
    assert [c.within is None for c in publisher.calls] == [False, False, True]


def test_since_ignores_an_older_outcome():
    publisher, transactions, _ = make()
    job = SyncReleases()
    publisher.record_outcome(job, Ready(None))
    handle = publish_committed(publisher, transactions, job)
    assert asyncio.run(handle.wait(timeout=NOW)) == Pending()
    publisher.record_outcome(job, Ready(None))
    assert asyncio.run(handle.wait(timeout=NOW)) == Ready(None)


def test_an_outcome_from_another_thread_wakes_the_waiter():
    publisher, transactions, _ = make()
    job = Pair(a="x", b="1")
    handle = publish_committed(publisher, transactions, job)

    async def scenario():
        wait = asyncio.create_task(handle.wait(timeout=timedelta(seconds=5)))
        await asyncio.sleep(0)
        await asyncio.to_thread(publisher.record_outcome, Pair(a="x", b="2"), Ready(None))
        return await wait  # a run of another payload under the same subject resolves it

    assert asyncio.run(scenario()) == Ready(None)


def test_rollback_returns_not_published_and_inserts_nothing():
    publisher, transactions, _ = make()
    with pytest.raises(RuntimeError, match="caller"):
        with transactions.begin() as tx:
            handle = publisher.publish(SyncReleases(), within=tx)
            raise RuntimeError("caller")
    assert asyncio.run(handle.wait(timeout=SOON)) is NOT_PUBLISHED
    assert publisher.inserted == []
    publish_committed(publisher, transactions, SyncReleases())
    assert publisher.inserted == [SyncReleases()]


def test_waiting_inside_the_open_transaction_raises():
    publisher, transactions, _ = make()
    with transactions.begin() as tx:
        handle = publisher.publish(SyncReleases(), within=tx)
        with pytest.raises(RuntimeError, match="await_after_commit"):
            asyncio.run(handle.wait(timeout=NOW))


def test_timeout_returns_pending_and_does_not_cancel():
    publisher, transactions, _ = make()
    handle = publish_committed(publisher, transactions, SyncReleases())
    assert asyncio.run(handle.wait(timeout=timedelta(milliseconds=10))) == Pending()
    assert publisher._waiters == {}
    assert publisher.inserted == [SyncReleases()]
    publisher.record_outcome(SyncReleases(), Ready(None))
    assert asyncio.run(handle.wait(timeout=NOW)) == Ready(None)


def test_cancel_unregisters_and_the_job_still_completes():
    publisher, transactions, _ = make()
    job = FetchOsImage(tag="v2.0.0")
    handle = publish_committed(publisher, transactions, job)

    async def scenario():
        wait = asyncio.create_task(handle.wait(timeout=timedelta(seconds=5)))
        await asyncio.sleep(0)
        assert len(publisher._waiters) == 1
        wait.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wait
        assert publisher._waiters == {}

    asyncio.run(scenario())
    assert publisher.inserted == [job]
    publisher.record_outcome(job, Ready(FACTS))
    assert asyncio.run(handle.wait(timeout=NOW)) == Ready(FACTS)


def test_transient_outcome_and_its_retry_window():
    publisher, transactions, clock = make()
    job = FetchOsImage(tag="v3.0.0")
    handle = publish_committed(publisher, transactions, job)
    publisher.record_outcome(job, Failed(False, "origin_down", timedelta(seconds=30)))
    assert asyncio.run(handle.wait(timeout=NOW)) == Failed(False, "origin_down",
                                                          timedelta(seconds=30))
    clock.advance(10)
    inside = publish_committed(publisher, transactions, job)  # PB2: inserts nothing
    assert isinstance(inside, SettledHandle)
    assert asyncio.run(inside.wait(timeout=NOW)) == Failed(False, "origin_down",
                                                          timedelta(seconds=20))
    inside_forced = publish_committed(publisher, transactions, job, retry_terminal=True)
    assert isinstance(inside_forced, SettledHandle)
    assert publisher.inserted == [job]
    clock.advance(40)  # past the window: max(0, retry_not_before - now) and a new insert
    assert asyncio.run(handle.wait(timeout=NOW)) == Failed(False, "origin_down", timedelta(0))
    after = publish_committed(publisher, transactions, job)
    assert not isinstance(after, SettledHandle)
    assert publisher.inserted == [job, job]
    assert len(publisher.calls) == 4  # suppressed calls are recorded too


def test_explicit_retry_not_before_overrides_retry_after():
    publisher, transactions, _ = make()
    job = SyncReleases()
    publisher.record_outcome(job, Failed(False, "busy", timedelta(0)), retry_not_before=1100.0)
    handle = publish_committed(publisher, transactions, job)
    assert asyncio.run(handle.wait(timeout=NOW)) == Failed(False, "busy", timedelta(seconds=100))


def test_terminal_outcome_suppresses_unless_retry_terminal():
    publisher, transactions, _ = make()
    job = FetchOsImage(tag="v4.0.0")
    publisher.record_outcome(job, Failed(True, "not_found", None))
    suppressed = publish_committed(publisher, transactions, job)
    assert asyncio.run(suppressed.wait(timeout=NOW)) == Failed(True, "not_found", None)
    assert publisher.inserted == []
    retried = asyncio.run(publisher.publish_now(job, retry_terminal=True))
    assert publisher.inserted == [job]
    assert asyncio.run(retried.wait(timeout=NOW)) == Pending()  # the old terminal does not count
    publisher.record_outcome(job, Failed(True, "not_found", None))
    assert asyncio.run(retried.wait(timeout=NOW)) == Failed(True, "not_found", None)
    assert publisher.calls[-1] == PublishedCall(job, True, None)


def test_periodic_type_merges_into_its_pending_tick():
    publisher, transactions, _ = make()
    first = publish_committed(publisher, transactions, SyncReleases())
    second = asyncio.run(publisher.publish_now(SyncReleases()))
    assert publisher.inserted == [SyncReleases()]
    publisher.record_outcome(SyncReleases(), Ready(None))
    assert asyncio.run(first.wait(timeout=NOW)) == asyncio.run(second.wait(timeout=NOW))
    publish_committed(publisher, transactions, SyncReleases())
    assert publisher.inserted == [SyncReleases(), SyncReleases()]  # the tick ran; a new copy


def test_merge_inside_within_leaves_the_caller_transaction_open():
    publisher, transactions, _ = make()
    with transactions.begin() as tx:
        publisher.publish(Pair(a="x", b="1"), within=tx)
        publisher.publish(Pair(a="x", b="1"), within=tx)  # a merge
        assert tx.state == "open"
        publisher.publish(Pair(a="x", b="2"), within=tx)  # another payload: its own pending copy
    assert tx.state == "committed"
    assert publisher.inserted == [Pair(a="x", b="1"), Pair(a="x", b="2")]


def test_publish_now_commits_its_own_transaction():
    publisher, _, _ = make()
    handle = asyncio.run(publisher.publish_now(SyncReleases()))
    assert asyncio.run(handle.wait(timeout=NOW)) == Pending()  # no await_after_commit
    assert publisher.calls == [PublishedCall(SyncReleases(), False, None)]


def test_asset_job_ready_is_read_from_the_asset_record():
    clock = ManualClock(1000.0)
    assets = InMemoryAssetRecords()
    publisher, transactions = RecordingPublisher(clock, assets), FakeTransactions()
    job = FetchOsImage(tag="v5.0.0")
    key = AssetKey(AssetKind.OS_IMAGE, "v5.0.0")
    with transactions.begin() as tx:
        assets.reference(tx, key, AssetReference(
            owner="v5.0.0", locator=OriginLocator("https://x.test/t", None, None),
            expected_size=None, expected_sha256=None))
        handle = publisher.publish(job, within=tx)
    publisher.record_outcome(job, Ready(FACTS))
    assert asyncio.run(handle.wait(timeout=NOW)) == Ready(FACTS)
    with transactions.begin() as tx:
        assert assets.get(tx, key).produced == FACTS
