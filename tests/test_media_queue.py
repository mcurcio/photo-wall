import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import UTC, datetime, timezone
from types import SimpleNamespace

import pytest

from central.db import DatabaseTransactionClock
from central.infra.photo_library_store import PgPhotoLibrary
from central.infra.queue_ops import QueueRescue
from central.media_queue import (
    CHECK_CONNECTION_TASK,
    CHECK_POOL_TASK,
    KEY_CONTENT_QUEUE_PREFIX,
    LIBRARY_CONNECTION_LOCK_PREFIX,
    LIBRARY_POOL_LOCK_PREFIX,
    MEASURE_KEY_CONTENT_TASK,
    MEDIA_QUEUE,
    MEDIA_STORAGE_LOCK,
    ProcrastinateMediaQueue,
)
from central.photo_library.model import SourceQuery
from central.photo_library.policy import CHECK_EVERY_SECONDS
from central.photo_library.ports import CheckComplete
from central.photo_library.views import ConnectionWrite
from media.library_checks import LibraryChecks
from media.task_queue import (
    CheckRetryStrategy,
    MediaRetryStrategy,
    RetryableMediaTask,
    create_worker_app,
)

DUE = datetime(2026, 11, 17, 12, 0, tzinfo=UTC).timestamp()


class Deferrer:
    def __init__(self, configured):
        self.configured = configured

    def defer(self, **kwargs):
        self.configured["payload"] = kwargs
        return 41


class App:
    def __init__(self):
        self.configured = None

    def configure_task(self, name, **kwargs):
        self.configured = {"name": name, **kwargs}
        return Deferrer(self.configured)


@pytest.mark.parametrize("enqueue,task,prefix,argument", [
    ("enqueue_connection_check_in", CHECK_CONNECTION_TASK, LIBRARY_CONNECTION_LOCK_PREFIX,
     "connection_ref"),
    ("enqueue_pool_check_in", CHECK_POOL_TASK, LIBRARY_POOL_LOCK_PREFIX, "pool_key"),
])
def test_library_checks_are_locked_per_connection_or_pool_and_scheduled_for_their_due_time(
        enqueue, task, prefix, argument):
    """Roadmap 4 (C1, C2): one check of a connection, one step of a pool, at a time (the lock),
    one waiting (the queueing lock); S3a: deferred for its due time (`scheduled_at`)."""
    queue = object.__new__(ProcrastinateMediaQueue)
    queue.app = App()
    connection = SimpleNamespace(transaction=nullcontext)

    receipt = getattr(queue, enqueue)(connection, "library-0123456789ab", due_at=DUE)

    assert not receipt.coalesced
    assert queue.app.configured == {
        "name": task,
        "queue": MEDIA_QUEUE,
        "lock": prefix + "library-0123456789ab",
        "queueing_lock": prefix + "library-0123456789ab",
        "schedule_at": datetime.fromtimestamp(DUE, UTC),
        "connection": connection,
        "payload": {argument: "library-0123456789ab"},
    }


def test_a_measurement_is_queued_once_per_photo_and_takes_no_lock():
    """Roadmap 5a (M1): measurements run beside preparation and the checks; one waits per
    photo."""
    queue = object.__new__(ProcrastinateMediaQueue)
    queue.app = App()
    connection = SimpleNamespace(transaction=nullcontext)

    assert not queue.enqueue_measure_in(connection, "asset-1").coalesced
    assert queue.app.configured == {
        "name": MEASURE_KEY_CONTENT_TASK,
        "queue": MEDIA_QUEUE,
        "lock": None,
        "queueing_lock": KEY_CONTENT_QUEUE_PREFIX + "asset-1",
        "schedule_at": None,
        "connection": connection,
        "payload": {"asset_id": "asset-1"},
    }


def _todo(registry):
    with registry.db.transaction() as conn:
        return [(job["task_name"], job["args"], job["scheduled_at"].timestamp()) for job in
                conn.execute("SELECT task_name,args,scheduled_at FROM procrastinate_jobs "
                             "WHERE status='todo' ORDER BY id").fetchall()]


