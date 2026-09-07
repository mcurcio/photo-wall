from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import procrastinate

from central.media_queue import (
    MEDIA_QUEUE,
    MEDIA_STORAGE_LOCK,
    PREPARE_MEDIA_TASK,
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
    } == {PREPARE_MEDIA_TASK, "photo_wall.media.refresh", "photo_wall.media.maintenance"}
    assert isinstance(
        app.tasks[PREPARE_MEDIA_TASK].retry_strategy,
        procrastinate.BaseRetryStrategy,
    )
