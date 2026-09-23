"""One delivery of a job ends in exactly one committed outcome.

The outcomes and Asset records are the real tables (PostgreSQL, the `registry` fixture); the boot
checks need no database.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest
from content_db import RecordingTransactions
from fakes.transactions import FakeTransactions
from runtime_fakes import (
    FACTS,
    FetchPackageStub,
    PrefetchStub,
    PurgeStub,
    RescueStub,
    SyncReleasesStub,
    catalog_stubs,
)

from central.infra.asset_records import PgAssetRecords
from central.infra.execution import MAX_RETRY_DELAY, JobExecutor, Redelivery
from central.infra.outcomes import JobOutcomes
from central.kernel.assets import AssetKey, AssetKind, AssetReady, AssetReference, OriginLocator
from central.kernel.handling import TerminalFailure, TransientFailure
from central.kernel.job_types import (
    CATALOG,
    FetchOsImage,
    PurgeFinishedJobs,
    RescueStalledJobs,
    SyncReleases,
)
from central.kernel.jobs import Delivery, Job, QueueName, job_keys
from contracts.time import ManualClock

TAG = "v1.0.0"
KEY = AssetKey(AssetKind.OS_IMAGE, TAG)
RETRY = (timedelta(seconds=5), timedelta(minutes=1), timedelta(minutes=5))  # FetchOsImage's


class Scripted:
    """A FetchOsImage handler whose `handle` runs the next scripted step."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = 0

    async def handle(self, job: FetchOsImage) -> AssetReady:
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class Harness:
    def __init__(self, registry, handler, *, reference=True):
        self.clock = ManualClock(1000.0)
        self.events = []
        self.outcomes = JobOutcomes()
        self.assets = PgAssetRecords(self.clock)
        self.transactions = RecordingTransactions(registry.db)
        self.reads = RecordingTransactions(registry.db)  # the test's own reads
        self.redeliveries = []
        self.handler = handler
        if reference:
            with self.reads.begin() as tx:
                self.assets.reference(tx, KEY, AssetReference(
                    owner=TAG, locator=OriginLocator("https://x.test/t", None, None),
                    expected_size=None, expected_sha256=None))

        async def redeliver(redelivery):
            # What a redelivered copy would read: the outcome must already be committed.
            row = await asyncio.to_thread(self.row, redelivery.job)
            self.events.append(("redeliver", redelivery.attempt, row and row.status))
            self.redeliveries.append(redelivery)

        handlers = [h for h in catalog_stubs() if type(h).__name__ != "FetchOsImageStub"]
        self.executor = JobExecutor(
            [handler, *handlers], transactions=self.transactions, outcomes=self.outcomes,
            assets=self.assets, clock=self.clock, redeliver=redeliver)

    def run(self, job=None, attempt=0):
        return asyncio.run(self.executor.execute(job or FetchOsImage(tag=TAG), attempt))

    def row(self, job=None):
        with self.reads.begin() as tx:
            return self.outcomes.get(tx, job_keys(job or FetchOsImage(tag=TAG)).lock)

    def produced(self):
        with self.reads.begin() as tx:
            return self.assets.get(tx, KEY).produced


@pytest.fixture
def harness(registry):
    return lambda handler, **options: Harness(registry, handler, **options)


# -- 2. a returned R ------------------------------------------------------------------------------


def test_ok_writes_the_facts_and_the_outcome_in_one_transaction(harness):
    h = harness(Scripted(FACTS))
    assert h.run() == "ok"
    assert h.produced() == FACTS
    assert (h.row().status, h.row().reason) == ("ok", None)
    assert h.redeliveries == []
    # one transaction read the stored outcome, ONE wrote the facts and the outcome
    assert [tx.state for tx in h.transactions.begun] == ["committed"] * 2


def test_ok_for_a_job_without_an_asset_writes_only_the_outcome(harness):
    h = harness(Scripted())
    assert h.run(SyncReleases()) == "ok"
    assert h.row(SyncReleases()).status == "ok"


def test_facts_conflict_is_a_terminal_bug(caplog, harness):
    h = harness(Scripted(FACTS, AssetReady(size=8, sha256="00" * 32)))
    assert h.run() == "ok"
    with caplog.at_level(logging.ERROR):
        assert h.run() == "terminal"
    assert (h.row().status, h.row().reason) == ("terminal", "facts_conflict")
    assert h.produced() == FACTS  # write-once facts are untouched
    assert "differ from the recorded facts" in caplog.text


# -- 3. TerminalFailure ---------------------------------------------------------------------------


def test_terminal_failure_is_terminal_without_redelivery(harness):
    h = harness(Scripted(TerminalFailure("not_found")))
    assert h.run(attempt=0) == "terminal"
    assert (h.row().status, h.row().reason, h.row().retry_not_before) == (
        "terminal", "not_found", None)
    assert h.redeliveries == []


# -- 4. TransientFailure and unclassified exceptions ----------------------------------------------


@pytest.mark.parametrize("attempt", range(len(RETRY)))
def test_transient_failure_follows_the_backoff(attempt, harness):
    h = harness(Scripted(TransientFailure("origin_down")))
    assert h.run(attempt=attempt) == "transient"
    not_before = 1000.0 + RETRY[attempt].total_seconds()
    assert (h.row().status, h.row().reason, h.row().retry_not_before) == (
        "transient", "origin_down", not_before)
    assert h.redeliveries == [Redelivery(FetchOsImage(tag=TAG), attempt + 1, not_before)]