def test_a_queued_check_absorbs_a_later_one_and_an_earlier_one_replaces_it(registry):
    """S3a: a re-arm finding any check queued no later coalesces; a defer due earlier ("Check
    now", a change) replaces a check queued for later, so it is never held back to the due time.
    Mutation probe: let the immediate defer coalesce (the job stays 15 minutes on)."""
    ProcrastinateMediaQueue.apply_schema(registry.db.dsn)
    queue = ProcrastinateMediaQueue(registry.db.dsn)
    ref, later = "library-0123456789ab", DUE + CHECK_EVERY_SECONDS
    with registry.db.transaction() as conn:
        first = queue.enqueue_connection_check_in(conn, ref, due_at=later)
        rearm = queue.enqueue_connection_check_in(conn, ref, due_at=later + 60)
        other = queue.enqueue_pool_check_in(conn, "a" * 64, due_at=later)
    assert (first.coalesced, rearm.coalesced, other.coalesced) == (False, True, False)
    assert _todo(registry) == [(CHECK_CONNECTION_TASK, {"connection_ref": ref}, later),
                               (CHECK_POOL_TASK, {"pool_key": "a" * 64}, later)]
    with registry.db.transaction() as conn:   # Check now
        now = queue.enqueue_pool_check_in(conn, "a" * 64, due_at=DUE)
        again = queue.enqueue_pool_check_in(conn, "a" * 64, due_at=DUE)
    assert (now.coalesced, again.coalesced) == (False, True)
    assert _todo(registry) == [(CHECK_CONNECTION_TASK, {"connection_ref": ref}, later),
                               (CHECK_POOL_TASK, {"pool_key": "a" * 64}, DUE)]
    with registry.db.transaction() as conn:   # the replaced row is deleted, not kept cancelled
        assert conn.execute("SELECT count(*) AS n FROM procrastinate_jobs").fetchone()["n"] == 2


def _library(registry):
    ProcrastinateMediaQueue.apply_schema(registry.db.dsn)
    library = PgPhotoLibrary(registry.db, times=DatabaseTransactionClock(),
                             queue=ProcrastinateMediaQueue(registry.db.dsn))
    ref = library.add_connection(ConnectionWrite(name="Home", url="http://immich.invalid",
                                                 key="PUBLIC-FIXTURE-KEY-1234"), by="console")
    draft = library.create_draft(SourceQuery(connection_ref=ref), None, by="console")
    with registry.db.transaction() as conn:
        key = library.pool_of_draft_in(conn, draft)
    return library, ref, key


def _pool_jobs(registry, key):
    with registry.db.transaction() as conn:
        return [(row["status"], row["scheduled_at"].timestamp()) for row in conn.execute(
            "SELECT status::text AS status,scheduled_at FROM procrastinate_jobs "
            "WHERE task_name=%s AND args->>'pool_key'=%s ORDER BY id",
            (CHECK_POOL_TASK, key)).fetchall()]


def _run_first(registry, key):
    """The pool's queued step, taken by a worker (`doing`), as Procrastinate fetches it."""
    with registry.db.transaction() as conn:
        conn.execute("UPDATE procrastinate_jobs SET status='doing' WHERE status='todo' AND "
                     "task_name=%s AND args->>'pool_key'=%s", (CHECK_POOL_TASK, key))


def _db_now(registry):
    with registry.db.transaction() as conn:
        return DatabaseTransactionClock().now_in(conn)


def test_a_completed_check_queues_the_next_for_its_due_time_and_check_now_brings_it_forward(
        registry):
    """S3a, the real Procrastinate schema: a new pool's first check is queued at once; once a
    check finishes, its next check is one `todo` job scheduled CHECK_EVERY_SECONDS after it; a
    second re-arm coalesces; "Check now" leaves one `todo` job, due now.
    Mutation probe: defer the next check without `scheduled_at` (due at once)."""
    library, _, key = _library(registry)
    assert [status for status, _ in _pool_jobs(registry, key)] == ["todo"]
    assert _pool_jobs(registry, key)[0][1] <= _db_now(registry)          # at once
    _run_first(registry, key)
    lease = library.begin_check(key)
    assert library.finish_check(lease, CheckComplete())
    with registry.db.transaction() as conn:
        finished = conn.execute("SELECT check_finished_at FROM photo_pools WHERE pool_key=%s",
                                (key,)).fetchone()["check_finished_at"]
    todo = [at for status, at in _pool_jobs(registry, key) if status == "todo"]
    assert todo == [pytest.approx(finished + CHECK_EVERY_SECONDS, abs=1e-3)]
    library.check_step_failed(key)                                        # a second re-arm
    assert [at for status, at in _pool_jobs(registry, key) if status == "todo"] == todo
    with registry.db.transaction() as conn:
        library._request_check_in(conn, key, by="console")
    todo = [at for status, at in _pool_jobs(registry, key) if status == "todo"]
    assert len(todo) == 1 and todo[0] <= _db_now(registry)


