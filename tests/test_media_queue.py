import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import procrastinate
import pytest

from central.media_queue import (
    CHECK_CONNECTION_TASK,
    CHECK_POOL_TASK,
    KEY_CONTENT_QUEUE_PREFIX,
    LIBRARY_CONNECTION_LOCK_PREFIX,
    LIBRARY_POOL_LOCK_PREFIX,
    MEASURE_KEY_CONTENT_TASK,
    MEDIA_QUEUE,
    MEDIA_STORAGE_LOCK,
    PREPARE_MEDIA_TASK,
    ProcrastinateMediaQueue,
)
from media.task_queue import (
    MAINTENANCE_CRON,
    REFRESH_CRON,
    RESCUE_CRON,
    RESCUE_TASK,
    WORKER_CHECK_IN_SECONDS,
    WORKER_FRESH_SECONDS,
    MediaRetryStrategy,
    RetryableMediaTask,
    create_worker_app,
    rescue_stalled,
)


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


def test_adapter_passes_the_caller_owned_connection_and_exact_job_identity():
    queue = object.__new__(ProcrastinateMediaQueue)
    queue.app = App()
    connection = object()

    assert queue.enqueue_in(connection, "job-one") == 41
    assert queue.app.configured == {
        "name": PREPARE_MEDIA_TASK,
        "queue": MEDIA_QUEUE,
        "lock": MEDIA_STORAGE_LOCK,
        "queueing_lock": "job-one",
        "connection": connection,
        "payload": {"job_id": "job-one"},
    }


@pytest.mark.parametrize("enqueue,task,prefix,argument", [
    ("enqueue_connection_check_in", CHECK_CONNECTION_TASK, LIBRARY_CONNECTION_LOCK_PREFIX,
     "connection_ref"),
    ("enqueue_pool_check_in", CHECK_POOL_TASK, LIBRARY_POOL_LOCK_PREFIX, "pool_key"),
])
def test_library_checks_are_locked_per_connection_or_pool_and_coalesce(enqueue, task, prefix,
                                                                        argument):
    """Roadmap 4 (C1, C2): one check of a connection, one step of a pool, at a time; a second
    request while one waits coalesces."""
    queue = object.__new__(ProcrastinateMediaQueue)
    queue.app = App()
    connection = SimpleNamespace(transaction=nullcontext)

    receipt = getattr(queue, enqueue)(connection, "library-0123456789ab")

    assert not receipt.coalesced
    assert queue.app.configured == {
        "name": task,
        "queue": MEDIA_QUEUE,
        "lock": prefix + "library-0123456789ab",
        "queueing_lock": prefix + "library-0123456789ab",
        "connection": connection,
        "payload": {argument: "library-0123456789ab"},
    }

    class Queued:
        def defer(self, **kwargs):
            raise procrastinate.exceptions.AlreadyEnqueued()

    queue.app.configure_task = lambda name, **kwargs: Queued()
    assert getattr(queue, enqueue)(connection, "library-0123456789ab").coalesced


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
        "connection": connection,
        "payload": {"asset_id": "asset-1"},
    }


def test_a_queued_connection_check_coalesces_in_procrastinate(registry):
    ProcrastinateMediaQueue.apply_schema(registry.db.dsn)
    queue = ProcrastinateMediaQueue(registry.db.dsn)
    with registry.db.transaction() as conn:
        first = queue.enqueue_connection_check_in(conn, "library-0123456789ab")
        second = queue.enqueue_connection_check_in(conn, "library-0123456789ab")
        other = queue.enqueue_pool_check_in(conn, "a" * 64)
    assert (first.coalesced, second.coalesced, other.coalesced) == (False, True, False)
    with registry.db.transaction() as conn:
        jobs = conn.execute("SELECT task_name,args FROM procrastinate_jobs ORDER BY id").fetchall()
    assert [(job["task_name"], job["args"]) for job in jobs] == [
        (CHECK_CONNECTION_TASK, {"connection_ref": "library-0123456789ab"}),
        (CHECK_POOL_TASK, {"pool_key": "a" * 64})]


