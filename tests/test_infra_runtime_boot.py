"""A3 (runtime boot and loops, no DB) and A7 (the worker-side retry effect, PostgreSQL in CI)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import procrastinate
import pytest
from fakes.asset_records import InMemoryAssetRecords
from fakes.transactions import FakeTransactions
from runtime_fakes import FetchPackageStub, PrefetchStub, apply_procrastinate_schema, catalog_stubs

from central.infra import runtime as runtime_module
from central.infra.execution import Redelivery
from central.infra.job_queue import build_app, defer, task_name
from central.infra.outcomes import JobOutcomes
from central.infra.runtime import JobRuntime, RecordedFailure
from central.infra.transactions import PgTransactions, pg_connection
from central.kernel.handling import TransientFailure
from central.kernel.job_types import SyncReleases
from central.kernel.jobs import Delivery, Job, QueueName, job_keys
from contracts.time import ManualClock, SystemClock

DSN = "postgresql://unused.invalid/none"
BOTH = {QueueName.FETCH: 2, QueueName.UPKEEP: 1}


def make(handlers=None, concurrency=None, **kwargs):
    return JobRuntime(DSN, catalog_stubs() if handlers is None else handlers,
                      BOTH if concurrency is None else concurrency,
                      transactions=FakeTransactions(), assets=InMemoryAssetRecords(),
                      clock=ManualClock(0.0), **kwargs)


# -- boot checks (no DB) ---------------------------------------------------------------------------


def test_boot_accepts_the_full_catalog():
    make()


def test_boot_refuses_a_missing_handler():
    with pytest.raises(ValueError, match="no handler"):
        make([h for h in catalog_stubs() if not isinstance(h, PrefetchStub)])


def test_boot_refuses_a_duplicate_handler():
    with pytest.raises(ValueError, match="two handlers"):
        make([*catalog_stubs(), FetchPackageStub()])


@pytest.mark.parametrize("concurrency", [
    {QueueName.FETCH: 1},
    {**BOTH, "photo-wall-media": 1},
    {QueueName.FETCH: 1, QueueName.UPKEEP: 0},
    {QueueName.FETCH: 1, QueueName.UPKEEP: True},
])
def test_boot_refuses_a_concurrency_map_that_does_not_match(concurrency):
    with pytest.raises(ValueError, match="concurrency"):
        make(concurrency=concurrency)


# -- loops (no DB: procrastinate's worker entry point is faked) -----------------------------------


class Opened:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        self.log.append("open")

    async def __aexit__(self, *exc):
        self.log.append("close")


def fake_app(runtime, monkeypatch):
    log, started = [], []

    async def run_worker_async(**kwargs):
        started.append(kwargs)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            log.append(("stopped", tuple(kwargs["queues"])))
            raise

    monkeypatch.setattr(runtime._app, "run_worker_async", run_worker_async)
    monkeypatch.setattr(runtime._app, "open_async", lambda: Opened(log))
    return log, started


def test_run_starts_one_loop_per_queue_and_stop_is_graceful_and_idempotent(monkeypatch):
    runtime = make()
    log, started = fake_app(runtime, monkeypatch)

    async def scenario():
        running = asyncio.create_task(runtime.run())
        while len(started) < 2:
            await asyncio.sleep(0)
        runtime.stop()
        runtime.stop()
        await asyncio.wait_for(running, 1)

    asyncio.run(scenario())
    assert sorted((tuple(s["queues"]), s["concurrency"]) for s in started) == [
        (("photo-wall-fetch",), 2), (("photo-wall-upkeep",), 1)]
    for kwargs in started:
        assert kwargs["install_signal_handlers"] is False
        assert kwargs["update_heartbeat_interval"] == 10
        assert kwargs["stalled_worker_timeout"] == 30
    assert log[0] == "open" and log[-1] == "close"
    assert sorted(entry for entry in log if entry not in ("open", "close")) == [
        ("stopped", ("photo-wall-fetch",)), ("stopped", ("photo-wall-upkeep",))]
    runtime.stop()  # after run returned: still a no-op


def test_stop_before_run_means_run_returns_at_once(monkeypatch):
    runtime = make()
    _, started = fake_app(runtime, monkeypatch)
    runtime.stop()
    asyncio.run(asyncio.wait_for(runtime.run(), 1))
    assert started == []


def test_stop_from_another_thread(monkeypatch):
    runtime = make()
    _, started = fake_app(runtime, monkeypatch)

    async def scenario():
        running = asyncio.create_task(runtime.run())
        while len(started) < 2:
            await asyncio.sleep(0)
        await asyncio.to_thread(runtime.stop)
        await asyncio.wait_for(running, 1)

    asyncio.run(scenario())


def test_body_raises_recorded_failure_only_for_failed_outcomes(monkeypatch):
    runtime = make()
    results = iter(["ok", None, "transient", "terminal"])

    async def execute(job, attempt):
        return next(results)

    monkeypatch.setattr(runtime._executor, "execute", execute)

    async def scenario():
        await runtime._body(SyncReleases(), 0)
        await runtime._body(SyncReleases(), 0)
        for status in ("transient", "terminal"):
            with pytest.raises(RecordedFailure, match=status):
                await runtime._body(SyncReleases(), 0)

    asyncio.run(scenario())


def test_redelivery_defers_the_next_attempt_at_the_window_end(monkeypatch):
    runtime = make()
    calls = []

    async def defer_async(app, job, *, attempt, schedule_at=None):
        calls.append((app, job, attempt, schedule_at))
        return True

    monkeypatch.setattr(runtime_module, "defer_async", defer_async)
    asyncio.run(runtime._redeliver(Redelivery(SyncReleases(), 2, 1_800_000_000.5)))
    assert calls == [(runtime._app, SyncReleases(), 2,
                      datetime.fromtimestamp(1_800_000_000.5, UTC))]


def test_the_task_body_is_wired_to_the_executor(monkeypatch):
    runtime = make()
    seen = []

    async def execute(job, attempt):
        seen.append((job, attempt))
        return "ok"

    monkeypatch.setattr(runtime._executor, "execute", execute)
    name = task_name(SyncReleases)

    class Context:
        class job:
            task_name = name

    asyncio.run(runtime._app.tasks[name](Context(), timestamp=1))
    assert seen == [(SyncReleases(), 0)]


# -- A7: the worker-side retry effect (PostgreSQL; CI) ---------------------------------------------


class Flaky(Job[None], name="test.runtime_flaky",
            delivery=Delivery(queue=QueueName.UPKEEP,
                              retry=(timedelta(milliseconds=1), timedelta(milliseconds=1)))):
    pass


class FlakyHandler:
    def __init__(self):
        self.calls = 0

    async def handle(self, job: Flaky) -> None:
        self.calls += 1
        if self.calls <= 2:
            raise TransientFailure("flaky")


def test_transient_failures_retry_by_republishing_never_in_place(registry):
    dsn = registry.db.dsn
    apply_procrastinate_schema(dsn)
    transactions, handler = PgTransactions(registry.db), FlakyHandler()
    runtime = JobRuntime(dsn, [handler], {QueueName.UPKEEP: 1}, transactions=transactions,
                         assets=InMemoryAssetRecords(), clock=SystemClock(), catalog=(Flaky,))
    publisher_app = build_app(procrastinate.SyncPsycopgConnector(conninfo=dsn), (Flaky,), None)
    with transactions.begin() as tx:
        assert defer(publisher_app, Flaky(), attempt=0, connection=pg_connection(tx))

    def snapshot():
        with registry.db.transaction() as conn:
            todo = conn.execute("SELECT count(*) AS n FROM procrastinate_jobs "
                                "WHERE status = 'todo'").fetchone()["n"]
        with transactions.begin() as tx:
            return todo, JobOutcomes().get(tx, job_keys(Flaky()).lock)

    async def scenario():
        running = asyncio.create_task(runtime.run())
        max_todo, deadline = 0, time.monotonic() + 30
        try:
            while time.monotonic() < deadline:
                todo, outcome = await asyncio.to_thread(snapshot)
                max_todo = max(max_todo, todo)
                if outcome is not None and outcome.status == "ok":
                    return max_todo, outcome
                await asyncio.sleep(0.02)
            raise AssertionError("the job never succeeded")
        finally:
            runtime.stop()
            await asyncio.wait_for(running, 30)

    max_todo, outcome = asyncio.run(scenario())
    assert max_todo <= 1
    assert handler.calls == 3
    assert outcome.failing_since is None
    with registry.db.transaction() as conn:
        rows = conn.execute("SELECT status::text AS status, attempts, args FROM procrastinate_jobs "
                            "ORDER BY id").fetchall()
        in_place = conn.execute("SELECT count(*) AS n FROM procrastinate_events "
                                "WHERE type = 'deferred_for_retry'").fetchone()["n"]
    assert [(r["status"], r["attempts"], r["args"]["_attempt"]) for r in rows] == [
        ("failed", 1, 0), ("failed", 1, 1), ("succeeded", 1, 2)]
    assert in_place == 0  # procrastinate's in-place retry_job was never used
