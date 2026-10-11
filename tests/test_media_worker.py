"""Procrastinate media task execution with private generated configuration."""

import asyncio
import hashlib
import inspect
import json

import pytest
from media_queue import RecordingMediaQueue
from support.library_store_fake import FakeLibraryStore
from test_media_store import ORIGINAL, RECIPE, VARIANT, grant, queued, ready, row

from central.db import ProcessTransactionClock
from central.media_queue import MEDIA_QUEUE, ProcrastinateMediaQueue
from central.media_repository import MediaRepository, StoreLimits
from central.media_store import MediaStore
from central.photo_library.model import LibraryAsset
from central.planner import AcquisitionRequest
from central.registry import RegistryError
from media.library_client import FetchedRendition
from media.models import MediaError
from media.prepare import BuildIdentity, PreparedMedia
from media.task_queue import (
    MediaTaskFailed,
    ProcrastinateCheckQueue,
    RetryableMediaTask,
    create_worker_app,
)
from media.worker import MediaWorker, WorkerLimits, _media_writer

SECRET = "PUBLIC_SYNTHETIC_PRIVATE_KEY"


def test_no_environment_variable_names_a_connections_file(worker_storage):
    """Roadmap 4: connections and keys are the store's; the worker reads no file and reports no
    connection list (the photo library's own view replaces it)."""
    import media.worker as module
    source = inspect.getsource(module)
    assert "PHOTO_WALL_CONNECTIONS_FILE" not in source and "load_connections" not in source
    instance = worker(worker_storage, FakeLibrary(worker_storage.clock))
    instance.store.recover = lambda: None
    asyncio.run(instance.maintain())
    assert "connection_ids" not in worker_storage.repository.health()


class FakeLibrary:
    """The connection's LibraryClient: fetches write the fetched rendition's bytes."""

    def __init__(self, clock):
        self.clock = clock
        self.downloads, self.closed = 0, False
        self.edges = []
        self.fault = None
        self.gate = None
        self.entered = asyncio.Event()
        self.cancelled = False

    async def fetch_rendition(self, asset, *, frame_long_edge, destination):
        self.downloads += 1
        self.edges.append(frame_long_edge)
        destination.write_bytes(b"partial public original")
        self.entered.set()
        try:
            if self.gate:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.fault:
            raise self.fault
        destination.write_bytes(ORIGINAL)
        return FetchedRendition(path=destination, rendition="preview", media_type="image/jpeg",
                                size=len(ORIGINAL), sha256=hashlib.sha256(ORIGINAL).hexdigest(),
                                width=1080, height=1920)

    async def close(self):
        self.closed = True


class FakePreparer:
    def __init__(self):
        from contracts.models import Variant
        self.variant = Variant(sha256=hashlib.sha256(VARIANT).hexdigest(), size=len(VARIANT),
                               media_type="image/jpeg", width=1080, height=1920)
        self.build = BuildIdentity(preparation_sha256="a" * 64, ffmpeg_sha256="b" * 64,
            ffprobe_sha256="c" * 64, ffmpeg_version="synthetic", ffprobe_version="synthetic",
            python_version="synthetic", pillow_version="synthetic", littlecms_version="synthetic",
            jpeg_version="synthetic", zlib_version="synthetic", platform="synthetic",
            memory_limit_enforced=False)
        self.recipe, self.fault, self.gate = RECIPE, None, None
        self.entered = asyncio.Event()
        self.cancelled = False
        self.prepared = []

    async def describe_recipe(self):
        return RECIPE, self.build

    async def prepare_rendition(self, asset, rendition, destination):
        self.prepared.append(asset)
        source = rendition.path
        destination.write_bytes(VARIANT)
        self.entered.set()
        try:
            if self.gate:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.fault:
            raise self.fault
        return PreparedMedia(path=destination, variant=self.variant, recipe_id=self.recipe,
            build=self.build, original_sha256=hashlib.sha256(source.read_bytes()).hexdigest())


