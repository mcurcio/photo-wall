"""Procrastinate media task execution with private generated configuration."""

import asyncio
import hashlib
import json
import os
from uuid import UUID

import pytest
from media_queue import RecordingMediaQueue
from test_media_store import ORIGINAL, RECIPE, VARIANT, grant, queued, ready, row

from central.catalog import CatalogSnapshot
from central.media_queue import MEDIA_QUEUE, ProcrastinateMediaQueue
from central.media_repository import MediaRepository, StoreLimits
from central.media_store import MediaStore
from central.planner import AcquisitionRequest
from central.registry import RegistryError
from media.models import (
    ConnectionConfig,
    DownloadedOriginal,
    MediaError,
    OriginalAsset,
    RefreshResult,
    SourceSpec,
)
from media.prepare import BuildIdentity, PreparedMedia
from media.task_queue import MediaTaskFailed, RetryableMediaTask, create_worker_app
from media.worker import MediaWorker, WorkerLimits, load_connections

SECRET = "PUBLIC_SYNTHETIC_PRIVATE_KEY"


def configuration():
    return dict(connection_id="fixture", base_url="https://immich.invalid/api",
                owner_id=str(UUID(int=1)), api_key=SECRET)


def private_file(tmp_path, document=None):
    path = tmp_path / "connections.json"
    path.write_text(json.dumps(document or {"schema": 1, "connections": [configuration()]}))
    path.chmod(0o600)
    return path


def test_private_connection_file_keeps_keys_out_of_models_and_repr(tmp_path):
    connections = load_connections(private_file(tmp_path))
    assert connections["fixture"].api_key.get_secret_value() == SECRET
    assert SECRET not in repr(connections)
    assert SECRET not in connections["fixture"].model_dump_json()


@pytest.mark.parametrize("fault", [
    "mode", "symlink", "owner", "oversize", "empty", "json", "duplicate_key",
    "duplicate_id", "schema", "unknown", "bad_secret", "nonfinite", "fifo",
])
def test_private_connection_failures_return_only_fixed_code(tmp_path, monkeypatch, fault):
    path = private_file(tmp_path)
    if fault == "mode":
        path.chmod(0o644)
    elif fault == "symlink":
        target = tmp_path / "linked"
        target.symlink_to(path)
        path = target
    elif fault == "owner":
        monkeypatch.setattr(os, "geteuid", lambda: path.stat().st_uid + 1)
    elif fault == "oversize":
        path.write_bytes(b"x" * (1024**2 + 1))
    elif fault == "empty":
        path.write_bytes(b"")
    elif fault == "json":
        path.write_text(SECRET)
    elif fault == "duplicate_key":
        path.write_text('{"schema":1,"schema":1,"connections":[]}')
    elif fault == "duplicate_id":
        path.write_text(json.dumps({"schema": 1, "connections": [configuration(), configuration()]}))
    elif fault == "schema":
        path.write_text('{"schema":2,"connections":[]}')
    elif fault == "unknown":
        path.write_text(json.dumps({"schema": 1, "connections": [], "private": SECRET}))
    elif fault == "bad_secret":
        path.write_text(json.dumps({"schema": 1, "connections": [
            {**configuration(), "api_key": "x\n" + SECRET},
        ]}))
    elif fault == "nonfinite":
        path.write_text('{"schema":NaN,"connections":[]}')
    else:
        path.unlink()
        os.mkfifo(path, 0o600)
    with pytest.raises(MediaError) as caught:
        load_connections(path)
    assert caught.value.code == "connection_file"
    assert SECRET not in str(caught.value)


def original():
    return OriginalAsset(connection_id="fixture", upstream_id=str(UUID(int=1)),
        original_sha1=hashlib.sha1(ORIGINAL).hexdigest(), kind="image", raw_width=1080,
        raw_height=1920, orientation=1, captured_at=1000, file_size=len(ORIGINAL))


class FakeSource:
    def __init__(self, clock, assets=()):
        self.clock, self.assets = clock, assets
        self.refreshes, self.downloads, self.closed = 0, 0, False
        self.fault = None
        self.gate = None
        self.refresh_gate = None
        self.refresh_entered = asyncio.Event()
        self.entered = asyncio.Event()
        self.cancelled = False

    async def refresh(self, spec):
        self.refreshes += 1
        self.refresh_entered.set()
        if self.refresh_gate:
            await self.refresh_gate.wait()
        return RefreshResult(snapshot=CatalogSnapshot(source_ref=spec.source_ref,
            refreshed_at=self.clock.utc(), candidates=tuple(a.candidate for a in self.assets)),
            assets=self.assets)

    async def download_original(self, asset, destination):
        self.downloads += 1
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
        return DownloadedOriginal(path=destination, size=len(ORIGINAL),
            sha1=hashlib.sha1(ORIGINAL).hexdigest(),
            sha256=hashlib.sha256(ORIGINAL).hexdigest())

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

    async def describe_recipe(self):
        return RECIPE, self.build

    async def prepare(self, asset, source, destination):
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
        max_video_bytes=2000), queue=RecordingMediaQueue())
    repository.set_recipe(RECIPE)
    return MediaStore(repository, tmp_path / "media")


