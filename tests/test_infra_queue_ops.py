"""Queue operations — stalled-job rescue and purging (unit, then PostgreSQL in CI)."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import procrastinate
import pytest
from fakes.transactions import FakeTransactions
from procrastinate.jobs import Job as ProcrastinateJob
from procrastinate.jobs import Status
from runtime_fakes import apply_procrastinate_schema, catalog_stubs

from central.infra import queue_ops
from central.infra.asset_records import PgAssetRecords
from central.infra.execution import JobExecutor
from central.infra.job_queue import build_app, defer, job_kwargs, task_name
from central.infra.outcomes import JobOutcomes
from central.infra.queue_ops import (
    FinishedJobs,
    PurgeFinishedJobsHandler,
    QueueAdmin,
    RescueStalledJobsHandler,
    StalledJob,
)
from central.infra.transactions import PgTransactions, pg_connection
from central.kernel.handling import TransientFailure, handler_job_type
from central.kernel.job_types import (
    FetchOsImage,
    PurgeFinishedJobs,
    RescueStalledJobs,
    SyncReleases,
)
from contracts.time import ManualClock

DAY = 86400.0
DSN = "postgresql://unused.invalid/none"


class RecordingAdmin:
    def __init__(self, stalled=()):
        self._stalled = list(stalled)
        self.calls = []

    async def stalled(self, *, heartbeat_timeout):
        self.calls.append(("stalled", heartbeat_timeout))
        return self._stalled

    async def republish(self, stalled):
        self.calls.append(("republish", stalled.id))

    async def close(self, stalled):
        self.calls.append(("close", stalled.id))


# -- handlers (no DB) -------------------------------------------------------------------------------


def test_rescue_republishes_each_stalled_job_then_closes_it():
    stalled = [StalledJob(7, FetchOsImage(tarball_sha256="1" * 64), 1), StalledJob(9, SyncReleases(), 0)]
    admin = RecordingAdmin(stalled)
    asyncio.run(RescueStalledJobsHandler(admin).handle(RescueStalledJobs()))
    assert admin.calls == [("stalled", timedelta(seconds=30)), ("republish", 7), ("close", 7),
                           ("republish", 9), ("close", 9)]


def test_one_row_failing_to_close_does_not_abort_the_rescue_of_the_others():
    # e.g. procrastinate's "not in doing" when the row's worker came back and finished it.
    class Refusing(RecordingAdmin):
        async def close(self, stalled):
            await super().close(stalled)
            if stalled.id == 7:
                raise procrastinate.exceptions.ConnectorException("not in doing")

    stalled = [StalledJob(7, FetchOsImage(tarball_sha256="1" * 64), 1), StalledJob(9, SyncReleases(), 0)]
    admin = Refusing(stalled)
    with pytest.raises(TransientFailure) as raised:
        asyncio.run(RescueStalledJobsHandler(admin).handle(RescueStalledJobs()))
    assert raised.value.reason == queue_ops.RESCUE_INCOMPLETE  # the next run looks again
    assert admin.calls[1:] == [("republish", 7), ("close", 7), ("republish", 9), ("close", 9)]


def test_the_handlers_dispatch_by_their_hints_and_fill_the_catalog():
    admin = RecordingAdmin()
    rescue = RescueStalledJobsHandler(admin)
    purge = PurgeFinishedJobsHandler(FinishedJobs(FakeTransactions().begin), transactions=FakeTransactions(),
                                     outcomes=JobOutcomes(), clock=ManualClock(0.0))
    assert handler_job_type(rescue) is RescueStalledJobs
    assert handler_job_type(purge) is PurgeFinishedJobs
    others = [handler for handler in catalog_stubs()
              if handler_job_type(handler) not in (RescueStalledJobs, PurgeFinishedJobs)]
    JobExecutor([*others, rescue, purge], transactions=FakeTransactions(), outcomes=JobOutcomes(),
                assets=PgAssetRecords(ManualClock(0.0)), clock=ManualClock(0.0), redeliver=None)


# -- QueueAdmin against a faked procrastinate job manager (no DB) ----------------------------------


class FakeJobManager:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    async def get_stalled_jobs(self, **kwargs):
        self.calls.append(("get_stalled_jobs", kwargs))
        return self.rows

    async def finish_job_by_id_async(self, **kwargs):
        self.calls.append(("finish", kwargs))


def admin_with(monkeypatch, manager):
    admin = QueueAdmin(DSN)

    async def opened():
        return SimpleNamespace(job_manager=manager)

    monkeypatch.setattr(admin, "_opened", opened)
    return admin


def row(id_, name, kwargs, queue="photo-wall-fetch"):
    return ProcrastinateJob(id=id_, queue=queue, lock=None, queueing_lock=None, task_name=name,
                            task_kwargs=kwargs, status="doing")


def test_stalled_returns_only_our_jobs_with_their_attempt(monkeypatch):
    ours = FetchOsImage(tarball_sha256="3" * 64)
    manager = FakeJobManager([
        row(1, "photo_wall.media.prepare", {"job_id": "x"}, queue="photo-wall-media"),
        row(2, task_name(FetchOsImage), job_kwargs(ours, attempt=2)),
        row(3, "builtin:procrastinate.builtin_tasks.remove_old_jobs", {}),
        row(4, task_name(SyncReleases), {"timestamp": 60}, queue="photo-wall-fetch"),
        row(5, "photo_wall.test_not_in_catalog", {}),
    ])
    admin = admin_with(monkeypatch, manager)
    found = asyncio.run(admin.stalled(heartbeat_timeout=timedelta(seconds=45)))
    assert found == [StalledJob(2, ours, 2), StalledJob(4, SyncReleases(), 0)]
    assert manager.calls == [("get_stalled_jobs", {"seconds_since_heartbeat": 45.0})]


def test_close_finishes_the_row_failed_and_republish_keeps_the_attempt(monkeypatch):
    manager = FakeJobManager()
    admin = admin_with(monkeypatch, manager)
    deferred = []

    async def defer_async(app, job, *, attempt, schedule_at=None):
        deferred.append((job, attempt, schedule_at))
        return True

    monkeypatch.setattr(queue_ops, "defer_async", defer_async)
    stalled = StalledJob(11, FetchOsImage(tarball_sha256="1" * 64), 2)
    asyncio.run(admin.republish(stalled))
    asyncio.run(admin.close(stalled))
    assert deferred == [(stalled.job, 2, None)]
    assert manager.calls == [("finish", {"job_id": 11, "status": Status.FAILED,
                                         "delete_job": False})]


# -- PostgreSQL (CI) --------------------------------------------------------------------------------


class Db:
    def __init__(self, registry):
        self.dsn = registry.db.dsn
        self.db = registry.db
        apply_procrastinate_schema(self.dsn)
        self.transactions = PgTransactions(self.db)
        self.publisher_app = build_app(procrastinate.SyncPsycopgConnector(conninfo=self.dsn),
                                       (FetchOsImage, SyncReleases), None)

    def sql(self, query, params=()):
        with self.db.transaction() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else None

    def publish(self, job, attempt=0):
        with self.transactions.begin() as tx:
            assert defer(self.publisher_app, job, attempt=attempt, connection=pg_connection(tx))

    def legacy(self, status="doing", queue="photo-wall-media"):
        return self.sql("INSERT INTO procrastinate_jobs (queue_name, task_name, lock, args, "
                        "status) VALUES (%s, 'photo_wall.media.prepare', 'photo-wall-media-"
                        "storage', '{\"job_id\": \"j\"}', %s) RETURNING id",
                        (queue, status))[0]["id"]

    def fetch(self, queue):
        return self.sql("SELECT id, args FROM procrastinate_fetch_job_v2(%s, NULL)",
                        ([queue],))[0]


def test_a_stalled_job_is_republished_then_closed_and_legacy_jobs_are_untouched(registry):
    db = Db(registry)
    job = FetchOsImage(tarball_sha256="5" * 64)
    db.publish(job, attempt=1)
    dead = db.sql("INSERT INTO procrastinate_workers (last_heartbeat) "
                  "VALUES (now() - interval '5 minutes') RETURNING id")[0]["id"]
    live = db.sql("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id")[0]["id"]
    db.sql("UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s", (dead,))
    legacy = db.legacy()
    db.sql("UPDATE procrastinate_jobs SET worker_id = %s WHERE id = %s", (dead, legacy))
    db.publish(SyncReleases())
    db.sql("UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s "
           "WHERE task_name = %s", (live, task_name(SyncReleases)))  # alive: not stalled
    (stalled_id,) = [r["id"] for r in db.sql(
        "SELECT id FROM procrastinate_jobs WHERE task_name = %s", (task_name(FetchOsImage),))]
    observed = []

    class Observing(QueueAdmin):
        async def republish(self, stalled):
            await super().republish(stalled)
            observed.append(("after_republish", db.fetch("photo-wall-fetch")["id"]))

        async def close(self, stalled):
            await super().close(stalled)
            observed.append(("after_close", db.fetch("photo-wall-fetch")))

    admin = Observing(db.dsn)

    async def scenario():
        try:
            await RescueStalledJobsHandler(admin).handle(RescueStalledJobs())
        finally:
            await admin.aclose()

    asyncio.run(scenario())
    assert observed[0] == ("after_republish", None)  # the new copy waits for the lock
    assert observed[1][0] == "after_close"
    new_copy = observed[1][1]
    assert new_copy["id"] != stalled_id and new_copy["args"] == job_kwargs(job, attempt=1)
    statuses = {r["id"]: r["status"] for r in db.sql(
        "SELECT id, status::text AS status FROM procrastinate_jobs")}
    assert statuses[stalled_id] == "failed"
    assert statuses[legacy] == "doing"  # never touched
    assert statuses[new_copy["id"]] == "doing"  # fetched by the probe above


def test_purge_deletes_old_finished_rows_of_every_queue_and_stale_outcomes_only(registry):
    """The hourly purge deletes every queue's finished jobs past `keep_jobs` (a legacy media job
    included: decision 0033), keeps young and unfinished ones, and stale outcomes only."""
    db = Db(registry)
    v1, v2, v3 = "1" * 64, "2" * 64, "3" * 64  # three OS images' tarball shas
    for tarball in (v1, v2, v3):
        db.publish(FetchOsImage(tarball_sha256=tarball))
    db.sql("UPDATE procrastinate_jobs SET status = 'doing'")
    rows = {r["tarball"]: r["id"] for r in db.sql(
        "SELECT id, args->>'tarball_sha256' AS tarball FROM procrastinate_jobs")}
    for tarball, status in ((v1, "failed"), (v2, "succeeded")):
        db.sql("SELECT procrastinate_finish_job_v1(%s, %s, false)", (rows[tarball], status))
    legacy = db.legacy(status="succeeded", queue="photo-wall-media")
    db.sql("INSERT INTO procrastinate_events (job_id, type) VALUES (%s, 'succeeded')", (legacy,))
    db.sql("UPDATE procrastinate_events SET at = now() - interval '8 days' "
           "WHERE job_id IN (%s, %s)", (rows[v1], legacy))

    outcomes, clock = JobOutcomes(), ManualClock(100 * DAY)
    with db.transactions.begin() as tx:
        for tarball, now in ((v1, 60 * DAY), (v2, 80 * DAY)):
            outcomes.record(tx, FetchOsImage(tarball_sha256=tarball), status="ok", reason=None,
                            retry_not_before=None, now=now)
    asyncio.run(PurgeFinishedJobsHandler(FinishedJobs(registry.db.transaction),
                                         transactions=db.transactions, outcomes=outcomes,
                                         clock=clock).handle(PurgeFinishedJobs()))
    left = {r["id"] for r in db.sql("SELECT id FROM procrastinate_jobs")}
    assert left == {rows[v2], rows[v3]}  # young, still doing
    assert [r["lock_key"] for r in db.sql("SELECT lock_key FROM job_outcomes")] == [
        f'os_image.fetch["{v2}"]']


def test_finished_jobs_go_in_bounded_batches_with_their_events(registry):
    """12 old finished media-queue jobs (the queue the old purge never reached), 2 batches of 5:
    the 10 oldest go with their events, each batch its own transaction; young, unfinished and
    the 2 left over stay, and the next run takes those 2. Mutation probe: put the queue filter
    back (`AND j.queue_name <> 'photo-wall-media'`: none go)."""
    db = Db(registry)
    old = [db.legacy(status=status, queue="photo-wall-media")
           for status in ("succeeded", "failed", "cancelled", "aborted") * 3]
    young = db.legacy(status="succeeded", queue="photo-wall-media")
    waiting = db.legacy(status="todo", queue="photo-wall-media")
    running = db.legacy(status="doing", queue="photo-wall-media")
    for job in (*old, young, waiting, running):
        db.sql("INSERT INTO procrastinate_events (job_id, type) VALUES (%s, 'succeeded')", (job,))
    db.sql("UPDATE procrastinate_events SET at = now() - interval '8 days' WHERE job_id <> %s",
           (young,))
    transactions = []

    @contextmanager
    def begin():
        with registry.db.transaction() as conn:
            transactions.append(conn)
            yield conn

    finished = FinishedJobs(begin, batch_rows=5, batches=2)
    assert finished.delete(older_than=timedelta(days=7)) == 10
    assert len(transactions) == 2
    left = {r["id"] for r in db.sql("SELECT id FROM procrastinate_jobs")}
    assert left == {*old[10:], young, waiting, running}
    assert {r["job_id"] for r in db.sql("SELECT DISTINCT job_id FROM procrastinate_events")} == left
    assert finished.delete(older_than=timedelta(days=7)) == 2  # a short last batch, hourly
    assert {r["id"] for r in db.sql("SELECT id FROM procrastinate_jobs")} == {young, waiting,
                                                                               running}
    with pytest.raises(ValueError):
        FinishedJobs(begin, batch_rows=0)