def test_retry_after_raises_the_delay_and_is_capped(harness):
    h = harness(Scripted(TransientFailure("rate_limited", retry_after=timedelta(minutes=2)),
                         TransientFailure("rate_limited", retry_after=timedelta(hours=5))))
    h.run(attempt=0)
    assert h.redeliveries[-1].not_before == 1000.0 + 120  # max(5s, 2min)
    h.clock.advance(120)
    h.run(attempt=1)
    assert h.redeliveries[-1].not_before == 1120.0 + MAX_RETRY_DELAY.total_seconds()


def test_exhausted_backoff_records_transient_without_redelivery(harness):
    h = harness(Scripted(TransientFailure("origin_down"),
                         TransientFailure("rate_limited", retry_after=timedelta(seconds=30))))
    assert h.run(attempt=len(RETRY)) == "transient"
    assert (h.row().status, h.row().retry_not_before) == ("transient", 1000.0)
    h.clock.advance(10)
    assert h.run(attempt=len(RETRY)) == "transient"
    assert h.row().retry_not_before == 1040.0
    assert h.redeliveries == []


def test_an_unclassified_exception_is_a_logged_transient_bug(caplog, harness):
    h = harness(Scripted(OSError(28, "No space left on device")))
    with caplog.at_level(logging.ERROR):
        assert h.run(attempt=0) == "transient"
    assert (h.row().status, h.row().reason) == ("transient", "unclassified_error")
    assert h.redeliveries[0].attempt == 1
    assert "No space left on device" in caplog.text  # the traceback is logged


def test_an_asset_handler_returning_the_wrong_type_is_a_bug(harness):
    h = harness(Scripted(None))
    assert h.run() == "transient"
    assert h.row().reason == "unclassified_error"


def test_a_job_type_with_no_retry_is_never_redelivered(harness):
    class NoRetry:
        async def handle(self, job: SyncReleases) -> None:
            raise TransientFailure("busy")

    h = harness(Scripted())
    handlers = [NoRetry() if isinstance(x, SyncReleasesStub) else x for x in catalog_stubs()]
    executor = JobExecutor(handlers, transactions=h.transactions, outcomes=h.outcomes,
                           assets=h.assets, clock=h.clock, redeliver=None)
    assert asyncio.run(executor.execute(SyncReleases(), 0)) == "transient"


# -- 5. ordering and propagation ------------------------------------------------------------------


def test_the_outcome_is_committed_before_the_redelivery_is_requested(harness):
    h = harness(Scripted(TransientFailure("origin_down")))
    h.run()
    assert h.events == [("redeliver", 1, "transient")]
    assert all(tx.state == "committed" for tx in h.transactions.begun)


def test_cancellation_propagates_without_outcome_or_redelivery(harness):
    h = harness(Scripted(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        h.run()
    assert h.row() is None and h.redeliveries == []


# -- 1. an early copy inside a retry window -------------------------------------------------------


def test_an_early_copy_is_redelivered_for_the_window_end_without_running(harness):
    handler = Scripted(TransientFailure("origin_down"), FACTS)
    h = harness(handler)
    h.run(attempt=0)  # window ends at 1005
    written = h.row()
    h.clock.advance(2)
    assert h.run(attempt=0) is None
    assert handler.calls == 1
    assert h.redeliveries[-1] == Redelivery(FetchOsImage(tag=TAG), 0, 1005.0)
    assert h.row() == written  # no outcome written
    h.clock.advance(2.5)  # within the 1s tolerance of the window's end: it runs
    assert h.run(attempt=1) == "ok"
    assert handler.calls == 2


# -- 6. boot checks -------------------------------------------------------------------------------


def build(handlers, catalog=CATALOG):
    # Boot checks run in the constructor, before any repository is used.
    return JobExecutor(handlers, transactions=FakeTransactions(), outcomes=JobOutcomes(),
                       assets=PgAssetRecords(ManualClock(0.0)), clock=ManualClock(0.0),
                       redeliver=None, catalog=catalog)


def test_boot_accepts_exactly_one_handler_per_catalog_type():
    executor = build(catalog_stubs())
    assert executor.queues == frozenset({QueueName.FETCH, QueueName.UPKEEP})


def test_boot_refuses_a_handler_without_exact_hints():
    class Unhinted:
        async def handle(self, job):
            return None

    with pytest.raises(TypeError):
        build([*catalog_stubs(), Unhinted()])


def test_boot_refuses_two_handlers_for_one_type():
    with pytest.raises(ValueError, match="two handlers"):
        build([*catalog_stubs(), FetchPackageStub()])


def test_boot_refuses_a_catalog_type_with_no_handler():
    handlers = [h for h in catalog_stubs() if not isinstance(h, PrefetchStub)]
    with pytest.raises(ValueError, match="no handler"):
        build(handlers)


class Outside(Job[None], name="test.execution_outside", delivery=Delivery(queue=QueueName.UPKEEP)):
    pass


class OutsideStub:
    async def handle(self, job: Outside) -> None:
        return None


def test_boot_refuses_a_handler_for_a_type_outside_the_catalog():
    with pytest.raises(ValueError, match="outside the catalog"):
        build([*catalog_stubs(), OutsideStub()])


def test_a_custom_catalog_bounds_the_queues():
    executor = build([RescueStub(), PurgeStub()], catalog=(RescueStalledJobs, PurgeFinishedJobs))
    assert executor.queues == frozenset({QueueName.UPKEEP})