def test_a_check_step_that_fails_for_good_queues_the_pools_next_check(registry):
    """S3a: a step that raises until its retries run out leaves the pool's next check queued at
    its due time (the task's failure path). Mutation probe: queue only on completion."""
    library, _, key = _library(registry)
    _run_first(registry, key)

    def unreachable(secret):
        raise RuntimeError("the step broke")

    checks = LibraryChecks(library, unreachable, SimpleNamespace(utc=lambda: DUE))
    task = create_worker_app(registry.db.dsn).tasks[CHECK_POOL_TASK]
    context = SimpleNamespace(job=SimpleNamespace(attempts=len(CheckRetryStrategy.delays) + 1),
                              additional_context={"library_checks": checks})
    before = _db_now(registry)
    with pytest.raises(RuntimeError):
        asyncio.run(task.func(context, pool_key=key))
    todo = [at for status, at in _pool_jobs(registry, key) if status == "todo"]
    assert len(todo) == 1
    assert before + CHECK_EVERY_SECONDS <= todo[0] <= _db_now(registry) + CHECK_EVERY_SECONDS
    context.job.attempts = 1                                             # not the last attempt
    with pytest.raises(RuntimeError):
        asyncio.run(task.func(context, pool_key=key))
    assert [at for status, at in _pool_jobs(registry, key) if status == "todo"] == todo


def test_schema_install_is_repeatable_and_safe_across_concurrent_central_starts(registry):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(ProcrastinateMediaQueue.apply_schema, [registry.db.dsn] * 4))

    ProcrastinateMediaQueue.apply_schema(registry.db.dsn)
    with registry.db.transaction() as conn:
        assert conn.execute(
            "SELECT to_regclass('procrastinate_jobs') IS NOT NULL AS installed"
        ).fetchone()["installed"] is True


def test_a_check_step_is_retried_on_any_failure_until_its_last_attempt():
    strategy = CheckRetryStrategy()
    first = strategy.get_retry_decision(exception=RuntimeError("x"), job=SimpleNamespace(attempts=0))
    now = datetime.now(timezone.utc)
    assert 4 <= (first.retry_at - now).total_seconds() <= 5
    assert strategy.get_retry_decision(exception=RuntimeError("x"),
                                       job=SimpleNamespace(attempts=4)) is None


def test_retry_strategy_is_bounded_and_only_retries_coded_retryable_failures():
    strategy = MediaRetryStrategy()
    retry = RetryableMediaTask("upstream_unavailable")

    first = strategy.get_retry_decision(
        exception=retry, job=SimpleNamespace(attempts=1)
    )
    third = strategy.get_retry_decision(
        exception=retry, job=SimpleNamespace(attempts=3)
    )
    now = datetime.now(timezone.utc)
    assert 4 <= (first.retry_at - now).total_seconds() <= 5
    assert 59 <= (third.retry_at - now).total_seconds() <= 60
    assert strategy.get_retry_decision(
        exception=retry, job=SimpleNamespace(attempts=4)
    ) is None
    assert strategy.get_retry_decision(
        exception=RuntimeError("private"), job=SimpleNamespace(attempts=1)
    ) is None


def test_worker_app_registers_only_domain_work_and_maintenance_tasks():
    """S3a: no library poll and no rescue of its own (QueueRescue covers this queue)."""
    app = create_worker_app("postgresql:///unused")

    assert {
        name for name in app.tasks if name.startswith("photo_wall.")
    } == {
        CHECK_CONNECTION_TASK,
        CHECK_POOL_TASK,
        "photo_wall.media.maintenance",
        MEASURE_KEY_CONTENT_TASK,
    }
    assert app.tasks[MEASURE_KEY_CONTENT_TASK].lock is None
    assert isinstance(app.tasks[MEASURE_KEY_CONTENT_TASK].retry_strategy, MediaRetryStrategy)
    assert isinstance(app.tasks[CHECK_POOL_TASK].retry_strategy, CheckRetryStrategy)