def worker(storage, source, preparer=None, **kwargs):
    return MediaWorker(storage.repository, storage,
        {"fixture": ConnectionConfig(**configuration())}, preparer=preparer or FakePreparer(),
        client_factory=lambda _: source, **kwargs)


def configure_source(storage):
    storage.repository.configure_source(SourceSpec(source_ref="library:1", connection_ref="fixture"))


async def close(instance):
    await instance._close_clients()


def test_periodic_refresh_task_discovers_assets_without_enqueuing_job(worker_storage):
    configure_source(worker_storage)
    source = FakeSource(worker_storage.clock, (original(),))
    instance = worker(worker_storage, source)
    async def exercise():
        try:
            assert await instance.refresh_once()
        finally:
            await close(instance)
    asyncio.run(exercise())
    with worker_storage.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM asset_revisions").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM media_jobs").fetchone()["n"] == 0
    assert source.closed


def test_source_task_catches_up_request_arriving_during_active_refresh(worker_storage):
    configure_source(worker_storage)
    source = FakeSource(worker_storage.clock, (original(),))
    source.refresh_gate = asyncio.Event()
    instance = worker(worker_storage, source)
    first = worker_storage.repository.request_refresh("library:1")

    async def exercise():
        try:
            task = asyncio.create_task(instance.refresh_source("library:1"))
            await source.refresh_entered.wait()
            second = await asyncio.to_thread(
                worker_storage.repository.request_refresh, "library:1"
            )
            source.refresh_gate.set()
            await task
            return second
        finally:
            await close(instance)

    second = asyncio.run(exercise())
    assert (first.requested_revision, second.requested_revision) == (1, 2)
    assert worker_storage.repository.refresh_revisions("library:1") == (2, 2)
    assert source.refreshes == 2


def test_scheduled_refresh_catches_up_request_arriving_during_upstream_io(worker_storage):
    configure_source(worker_storage)
    source = FakeSource(worker_storage.clock, (original(),))
    source.refresh_gate = asyncio.Event()
    instance = worker(worker_storage, source)

    async def exercise():
        try:
            task = asyncio.create_task(instance.refresh_once())
            await source.refresh_entered.wait()
            receipt = await asyncio.to_thread(
                worker_storage.repository.request_refresh, "library:1"
            )
            source.refresh_gate.set()
            assert await task
            return receipt
        finally:
            await close(instance)

    receipt = asyncio.run(exercise())
    assert receipt.requested_revision == 1
    assert worker_storage.repository.refresh_revisions("library:1") == (1, 1)
    assert source.refreshes == 2


def test_scheduled_catch_up_defers_to_competing_exact_refresh_lease(
    worker_storage, monkeypatch
):
    configure_source(worker_storage)
    source = FakeSource(worker_storage.clock, (original(),))
    source.refresh_gate = asyncio.Event()
    instance = worker(worker_storage, source)
    competing = []
    revisions = worker_storage.repository.refresh_revisions

    def claim_exact_before_read(source_ref):
        if not competing:
            competing.append(worker_storage.repository.begin_requested_refresh(source_ref))
        return revisions(source_ref)

    monkeypatch.setattr(
        worker_storage.repository, "refresh_revisions", claim_exact_before_read
    )

    async def exercise():
        try:
            task = asyncio.create_task(instance.refresh_once())
            await source.refresh_entered.wait()
            await asyncio.to_thread(
                worker_storage.repository.request_refresh, "library:1"
            )
            source.refresh_gate.set()
            return await task
        finally:
            await close(instance)

    assert asyncio.run(exercise())
    assert competing[0] is not None and competing[0].request_revision == 1
    assert revisions("library:1") == (1, 0)
    assert worker_storage.repository.publish_refresh(
        competing[0],
        RefreshResult(snapshot=CatalogSnapshot(
            source_ref="library:1",
            refreshed_at=worker_storage.clock.utc(),
            candidates=(original().candidate,),
        ), assets=(original(),)),
    )
    assert revisions("library:1") == (1, 1)