@pytest.fixture
def worker_storage(registry, tmp_path):
    repository = MediaRepository(registry.db, registry.clock, StoreLimits(
        max_bytes=10000, max_original_bytes=1000, max_image_bytes=1000,
        max_video_bytes=2000), queue=RecordingMediaQueue(), times=ProcessTransactionClock(registry.clock))
    repository.set_recipe(RECIPE)
    return MediaStore(repository, tmp_path / "media")


def library_store(clock):
    library = FakeLibraryStore(clock)
    library.add_connection("fixture", "https://immich.invalid/api", SECRET)
    library.frame_edge = 2160
    return library


def worker(storage, source, preparer=None, **kwargs):
    return MediaWorker(storage.repository, storage, library_store(storage.clock),
                       preparer=preparer or FakePreparer(),
                       client_factory=lambda _: source, **kwargs)


async def close(instance):
    await instance._close_clients()


def test_procrastinate_task_processes_only_exact_job_and_publishes(worker_storage):
    queued(worker_storage)
    queued(worker_storage, 2)
    source = FakeLibrary(worker_storage.clock)
    instance = worker(worker_storage, source)
    async def exercise():
        try:
            with worker_storage.worker_lock():
                await instance.process_job("job-2", attempt=1)
        finally:
            await close(instance)
    asyncio.run(exercise())
    assert row(worker_storage, "media_jobs", "id", "job-1")["state"] == "queued"
    assert row(worker_storage, "media_jobs", "id", "job-2")["state"] == "ready"
    assert (worker_storage.root / "blobs" / hashlib.sha256(VARIANT).hexdigest()).read_bytes() == VARIANT
    assert worker_storage.repository.health()["accounted_bytes"] == len(VARIANT)


def test_retryable_task_cleanup_leaves_domain_job_claimable_without_local_delay(worker_storage):
    queued(worker_storage)
    source = FakeLibrary(worker_storage.clock)
    source.fault = MediaError("upstream_unavailable")
    instance = worker(worker_storage, source)
    with worker_storage.worker_lock(), pytest.raises(RetryableMediaTask, match="upstream_unavailable"):
        asyncio.run(instance.process_job("job-1", attempt=1))
    job = row(worker_storage, "media_jobs", "id", "job-1")
    assert job["state"] == "retry" and job["retry_at"] == 0
    assert job["reserved_bytes"] == 0


def test_procrastinate_worker_consumes_deferred_job(worker_storage):
    queued(worker_storage)
    queue = ProcrastinateMediaQueue(worker_storage.db.dsn)
    queue.apply_schema(worker_storage.db.dsn)
    with worker_storage.repository.transaction() as conn:
        queue.enqueue_in(conn, "job-1")
    instance = worker(worker_storage, FakeLibrary(worker_storage.clock))

    async def exercise():
        app = create_worker_app(worker_storage.db.dsn)
        try:
            with worker_storage.worker_lock():
                async with app.open_async():
                    await app.run_worker_async(queues=[MEDIA_QUEUE], concurrency=1, wait=False,
                                               additional_context={"media_worker": instance})
        finally:
            await close(instance)
    asyncio.run(exercise())
    assert row(worker_storage, "media_jobs", "id", "job-1")["state"] == "ready"


