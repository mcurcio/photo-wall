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
from central.infra.queue_ops import QueueAdmin
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


def test_the_job_runtime_owns_and_closes_its_queue_ops_pool(tmp_path):
    # No caller-held pool: `run()` closes it when the runtime ends (JobRuntime tests prove when).
    runtime = build_job_runtime(StubDatabase(), ManualClock(0.0), cache_root=tmp_path, env=ENV)
    assert [type(owned) for owned in runtime._owned] == [QueueAdmin]


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
    from media.worker import _media_queue, _run_workers

    app, runtime = FakeMediaApp(), FakeRuntime()

    async def main() -> None:
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
        media = _media_queue(app, {"media_worker": object()})
        await asyncio.wait_for(_run_workers(media, runtime), 5)
        # the handler is removed again: SIGTERM is back to the default disposition
        assert signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, None)

    asyncio.run(main())
    assert runtime.stops == 1
    assert app.stopped_gracefully
    assert app.kwargs is not None
    assert app.kwargs["queues"] == ["photo-wall-media"]
    assert app.kwargs["install_signal_handlers"] is False


def test_a_failing_loop_stops_the_other():
    from media.worker import _media_queue, _run_workers

    class Broken(FakeRuntime):
        async def run(self) -> None:
            raise RuntimeError("boom")

    app = FakeMediaApp()

    async def main() -> None:
        await _run_workers(_media_queue(app, {}), Broken())

    with pytest.raises(ExceptionGroup):
        asyncio.run(main())
    assert app.stopped_gracefully


class Returns(FakeRuntime):
    async def run(self) -> None:
        return None  # e.g. procrastinate ended a worker whose LISTEN connection failed


async def _returns() -> None:
    return None


@pytest.mark.parametrize("which", ["media", "jobs"])
def test_a_loop_that_returns_while_not_stopping_fails_the_worker(which):
    from media.worker import _media_queue, _run_workers

    app = FakeMediaApp()

    async def main() -> None:
        if which == "media":
            await _run_workers(_returns(), FakeRuntime())
        else:
            await _run_workers(_media_queue(app, {}), Returns())

    with pytest.raises(ExceptionGroup) as caught:
        asyncio.run(asyncio.wait_for(main(), 5))
    assert [str(e) for e in caught.value.exceptions] == ["worker_exited"]
    if which == "jobs":
        assert app.stopped_gracefully


class HeldLock:
    """`MediaStore.worker_lock` of a store whose lock another process holds `busy` times."""

    def __init__(self, busy: int) -> None:
        self.busy, self.attempts, self.held = busy, 0, False

    def worker_lock(self):
        import contextlib

        from central.media_store import MediaStoreError

        self.attempts += 1
        if self.attempts <= self.busy:
            raise MediaStoreError("media_writer_active", 503)

        @contextlib.contextmanager
        def held():
            self.held = True
            try:
                yield self
            finally:
                self.held = False

        return held()


class MediaStub:
    def __init__(self, store: HeldLock) -> None:
        self.store, self.boot = store, []

    async def _register_recipe(self) -> None:
        self.boot.append(("recipe", self.store.held))

    async def maintain(self) -> None:
        self.boot.append(("maintain", self.store.held))

    async def refresh_once(self) -> bool:
        self.boot.append(("refresh", self.store.held))
        return False


def test_the_media_loop_stands_by_while_another_process_writes_and_jobs_run_meanwhile():
    from media.worker import _media_writer, _run_workers

    store = HeldLock(busy=3)
    media, queue_ran = MediaStub(store), []

    class Started(FakeRuntime):
        started = False

        async def run(self) -> None:
            self.started = True
            await super().run()

    runtime = Started()

    async def queue() -> None:
        queue_ran.append(store.held)
        await asyncio.Event().wait()

    async def main() -> None:
        running = asyncio.create_task(_run_workers(
            _media_writer(media, queue, standby_seconds=0.01), runtime))
        async with asyncio.timeout(5):
            while not queue_ran:
                assert runtime.started or store.attempts <= 1
                await asyncio.sleep(0.005)
        assert runtime.started  # the job runtime never waited for the media lock
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(running, 5)

    asyncio.run(main())
    assert store.attempts == 4 and queue_ran == [True]
    assert media.boot == [("recipe", True), ("maintain", True), ("refresh", True)]
    assert not store.held  # released on stop


def test_the_media_loop_does_not_stand_by_on_other_lock_errors():
    from central.media_store import MediaStoreError
    from media.worker import _media_writer

    class Broken:
        def worker_lock(self):
            raise MediaStoreError("media_io", 503)

    async def never() -> None:
        raise AssertionError("the queue must not start")

    with pytest.raises(MediaStoreError, match="media_io"):
        asyncio.run(_media_writer(MediaStub(Broken()), never, standby_seconds=0.01))


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