def test_schema_install_is_repeatable_and_safe_across_concurrent_central_starts(registry):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(ProcrastinateMediaQueue.apply_schema, [registry.db.dsn] * 4))

    ProcrastinateMediaQueue.apply_schema(registry.db.dsn)
    with registry.db.transaction() as conn:
        assert conn.execute(
            "SELECT to_regclass('procrastinate_jobs') IS NOT NULL AS installed"
        ).fetchone()["installed"] is True


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
    app = create_worker_app("postgresql:///unused")

    assert {
        name for name in app.tasks if name.startswith("photo_wall.")
    } == {
        PREPARE_MEDIA_TASK,
        "photo_wall.media.refresh",
        CHECK_CONNECTION_TASK,
        CHECK_POOL_TASK,
        "photo_wall.media.maintenance",
        RESCUE_TASK,
        MEASURE_KEY_CONTENT_TASK,
    }
    assert isinstance(
        app.tasks[PREPARE_MEDIA_TASK].retry_strategy,
        procrastinate.BaseRetryStrategy,
    )
    assert app.tasks["photo_wall.media.refresh"].lock is None
    assert app.tasks[MEASURE_KEY_CONTENT_TASK].lock is None
    assert isinstance(app.tasks[MEASURE_KEY_CONTENT_TASK].retry_strategy, MediaRetryStrategy)
    # A dead worker's row holding MEDIA_STORAGE_LOCK must never block its own rescue.
    assert app.tasks[RESCUE_TASK].lock is None
    assert app.tasks[RESCUE_TASK].queueing_lock == "media-rescue"


def test_periodic_schedules_use_croniter_seconds_last_semantics():
    app = create_worker_app("postgresql:///unused")
    schedules = {
        task.task.name: task for task in app.periodic_registry.periodic_tasks.values()
    }
    base = datetime(2026, 9, 7, 18, 28, 19, tzinfo=timezone.utc).timestamp()

    refresh = schedules["photo_wall.media.refresh"].croniter.get_next(
        ret_type=float, start_time=base
    )
    maintenance = schedules["photo_wall.media.maintenance"].croniter.get_next(
        ret_type=float, start_time=base
    )

    rescue = schedules[RESCUE_TASK].croniter.get_next(ret_type=float, start_time=base)

    assert refresh - base == 11
    assert maintenance - base == 101
    assert rescue - base == 11
    assert RESCUE_CRON == REFRESH_CRON


def _every_seconds(cron):
    """The period of a Procrastinate periodic schedule written as `*/N` in one field
    (minute, hour, day, month, weekday, second)."""
    fields = cron.split()
    steps = [(index, int(field[2:])) for index, field in enumerate(fields) if field.startswith("*/")]
    assert len(steps) == 1, cron
    index, step = steps[0]
    assert index in (0, 5), cron
    return step * 60 if index == 0 else step


def test_the_console_media_thresholds_follow_the_worker_schedule():
    # Slice 3 §14: "quiet" and "overdue" are console constants derived from these.
    source = (Path(__file__).parents[1] / "central/console/src/mediaHealth.js").read_text()
    pinned = dict(re.findall(r"^export const ([A-Z_]+_SECONDS) = (\d+);$", source, re.MULTILINE))
    assert int(pinned["WORKER_CHECK_IN_SECONDS"]) == _every_seconds(MAINTENANCE_CRON)


def test_the_worker_healthcheck_window_is_the_console_worker_quiet_window():
    # HC-2: one cadence (boot + maintenance), one window; Python and the console cannot disagree.
    source = (Path(__file__).parents[1] / "central/console/src/mediaHealth.js").read_text()
    pinned = dict(re.findall(r"^export const ([A-Z_]+_SECONDS) = (\d+);$", source, re.MULTILINE))
    assert WORKER_CHECK_IN_SECONDS == _every_seconds(MAINTENANCE_CRON)
    assert int(pinned["WORKER_CHECK_IN_SECONDS"]) == WORKER_CHECK_IN_SECONDS
    quiet = re.search(r"^export const WORKER_QUIET_AFTER = (\d+) \* WORKER_CHECK_IN_SECONDS \+ (\d+);$",
                      source, re.MULTILINE)
    assert quiet, "WORKER_QUIET_AFTER is no longer `a * WORKER_CHECK_IN_SECONDS + b`"
    assert int(quiet[1]) * int(pinned["WORKER_CHECK_IN_SECONDS"]) + int(quiet[2]) == WORKER_FRESH_SECONDS
    assert WORKER_FRESH_SECONDS == 660


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
            return self.media.enqueue_pool_check_in(conn, pool_key)

    def maintenance(self):
        with self.db.transaction() as conn:
            self.media.app.configure_task(
                "photo_wall.media.maintenance", queue=MEDIA_QUEUE, lock=MEDIA_STORAGE_LOCK,
                queueing_lock="media-maintenance", connection=conn).defer(timestamp=1)

    def rows(self):
        return [(r["task_name"], r["status"], r["args"]) for r in self.sql(
            "SELECT task_name,status::text AS status,args FROM procrastinate_jobs ORDER BY id")]


def _rescue(dsn):
    app = create_worker_app(dsn)

    async def run():
        async with app.open_async():
            return await rescue_stalled(app)

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