@pytest.mark.parametrize("failure,state,error", [
    (MediaError("unsupported_color", "incompatible"), "failed", MediaTaskFailed),
    (MediaError("asset_integrity"), "failed", MediaTaskFailed),
    (MediaError("asset_oversize", "incompatible"), "failed", MediaTaskFailed),
    (MediaError("asset_unavailable"), "retry", RetryableMediaTask),
    (MediaError("asset_missing"), "retry", RetryableMediaTask),
    (MediaError("unsupported_version", "incompatible"), "retry", RetryableMediaTask),
    (MediaError("upstream_schema", "incompatible"), "retry", RetryableMediaTask),
    (MediaError("upstream_permission", "permission"), "retry", RetryableMediaTask),
    (RuntimeError(SECRET), "retry", RetryableMediaTask),
])
def test_task_failures_are_typed_and_do_not_leak(worker_storage, failure, state, error):
    queued(worker_storage)
    source = FakeLibrary(worker_storage.clock)
    source.fault = failure
    instance = worker(worker_storage, source)
    with worker_storage.worker_lock(), pytest.raises(error) as caught:
        asyncio.run(instance.process_job("job-1", attempt=1))
    job = row(worker_storage, "media_jobs", "id", "job-1")
    assert job["state"] == state and job["retry_at"] == 0 and job["reserved_bytes"] == 0
    assert SECRET not in str(caught.value)
    assert not list((worker_storage.root / "staging").glob("*/*/*"))


def test_fourth_queue_attempt_becomes_final_domain_failure(worker_storage):
    queued(worker_storage)
    source = FakeLibrary(worker_storage.clock)
    source.fault = MediaError("upstream_unavailable")
    instance = worker(worker_storage, source)
    with worker_storage.worker_lock(), pytest.raises(MediaTaskFailed):
        asyncio.run(instance.process_job("job-1", attempt=4))
    assert row(worker_storage, "media_jobs", "id", "job-1")["state"] == "failed"