def test_periodic_schedules_use_croniter_seconds_last_semantics():
    app = create_worker_app("postgresql:///unused")
    schedules = {
        task.task.name: task for task in app.periodic_registry.periodic_tasks.values()
    }
    base = datetime(2026, 9, 7, 18, 28, 19, tzinfo=timezone.utc).timestamp()
    maintenance = schedules["photo_wall.media.maintenance"].croniter.get_next(
        ret_type=float, start_time=base
    )
    assert set(schedules) == {"photo_wall.media.maintenance"}
    assert maintenance - base == 101


class StalledQueue:
    """The media queue's Procrastinate tables, driven through its own SQL functions: workers
    with a chosen heartbeat age, and fetches as a given worker."""

    def __init__(self, registry):
        self.db = registry.db
        self.dsn = registry.db.dsn
        ProcrastinateMediaQueue.apply_schema(self.dsn)
        self.media = ProcrastinateMediaQueue(self.dsn)

    def sql(self, query, params=()):
        with self.db.transaction() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else None

    def worker(self, *, heartbeat_age="0 seconds"):
        return self.sql("INSERT INTO procrastinate_workers (last_heartbeat) "
                        "VALUES (now() - %s::interval) RETURNING id", (heartbeat_age,))[0]["id"]

    def fetch(self, worker_id):
        row = self.sql("SELECT * FROM procrastinate_fetch_job_v2(%s, %s)",
                       ([MEDIA_QUEUE], worker_id))[0]
        return row if row["id"] is not None else None

    def pool_check(self, pool_key):
        with self.db.transaction() as conn:
            return self.media.enqueue_pool_check_in(conn, pool_key, due_at=0.0)

    def maintenance(self):
        with self.db.transaction() as conn:
            self.media.app.configure_task(
                "photo_wall.media.maintenance", queue=MEDIA_QUEUE, lock=MEDIA_STORAGE_LOCK,
                queueing_lock="media-maintenance", connection=conn).defer(timestamp=1)

    def rows(self):
        return [(r["task_name"], r["status"], r["args"]) for r in self.sql(
            "SELECT task_name,status::text AS status,args FROM procrastinate_jobs ORDER BY id")]


def _rescue(dsn):
    """The one rescue (central/infra/queue_ops.py `QueueRescue`), which covers this queue."""
    rescue = QueueRescue(dsn)

    async def run():
        try:
            return await rescue.rescue()
        finally:
            await rescue.aclose()

    return asyncio.run(run())


def test_a_dead_workers_media_jobs_go_back_to_the_queue_and_free_their_locks(registry):
    """S1. Mutation probe: finish a stalled row without deferring its copy."""
    queue = StalledQueue(registry)
    dead, live = queue.worker(heartbeat_age="1 hour"), queue.worker()
    lost, absorbed, running = "a" * 64, "b" * 64, "c" * 64
    queue.pool_check(lost)
    stalled_step = queue.fetch(dead)
    queue.maintenance()
    stalled_maintenance = queue.fetch(dead)
    queue.pool_check(running)
    beating = queue.fetch(live)
    queue.pool_check(absorbed)
    stalled_absorbed = queue.fetch(dead)
    assert queue.pool_check(absorbed).coalesced is False      # a copy waits behind its lock
    assert [stalled_step["task_name"], stalled_maintenance["task_name"],
            stalled_absorbed["task_name"], beating["task_name"]] == [
        CHECK_POOL_TASK, "photo_wall.media.maintenance", CHECK_POOL_TASK, CHECK_POOL_TASK]
    assert queue.fetch(live) is None                          # every lock is held

    assert _rescue(queue.dsn) == 3

    rows = queue.rows()
    assert rows[:4] == [
        (CHECK_POOL_TASK, "failed", {"pool_key": lost}),
        ("photo_wall.media.maintenance", "failed", {"timestamp": 1}),
        (CHECK_POOL_TASK, "doing", {"pool_key": running}),    # its worker beats: untouched
        (CHECK_POOL_TASK, "failed", {"pool_key": absorbed})]
    waiting = [(task, args) for task, status, args in rows if status == "todo"]
    assert sorted(waiting, key=str) == sorted([
        (CHECK_POOL_TASK, {"pool_key": absorbed}),            # the one that already waited
        (CHECK_POOL_TASK, {"pool_key": lost})], key=str)      # the periodic row is only finished
    copies = {queue.fetch(live)["args"]["pool_key"], queue.fetch(live)["args"]["pool_key"]}
    assert copies == {lost, absorbed}                         # the freed locks let them run
    assert _rescue(queue.dsn) == 0
