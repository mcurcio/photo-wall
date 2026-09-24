"""Only asset fetches hold procrastinate's one-runner lock (idempotent jobs §6, §11 defect 4).

Every scenario runs procrastinate 3.9.0's own schema and SQL functions on PostgreSQL: the fetch
(`procrastinate_fetch_job_v2`) is what decides whether a lock blocks a pending copy.
"""

from __future__ import annotations

import asyncio

import procrastinate
import psycopg
import pytest
from procrastinate.periodic import PeriodicDeferrer
from runtime_fakes import apply_procrastinate_schema

from central.infra.job_queue import build_app, decode, defer, job_kwargs, task_name
from central.infra.queue_ops import QueueAdmin, RescueStalledJobsHandler
from central.infra.transactions import PgTransactions, pg_connection
from central.kernel.job_types import CATALOG, FetchOsImage, FetchPackage, RescueStalledJobs

FETCH, UPKEEP = "photo-wall-fetch", "photo-wall-upkeep"


class Queue:
    """procrastinate's tables in the test's private schema, driven through its SQL functions."""

    def __init__(self, registry):
        self.dsn = registry.db.dsn
        self.db = registry.db
        apply_procrastinate_schema(self.dsn)
        self.transactions = PgTransactions(self.db)
        self.app = build_app(procrastinate.SyncPsycopgConnector(conninfo=self.dsn), CATALOG, None)

    def sql(self, query, params=()):
        with self.db.transaction() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else None

    def worker(self, *, heartbeat_age="0 seconds"):
        return self.sql("INSERT INTO procrastinate_workers (last_heartbeat) "
                        "VALUES (now() - %s::interval) RETURNING id", (heartbeat_age,))[0]["id"]

    def publish(self, job, attempt=0):
        with self.transactions.begin() as tx:
            assert defer(self.app, job, attempt=attempt, connection=pg_connection(tx))

    def tick(self, job_type, timestamp):
        """Defer one periodic tick exactly as procrastinate's periodic deferrer does."""
        app = build_app(procrastinate.PsycopgConnector(conninfo=self.dsn), CATALOG, None)
        (entry,) = [pt for pt in app.periodic_registry.periodic_tasks.values()
                    if pt.task.name == task_name(job_type)]

        async def run():
            async with app.open_async():
                await PeriodicDeferrer(app.periodic_registry).defer_jobs([(entry, timestamp)])

        asyncio.run(run())

    def fetch(self, queue, worker_id):
        """One worker's fetch; None when nothing is fetchable."""
        row = self.sql("SELECT * FROM procrastinate_fetch_job_v2(%s, %s)",
                       ([queue], worker_id))[0]
        return row if row["id"] is not None else None

    def statuses(self):
        return {r["id"]: r["status"] for r in self.sql(
            "SELECT id, status::text AS status FROM procrastinate_jobs")}

    def pending(self, name):
        return [r["args"] for r in self.sql(
            "SELECT args FROM procrastinate_jobs WHERE status = 'todo' AND task_name = %s",
            (name,))]


def run_rescue(queue, row):
    """Run the fetched rescue row through the production handler and a real `QueueAdmin`."""
    job, attempt = decode(row["task_name"], row["args"])
    assert (job, attempt) == (RescueStalledJobs(), 0)
    admin = QueueAdmin(queue.dsn)

    async def scenario():
        try:
            await RescueStalledJobsHandler(admin).handle(job)
        finally:
            await admin.aclose()

    asyncio.run(scenario())


def test_t3_a_stalled_rescue_does_not_block_the_next_tick_which_rescues_it(registry):
    queue = Queue(registry)
    w1 = queue.worker(heartbeat_age="1 hour")
    w2 = queue.worker()
    image = FetchOsImage(tarball_sha256="5" * 64)
    queue.publish(image)
    stalled_fetch = queue.fetch(FETCH, w1)["id"]
    queue.tick(RescueStalledJobs, 1_700_000_000)
    stalled_rescue = queue.fetch(UPKEEP, w1)["id"]
    queue.tick(RescueStalledJobs, 1_700_000_060)  # the next tick

    tick = queue.fetch(UPKEEP, w2)
    assert tick is not None, "the stalled rescue's lock blocked the next tick"
    assert tick["id"] not in (stalled_fetch, stalled_rescue)
    run_rescue(queue, tick)

    statuses = queue.statuses()
    assert statuses[stalled_fetch] == statuses[stalled_rescue] == "failed"  # closed
    assert statuses[tick["id"]] == "doing"  # W2 is alive: its own run is not rescued
    assert queue.pending(task_name(FetchOsImage)) == [job_kwargs(image, attempt=0)]
    assert queue.pending(task_name(RescueStalledJobs)) == [
        job_kwargs(RescueStalledJobs(), attempt=0)]


def test_p1_retrying_a_doing_row_in_place_violates_the_queueing_lock_so_rescue_republishes(
        registry):
    queue = Queue(registry)
    worker = queue.worker()
    queue.publish(RescueStalledJobs())
    doing = queue.fetch(UPKEEP, worker)["id"]
    queue.publish(RescueStalledJobs())  # the index covers `todo` only: this copy inserts
    with pytest.raises(psycopg.errors.UniqueViolation) as raised:
        queue.sql("SELECT procrastinate_retry_job_v2(%s, now(), NULL, NULL, NULL)", (doing,))
    assert raised.value.diag.constraint_name == "procrastinate_jobs_queueing_lock_idx_v1"


def test_an_asset_fetch_keeps_one_runner(registry):
    queue = Queue(registry)
    worker = queue.worker()
    package = FetchPackage(sha256="ab" * 32)
    queue.publish(package)
    assert queue.fetch(FETCH, worker) is not None  # now `doing`
    queue.publish(package)  # its pending copy
    assert queue.pending(task_name(FetchPackage)) == [job_kwargs(package, attempt=0)]
    assert queue.fetch(FETCH, worker) is None  # the running copy's lock holds it back