def test_cancellation_cleans_partial_bytes_and_keeps_attempt_fence(worker_storage):
    queued(worker_storage)
    source = FakeLibrary(worker_storage.clock)
    source.gate = asyncio.Event()
    instance = worker(worker_storage, source)
    async def exercise():
        with worker_storage.worker_lock():
            task = asyncio.create_task(instance.process_job("job-1", attempt=1))
            await asyncio.wait_for(source.entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await close(instance)
    asyncio.run(exercise())
    assert source.cancelled and source.closed
    assert row(worker_storage, "media_jobs", "id", "job-1")["state"] == "retry"
    assert worker_storage.repository.health()["accounted_bytes"] == 0


def test_recipe_change_between_claim_and_preparation_never_publishes(worker_storage):
    queued(worker_storage)
    preparation = FakePreparer()
    preparation.recipe = "f" * 64
    instance = worker(worker_storage, FakeLibrary(worker_storage.clock), preparation)
    with worker_storage.worker_lock(), pytest.raises(RetryableMediaTask, match="recipe_changed"):
        asyncio.run(instance.process_job("job-1", attempt=1))
    assert row(worker_storage, "media_jobs", "id", "job-1")["state"] == "retry"
    assert worker_storage.repository.health()["accounted_bytes"] == 0


def test_pressure_collects_unused_content_then_processes_request(worker_storage):
    with worker_storage.worker_lock():
        ready(worker_storage)
    queued(worker_storage, 2)
    worker_storage.set_quota(2010)
    instance = worker(worker_storage, FakeLibrary(worker_storage.clock))
    with worker_storage.worker_lock():
        asyncio.run(instance.process_job("job-2", attempt=1))
    assert row(worker_storage, "media_jobs", "id", "job-2")["state"] == "ready"
    evicted = row(worker_storage, "media_jobs", "id", "job-1")
    assert evicted["state"] == "evicted" and evicted["variant_sha"] is None


def test_pressure_cannot_collect_secured_content(worker_storage, registry):
    with worker_storage.worker_lock():
        _, _, prepared = ready(worker_storage)
        grant(worker_storage, registry, prepared.variant, secured=True)
    queued(worker_storage, 2)
    worker_storage.set_quota(2010)
    source = FakeLibrary(worker_storage.clock)
    instance = worker(worker_storage, source)
    with worker_storage.worker_lock(), pytest.raises(RetryableMediaTask, match="media_job_unavailable"):
        asyncio.run(instance.process_job("job-2", attempt=1))
    assert source.downloads == 0
    assert row(worker_storage, "media_jobs", "id", "job-2")["state"] == "queued"


def test_evicted_job_only_requeues_on_new_request_and_obeys_capacity(worker_storage):
    with worker_storage.worker_lock():
        lease, _, _ = ready(worker_storage)
        worker_storage.collect(target_bytes=0)
    queued(worker_storage, 2)
    worker_storage.repository.limits = worker_storage.repository.limits.model_copy(
        update={"max_jobs": 1})
    request = AcquisitionRequest(asset_id=lease.asset.asset_id,
        assignment_ids=("new-assignment",), earliest_start=1100)
    with pytest.raises(RegistryError, match="job_capacity"):
        worker_storage.repository.request_acquisitions((request,))
    with worker_storage.repository.transaction() as conn:
        conn.execute("UPDATE media_jobs SET state='failed' WHERE id='job-2'")
    assert worker_storage.repository.request_acquisitions((request,)) == 1
    assert row(worker_storage, "media_jobs", "id", "job-1")["state"] == "queued"


def test_stale_failure_cleanup_cannot_overwrite_newer_attempt(worker_storage):
    queued(worker_storage)
    source = FakeLibrary(worker_storage.clock)
    source.gate = asyncio.Event()
    instance = worker(worker_storage, source)
    async def exercise():
        with worker_storage.worker_lock():
            task = asyncio.create_task(instance.process_job("job-1", attempt=1))
            await asyncio.wait_for(source.entered.wait(), 5)
            with worker_storage.repository.transaction() as conn:
                conn.execute("UPDATE media_jobs SET attempt_token='replacement' WHERE id='job-1'")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await close(instance)
    asyncio.run(exercise())
    job = row(worker_storage, "media_jobs", "id", "job-1")
    assert job["attempt_token"] == "replacement" and job["state"] == "running"
    assert job["reserved_bytes"] == 2000


def test_outer_job_deadline_cancels_partial_download(worker_storage):
    queued(worker_storage)
    source = FakeLibrary(worker_storage.clock)
    source.gate = asyncio.Event()
    instance = worker(worker_storage, source, limits=WorkerLimits(job_seconds=.2))
    with worker_storage.worker_lock(), pytest.raises(RetryableMediaTask, match="worker_timeout"):
        asyncio.run(instance.process_job("job-1", attempt=1))
    assert source.cancelled
    job = row(worker_storage, "media_jobs", "id", "job-1")
    assert job["state"] == "retry" and job["retry_at"] == 0
    assert worker_storage.repository.health()["accounted_bytes"] == 0


def test_nonfinite_clock_cannot_start_task_activity(worker_storage):
    instance = worker(worker_storage, FakeLibrary(worker_storage.clock))
    worker_storage.clock.wall = float("nan")
    with pytest.raises(MediaError, match="clock_invalid"):
        asyncio.run(instance.process_job("missing", attempt=1))
    with worker_storage.db.transaction() as conn:
        assert conn.execute("SELECT worker_seen FROM media_settings WHERE singleton").fetchone()["worker_seen"] == 1000


def test_hung_client_close_is_bounded(worker_storage):
    source = FakeLibrary(worker_storage.clock)
    instance = worker(worker_storage, source, limits=WorkerLimits(close_seconds=.05))
    asyncio.run(instance._client("fixture"))
    async def hang():
        await asyncio.Event().wait()
    source.close = hang
    asyncio.run(instance._close_clients())
    assert instance._error == "connection_close"


def test_command_startup_error_contains_no_environment_or_raw_exception(monkeypatch, capsys):
    import media.worker as module
    monkeypatch.setattr(module.sys, "argv", ["media.worker"])
    monkeypatch.delenv("PHOTO_WALL_DATABASE_URL", raising=False)
    assert module.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.err) == {"error": "worker_config"}
    assert not output.out