def test_procrastinate_task_processes_only_exact_job_and_publishes(worker_storage):
    queued(worker_storage)
    queued(worker_storage, 2)
    source = FakeSource(worker_storage.clock)
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
    source = FakeSource(worker_storage.clock)
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
    instance = worker(worker_storage, FakeSource(worker_storage.clock))

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


def test_procrastinate_worker_consumes_exact_deferred_source_refresh(worker_storage):
    configure_source(worker_storage)
    queue = ProcrastinateMediaQueue(worker_storage.db.dsn)
    queue.apply_schema(worker_storage.db.dsn)
    worker_storage.repository.queue = queue
    receipt = worker_storage.repository.request_refresh("library:1")
    source = FakeSource(worker_storage.clock, (original(),))
    instance = worker(worker_storage, source)

    async def exercise():
        app = create_worker_app(worker_storage.db.dsn)
        try:
            with worker_storage.worker_lock():
                async with app.open_async():
                    await app.run_worker_async(
                        queues=[MEDIA_QUEUE],
                        concurrency=1,
                        wait=False,
                        additional_context={"media_worker": instance},
                    )
        finally:
            await close(instance)

    asyncio.run(exercise())
    assert receipt.requested_revision == 1
    assert worker_storage.repository.refresh_revisions("library:1") == (1, 1)
    assert source.refreshes == 1
    with worker_storage.db.transaction() as conn:
        job = conn.execute(
            "SELECT args,status FROM procrastinate_jobs "
            "WHERE task_name='photo_wall.media.refresh_source'"
        ).fetchone()
        members = conn.execute("SELECT count(*) AS n FROM source_members").fetchone()["n"]
    assert job == {"args": {"source_ref": "library:1"}, "status": "succeeded"}
    assert members == 1


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
    source = FakeSource(worker_storage.clock)
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
    source = FakeSource(worker_storage.clock)
    source.fault = MediaError("upstream_unavailable")
    instance = worker(worker_storage, source)
    with worker_storage.worker_lock(), pytest.raises(MediaTaskFailed):
        asyncio.run(instance.process_job("job-1", attempt=4))
    assert row(worker_storage, "media_jobs", "id", "job-1")["state"] == "failed"


def test_cancellation_cleans_partial_bytes_and_keeps_attempt_fence(worker_storage):
    queued(worker_storage)
    source = FakeSource(worker_storage.clock)
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
    instance = worker(worker_storage, FakeSource(worker_storage.clock), preparation)
    with worker_storage.worker_lock(), pytest.raises(RetryableMediaTask, match="recipe_changed"):
        asyncio.run(instance.process_job("job-1", attempt=1))
    assert row(worker_storage, "media_jobs", "id", "job-1")["state"] == "retry"
    assert worker_storage.repository.health()["accounted_bytes"] == 0


def test_pressure_collects_unused_content_then_processes_request(worker_storage):
    with worker_storage.worker_lock():
        ready(worker_storage)
    queued(worker_storage, 2)
    worker_storage.set_quota(2010)
    instance = worker(worker_storage, FakeSource(worker_storage.clock))
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
    source = FakeSource(worker_storage.clock)
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
    source = FakeSource(worker_storage.clock)
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
    source = FakeSource(worker_storage.clock)
    source.gate = asyncio.Event()
    instance = worker(worker_storage, source, limits=WorkerLimits(job_seconds=.2))
    with worker_storage.worker_lock(), pytest.raises(RetryableMediaTask, match="worker_timeout"):
        asyncio.run(instance.process_job("job-1", attempt=1))
    assert source.cancelled
    job = row(worker_storage, "media_jobs", "id", "job-1")
    assert job["state"] == "retry" and job["retry_at"] == 0
    assert worker_storage.repository.health()["accounted_bytes"] == 0


def test_nonfinite_clock_cannot_start_task_activity(worker_storage):
    instance = worker(worker_storage, FakeSource(worker_storage.clock))
    worker_storage.clock.wall = float("nan")
    with pytest.raises(MediaError, match="clock_invalid"):
        asyncio.run(instance.process_job("missing", attempt=1))
    with worker_storage.db.transaction() as conn:
        assert conn.execute("SELECT worker_seen FROM media_settings WHERE singleton").fetchone()["worker_seen"] == 1000


def test_hung_client_close_is_bounded(worker_storage):
    source = FakeSource(worker_storage.clock)
    instance = worker(worker_storage, source, limits=WorkerLimits(close_seconds=.05))
    instance._client("fixture")
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
