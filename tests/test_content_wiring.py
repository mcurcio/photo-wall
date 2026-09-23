"""P2.1: the composition helper, the worker root's one signal handler, and migration 022.

The wiring cases need no database: nothing in `central.content_wiring` connects at build time.
The 022 case needs PostgreSQL (`PHOTO_WALL_TEST_DATABASE_URL`) and skips without it.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import signal
from pathlib import Path

import pytest
from runtime_fakes import apply_procrastinate_schema

from central.content_wiring import (
    WAITER_SLOTS,
    WORKER_CONCURRENCY,
    ContentServices,
    build_content_services,
    build_job_runtime,
)
from central.infra.outcome_feed import OutcomeFeed
from central.infra.runtime import JobRuntime
from central.kernel.jobs import QueueName
from contracts.time import ManualClock

DSN = "postgresql://unused.invalid/none"
ENV = {
    "PHOTO_WALL_RELEASE_REPO": "example/photo-wall",
    "PHOTO_WALL_RELEASE_TOKEN": "token-value",
    "PHOTO_WALL_RELEASE_PRERELEASES": "true",
}
MIGRATION_022 = (Path(__file__).parents[1] / "central" / "migrations"
                 / "022_retire_release_queue.sql")


class StubDatabase:
    """Only what the wiring reads at build time; any query would fail loudly."""

    dsn = DSN

    def healthy(self) -> bool:
        return True

    def transaction(self):
        raise AssertionError("the wiring must not touch the database at build time")


def test_build_job_runtime_passes_every_boot_check(tmp_path):
    # JobRuntime.__init__ raises unless every CATALOG type has exactly one handler and the
    # concurrency map names exactly the catalog's queues.
    runtime = build_job_runtime(StubDatabase(), ManualClock(0.0), cache_root=tmp_path, env=ENV)
    assert isinstance(runtime, JobRuntime)
    assert dict(WORKER_CONCURRENCY) == {QueueName.FETCH: 2, QueueName.UPKEEP: 2}


def test_build_content_services_owns_one_feed_and_binds_no_handler(tmp_path):
    services = build_content_services(StubDatabase(), ManualClock(0.0), cache_root=tmp_path)
    assert isinstance(services, ContentServices)
    assert isinstance(services.feed, OutcomeFeed)
    assert services.probe.ready() is True
    assert WAITER_SLOTS == 32


def test_the_worker_module_imports_without_the_release_queue():
    module = importlib.import_module("media.worker")
    assert not hasattr(module, "worker_queues")
    assert not hasattr(module, "register_app_release_tasks")


class FakeMediaApp:
    def __init__(self) -> None:
        self.kwargs: dict | None = None
        self.stopped_gracefully = False

    async def run_worker_async(self, **kwargs) -> None:
        self.kwargs = kwargs
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.stopped_gracefully = True  # procrastinate: finish running jobs, then re-raise
            raise


class FakeRuntime:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self.stops = 0

    async def run(self) -> None:
        await self._stop.wait()

    def stop(self) -> None:
        self.stops += 1
        self._stop.set()


def test_one_sigterm_stops_both_loops_and_the_worker_returns_normally():
    from media.worker import _run_workers

    app, runtime = FakeMediaApp(), FakeRuntime()

    async def main() -> None:
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(_run_workers(app, runtime, {"media_worker": object()}), 5)
        # the handler is removed again: SIGTERM is back to the default disposition
        assert signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, None)

    asyncio.run(main())
    assert runtime.stops == 1
    assert app.stopped_gracefully
    assert app.kwargs is not None
    assert app.kwargs["queues"] == ["photo-wall-media"]
    assert app.kwargs["install_signal_handlers"] is False


def test_a_failing_loop_stops_the_other():
    from media.worker import _run_workers

    class Broken(FakeRuntime):
        async def run(self) -> None:
            raise RuntimeError("boom")

    app = FakeMediaApp()
    with pytest.raises(ExceptionGroup):
        asyncio.run(_run_workers(app, Broken(), {}))
    assert app.stopped_gracefully


# -- migration 022 (PostgreSQL) --------------------------------------------------------------------


def test_022_cancels_pending_release_queue_tasks_only(registry):
    db = registry.db
    apply_procrastinate_schema(db.dsn)
    with db.transaction() as conn:
        for queue, status in (("photo-wall-app-release", "todo"),
                              ("photo-wall-app-release", "succeeded"),
                              ("photo-wall-media", "todo")):
            conn.execute(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status) "
                "VALUES (%s, 'legacy.task', '{}', %s)", (queue, status))
    with db.transaction() as conn:
        conn.execute(MIGRATION_022.read_text())
    with db.transaction() as conn:
        rows = conn.execute("SELECT queue_name, status::text AS status FROM procrastinate_jobs "
                            "ORDER BY id").fetchall()
        events = conn.execute("SELECT type::text AS type FROM procrastinate_events "
                              "WHERE type = 'cancelled'").fetchall()
    assert [(r["queue_name"], r["status"]) for r in rows] == [
        ("photo-wall-app-release", "cancelled"),
        ("photo-wall-app-release", "succeeded"),
        ("photo-wall-media", "todo"),
    ]
    assert len(events) == 1


def test_022_is_a_no_op_before_procrastinate_is_installed(registry):
    # db.migrate() already ran 022 in the fixture, before any procrastinate schema existed.
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT to_regclass('procrastinate_jobs') IS NULL AS absent"
                            ).fetchone()["absent"]
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = %s",
                            (MIGRATION_022.name,)).fetchone() is not None