@pytest.mark.parametrize(("error", "code"), [
    (RuntimeError("worker_exited"), "worker_exited"),
    (RuntimeError("completion_not_recorded"), "completion_not_recorded"),
    (RuntimeError("something else"), "worker_internal"),
])
def test_command_reports_the_job_runtime_exit_code(monkeypatch, capsys, error, code):
    """`until_stopped`/`JobRuntime.run` end the process with these codes; `main()` names them."""
    import media.worker as module

    async def failing_entry():
        raise ExceptionGroup("worker loops", [error])

    monkeypatch.setattr(module, "_entry", failing_entry)
    assert module.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.err) == {"error": code}
    assert not output.out


def test_the_boot_path_runs_maintenance_and_checks_the_worker_in_then_runs_the_queue(
        worker_storage):
    """No refresh, preview or tag list at boot (roadmap 4): the library tick queues checks."""
    instance = worker(worker_storage, FakeLibrary(worker_storage.clock))
    instance.store.recover = lambda: None
    ran = []

    async def queue():
        ran.append(worker_storage.repository.health()["worker_seen"])

    asyncio.run(_media_writer(instance, queue))
    assert ran == [1000.0]
    for gone in ("refresh_once", "refresh_source", "preview_source", "list_tags"):
        assert not hasattr(instance, gone)


def test_the_queue_runs_a_deferred_pool_check_step_through_the_library_checks(worker_storage):
    queue = ProcrastinateMediaQueue(worker_storage.db.dsn)
    queue.apply_schema(worker_storage.db.dsn)
    instance = worker(worker_storage, FakeLibrary(worker_storage.clock))

    class Checks:
        def __init__(self):
            self.calls = []

        async def check_pool(self, pool_key):
            self.calls.append(("pool", pool_key))

        async def contact(self, connection_ref):
            self.calls.append(("connection", connection_ref))

    checks = Checks()

    async def exercise():
        app = create_worker_app(worker_storage.db.dsn)
        async with app.open_async():
            checks_queue = ProcrastinateCheckQueue(app)
            await checks_queue.pool("a" * 64)
            await checks_queue.pool("a" * 64)       # an already queued step coalesces
            await checks_queue.connection("main")
            await app.run_worker_async(queues=[MEDIA_QUEUE], concurrency=1, wait=False,
                                       additional_context={"media_worker": instance,
                                                           "library_checks": checks})
        await close(instance)
    asyncio.run(exercise())
    assert sorted(checks.calls) == [("connection", "main"), ("pool", "a" * 64)]


def test_a_pool_photo_is_fetched_for_the_largest_frame_prepared_and_published(worker_storage):
    queued(worker_storage)
    source = FakeLibrary(worker_storage.clock)
    preparer = FakePreparer()
    instance = worker(worker_storage, source, preparer)

    async def exercise():
        try:
            with worker_storage.worker_lock():
                await instance.process_job("job-1", attempt=1)
        finally:
            await close(instance)
    asyncio.run(exercise())
    assert source.edges == [2160]
    assert isinstance(preparer.prepared[0], LibraryAsset)
    assert row(worker_storage, "media_jobs", "id", "job-1")["state"] == "ready"


def test_a_new_key_or_address_gets_a_new_client(worker_storage):
    made = []
    instance = MediaWorker(worker_storage.repository, worker_storage,
                           library_store(worker_storage.clock), preparer=FakePreparer(),
                           client_factory=lambda secret: made.append(secret) or FakeLibrary(
                               worker_storage.clock))
    asyncio.run(instance._client("fixture"))
    asyncio.run(instance._client("fixture"))
    instance.library.connection("fixture").key = "another-synthetic-key"
    asyncio.run(instance._client("fixture"))
    instance.library.connection("fixture").url = "https://other.invalid/api"
    asyncio.run(instance._client("fixture"))
    assert [(secret.base_url, secret.api_key.get_secret_value()) for secret in made][1:] == [
        (made[0].base_url, "another-synthetic-key"),
        ("https://other.invalid/api", "another-synthetic-key")]   # a new key, then address
    with pytest.raises(MediaError, match="connection_unknown"):
        asyncio.run(instance._client("removed"))
