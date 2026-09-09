from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace

import procrastinate

from central.media_queue import (
    MEDIA_QUEUE,
    MEDIA_REFRESH_LOCK_PREFIX,
    MEDIA_STORAGE_LOCK,
    PREPARE_MEDIA_TASK,
    REFRESH_MEDIA_SOURCE_TASK,
    ProcrastinateMediaQueue,
)
from media.task_queue import MediaRetryStrategy, RetryableMediaTask, create_worker_app


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


def test_refresh_adapter_uses_source_scoped_coalescing_and_shared_execution_lock():
    queue = object.__new__(ProcrastinateMediaQueue)
    queue.app = App()
    connection = SimpleNamespace(transaction=nullcontext)

    receipt = queue.enqueue_refresh_in(connection, "source:one")

    assert not receipt.coalesced
    assert queue.app.configured == {
        "name": REFRESH_MEDIA_SOURCE_TASK,
        "queue": MEDIA_QUEUE,
        "lock": MEDIA_REFRESH_LOCK_PREFIX + "source:one",
        "queueing_lock": "media-refresh:source:one",
        "connection": connection,
        "payload": {"source_ref": "source:one"},
    }


def test_refresh_adapter_coalesces_already_queued_request_without_private_job_lookup():
    class Savepoint:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class TransitionConnection:
        def transaction(self):
            return Savepoint()

    class TransitionDeferrer:
        def defer(self, **kwargs):
            del kwargs
            raise procrastinate.exceptions.AlreadyEnqueued()

    class TransitionApp(App):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def configure_task(self, name, **kwargs):
            self.calls += 1
            self.configured = {"name": name, **kwargs}
            return TransitionDeferrer()

    queue = object.__new__(ProcrastinateMediaQueue)
    queue.app = TransitionApp()

    receipt = queue.enqueue_refresh_in(TransitionConnection(), "source:one")

    assert receipt.coalesced
    assert queue.app.calls == 1


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

    assert PREPARE_MEDIA_TASK in app.tasks
    assert {
        name for name in app.tasks if name.startswith("photo_wall.")
    } == {
        PREPARE_MEDIA_TASK,
        REFRESH_MEDIA_SOURCE_TASK,
        "photo_wall.media.refresh",
        "photo_wall.media.maintenance",
    }
    assert isinstance(
        app.tasks[PREPARE_MEDIA_TASK].retry_strategy,
        procrastinate.BaseRetryStrategy,
    )
    assert app.tasks[REFRESH_MEDIA_SOURCE_TASK].lock is None
    assert app.tasks["photo_wall.media.refresh"].lock is None


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

    assert refresh - base == 11
    assert maintenance - base == 101
