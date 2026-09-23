"""A2 (the kernel-job <-> procrastinate mapping) and the `job_outcomes` repository."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import SimpleNamespace

import croniter
import procrastinate
import psycopg
import pytest
from fakes.transactions import FakeTransaction
from hypothesis import given
from hypothesis import strategies as st
from procrastinate.exceptions import AlreadyEnqueued, TaskNotFound

from central.infra.job_queue import (
    ATTEMPT_KWARG,
    TASK_PREFIX,
    build_app,
    decode,
    defer,
    defer_async,
    job_kwargs,
    periodic_cron,
    task_name,
)
from central.infra.outcomes import JobOutcomes
from central.infra.transactions import PgTransactions
from central.kernel.job_types import (
    CATALOG,
    FetchOsImage,
    FetchPackage,
    PurgeFinishedJobs,
    SyncReleases,
)
from central.kernel.jobs import PERIODIC_CADENCES, Delivery, Job, QueueName, job_keys

SHA = "ab" * 32


class Colour(StrEnum):
    RED = "red"
    BLUE = "blue"


class Mixed(Job[None], name="test.job_queue_mixed", subject=("s",),
            delivery=Delivery(queue=QueueName.UPKEEP, priority=7)):
    s: str
    i: int
    b: bool
    c: Colour


def catalog_instances() -> list[Job]:
    samples = {FetchOsImage: FetchOsImage(tag="v1.2.3-rc.1"), FetchPackage: FetchPackage(sha256=SHA)}
    return [samples.get(job_type) or job_type() for job_type in CATALOG]


# -- task names, kwargs and decode ------------------------------------------------------------


@pytest.mark.parametrize("attempt", [0, 3])
def test_decode_inverts_job_kwargs_for_every_catalog_type(attempt):
    for job in catalog_instances():
        name = task_name(type(job))
        assert name == TASK_PREFIX + type(job).job_name
        assert decode(name, job_kwargs(job, attempt=attempt)) == (job, attempt)


@given(st.builds(Mixed, s=st.text(), i=st.integers(), b=st.booleans(),
                 c=st.sampled_from(Colour)),
       st.integers(min_value=0, max_value=1000))
def test_decode_inverts_job_kwargs_for_generated_jobs(job, attempt):
    kwargs = job_kwargs(job, attempt=attempt)
    assert kwargs[ATTEMPT_KWARG] == attempt
    assert decode(task_name(Mixed), kwargs) == (job, attempt)


def test_decode_drops_the_periodic_timestamp_and_defaults_the_attempt():
    assert decode(task_name(SyncReleases), {"timestamp": 1_700_000_000}) == (SyncReleases(), 0)


def test_decode_refuses_tasks_that_are_not_ours():
    with pytest.raises(KeyError):
        decode("photo_wall.media.prepare", {"job_id": "x"})
    with pytest.raises(KeyError):
        decode("os_image.fetch", {"tag": "v1.0.0"})  # no prefix


def test_attempt_must_be_a_non_negative_int():
    for bad in (-1, True, "1", 1.0):
        with pytest.raises(ValueError, match="invalid_attempt"):
            job_kwargs(SyncReleases(), attempt=bad)
        with pytest.raises(ValueError, match="invalid_attempt"):
            decode(task_name(SyncReleases), {ATTEMPT_KWARG: bad})


def test_job_kwargs_refuses_an_unregistered_job():
    with pytest.raises(TypeError):
        job_kwargs(Job[None](), attempt=0)


# -- periodic cron -------------------------------------------------------------------------------


def test_periodic_cron_is_seconds_last():
    assert periodic_cron(timedelta(minutes=5)) == "*/5 * * * * 0"
    assert periodic_cron(timedelta(hours=1)) == "0 */1 * * * 0"


@pytest.mark.parametrize("every", sorted(PERIODIC_CADENCES))
def test_periodic_cron_fires_at_exactly_every_cadence(every):
    start = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    iterator = croniter.croniter(periodic_cron(every), start)
    fires = [iterator.get_next(float) for _ in range(4)]
    assert all(b - a == every.total_seconds() for a, b in zip(fires, fires[1:], strict=False))
    assert all(fire % 60 == 0 for fire in fires)  # pinned to second 0


def test_periodic_cron_refuses_other_cadences():
    for bad in (timedelta(minutes=7), timedelta(seconds=30), timedelta(days=2)):
        with pytest.raises(ValueError):
            periodic_cron(bad)


# -- build_app -----------------------------------------------------------------------------------


def connector() -> procrastinate.BaseConnector:
    return procrastinate.SyncPsycopgConnector(conninfo="postgresql://unused.invalid/none")


def our_tasks(app: procrastinate.App) -> dict[str, object]:
    return {name: task for name, task in app.tasks.items() if name.startswith(TASK_PREFIX)}


def test_build_app_registers_one_task_per_type_with_its_delivery():
    types = (*CATALOG, Mixed)
    app = build_app(connector(), types, None)
    tasks = our_tasks(app)
    assert set(tasks) == {task_name(t) for t in types}
    for job_type in types:
        task = tasks[task_name(job_type)]
        assert task.retry_strategy is None
        assert task.pass_context is True
        assert task.queue == job_type.delivery.queue.value
        assert task.priority == job_type.delivery.priority
        assert task.lock is None and task.queueing_lock is None  # always per job, from job_keys


def test_build_app_registers_periodic_types_with_their_constant_locks():
    app = build_app(connector(), CATALOG, None)
    periodic = {pt.task.name: pt for pt in app.periodic_registry.periodic_tasks.values()}
    expected = [t for t in CATALOG if t.delivery.every is not None]
    assert set(periodic) == {task_name(t) for t in expected}
    for job_type in expected:
        entry = periodic[task_name(job_type)]
        keys = job_keys(job_type())
        assert entry.cron == periodic_cron(job_type.delivery.every)
        assert entry.periodic_id == job_type.job_name
        assert entry.configure_kwargs["lock"] == keys.lock
        assert entry.configure_kwargs["queueing_lock"] == keys.queueing_lock


def test_build_app_refuses_duplicates():
    with pytest.raises(ValueError):
        build_app(connector(), (SyncReleases, SyncReleases), None)


def test_task_decodes_and_runs_the_body():
    seen = []

    async def body(job, attempt):
        seen.append((job, attempt))

    app = build_app(connector(), (FetchOsImage, SyncReleases), body)
    job = FetchOsImage(tag="v2.0.0")

    async def run():
        for name, kwargs in ((task_name(FetchOsImage), job_kwargs(job, attempt=2)),
                             (task_name(SyncReleases), {"timestamp": 5})):
            context = SimpleNamespace(job=SimpleNamespace(task_name=name))
            await app.tasks[name](context, **kwargs)

    asyncio.run(run())
    assert seen == [(job, 2), (SyncReleases(), 0)]


def test_publish_only_task_refuses_to_run():
    app = build_app(connector(), (SyncReleases,), None)
    name = task_name(SyncReleases)
    context = SimpleNamespace(job=SimpleNamespace(task_name=name))
    with pytest.raises(RuntimeError, match="publish-only"):
        asyncio.run(app.tasks[name](context, timestamp=1))


# -- defer ---------------------------------------------------------------------------------------


class RecordingDeferrer:
    def __init__(self, app, raises):
        self.app = app
        self.raises = raises

    def defer(self, **kwargs):
        self.app.deferred.append(kwargs)
        if self.raises:
            raise AlreadyEnqueued()
        return 1

    async def defer_async(self, **kwargs):
        return self.defer(**kwargs)


class RecordingApp:
    def __init__(self, raises=False):
        self.raises = raises
        self.configured = []
        self.deferred = []

    def configure_task(self, name, **kwargs):
        self.configured.append({"name": name, **kwargs})
        return RecordingDeferrer(self, self.raises)


class SavepointConnection:
    def __init__(self):
        self.savepoints = []

    @contextmanager
    def transaction(self):
        try:
            yield
        except BaseException:
            self.savepoints.append("rolled_back")
            raise
        self.savepoints.append("released")


def test_defer_passes_both_locks_inside_a_savepoint():
    app, conn = RecordingApp(), SavepointConnection()
    job = Mixed(s="x", i=1, b=True, c=Colour.RED)
    at = datetime(2026, 1, 1, tzinfo=UTC)
    assert defer(app, job, attempt=1, connection=conn, schedule_at=at) is True
    keys = job_keys(job)
    assert app.configured == [{
        "name": task_name(Mixed), "allow_unknown": False, "lock": keys.lock,
        "queueing_lock": keys.queueing_lock, "connection": conn, "schedule_at": at,
    }]
    assert app.deferred == [job_kwargs(job, attempt=1)]
    assert conn.savepoints == ["released"]


def test_defer_reports_already_enqueued_as_merged_and_rolls_back_only_the_savepoint():
    app, conn = RecordingApp(raises=True), SavepointConnection()
    assert defer(app, SyncReleases(), attempt=0, connection=conn) is False
    assert conn.savepoints == ["rolled_back"]


def test_defer_async_uses_the_app_pool_and_reports_merges():
    app = RecordingApp()
    assert asyncio.run(defer_async(app, SyncReleases(), attempt=2)) is True
    assert app.configured[0]["connection"] is None
    assert app.deferred == [job_kwargs(SyncReleases(), attempt=2)]
    assert asyncio.run(defer_async(RecordingApp(raises=True), SyncReleases(), attempt=0)) is False


def test_defer_refuses_a_type_the_app_was_not_built_with():
    app = build_app(connector(), (SyncReleases,), None)
    with pytest.raises(TaskNotFound):  # never the procrastinate "default" queue
        defer(app, PurgeFinishedJobs(), attempt=0, connection=SavepointConnection())


# -- job_outcomes: validation (no DB) ------------------------------------------------------------


@pytest.mark.parametrize("status,reason,retry_not_before", [
    ("done", None, None),
    ("ok", "why", None),
    ("transient", None, 1.0),
    ("terminal", "Bad Reason", None),
    ("terminal", "gone", 5.0),
    ("ok", None, 5.0),
    ("transient", "busy", float("nan")),
])
def test_record_refuses_rows_the_table_would_refuse(status, reason, retry_not_before):
    with pytest.raises(ValueError):
        JobOutcomes().record(FakeTransaction(), SyncReleases(), status=status, reason=reason,
                             retry_not_before=retry_not_before, now=1.0)


def test_repository_refuses_a_fake_transaction():
    with pytest.raises(TypeError):
        JobOutcomes().get(FakeTransaction(), "k")


# -- job_outcomes: PostgreSQL (CI) ---------------------------------------------------------------


def test_outcomes_upsert_sequence_failing_since_and_purge(registry):
    transactions, outcomes = PgTransactions(registry.db), JobOutcomes()
    job, other = FetchOsImage(tag="v1.0.0"), SyncReleases()
    lock = job_keys(job).lock
    with transactions.begin() as tx:
        assert outcomes.get(tx, lock) is None
        first = outcomes.record(tx, job, status="transient", reason="origin_down",
                                retry_not_before=110.0, now=100.0)
    assert (first.status, first.failing_since, first.retry_not_before) == ("transient", 100.0, 110.0)
    with transactions.begin() as tx:
        second = outcomes.record(tx, job, status="terminal", reason="not_found",
                                 retry_not_before=None, now=200.0)
        third = outcomes.record(tx, other, status="ok", reason=None, retry_not_before=None,
                                now=50.0)
    assert second.seq > first.seq and third.seq > second.seq
    assert (second.failing_since, second.retry_not_before) == (100.0, None)  # kept while failing
    with transactions.begin() as tx:
        ok = outcomes.record(tx, job, status="ok", reason=None, retry_not_before=None, now=300.0)
        assert ok.failing_since is None and ok.reason is None
        assert outcomes.get(tx, lock) == ok
        assert outcomes.get_many(tx, [lock, job_keys(other).lock, "absent"]) == {
            lock: ok, job_keys(other).lock: third}
        assert outcomes.get_many(tx, []) == {}
        assert outcomes.purge(tx, older_than=100.0) == 1  # `other`, last written at 50
        assert outcomes.get(tx, job_keys(other).lock) is None
        assert outcomes.get(tx, lock) == ok


def test_outcome_notify_is_sent_only_on_commit(registry):
    transactions, outcomes = PgTransactions(registry.db), JobOutcomes()
    job = FetchPackage(sha256=SHA)
    with psycopg.connect(registry.db.dsn, autocommit=True) as listener:
        listener.execute("LISTEN job_outcome")
        with pytest.raises(RuntimeError):
            with transactions.begin() as tx:
                outcomes.record(tx, job, status="ok", reason=None, retry_not_before=None, now=1.0)
                raise RuntimeError("rollback")
        assert list(listener.notifies(timeout=0.3)) == []
        with transactions.begin() as tx:
            outcomes.record(tx, job, status="ok", reason=None, retry_not_before=None, now=1.0)
        payloads = [n.payload for n in listener.notifies(timeout=2, stop_after=1)]
    assert payloads == [job_keys(job).lock]
