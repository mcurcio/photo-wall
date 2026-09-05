"""Worker orchestration with private generated configuration and real PG/local bytes."""

import asyncio
import hashlib
import json
import os
import threading
from uuid import UUID

import pytest
from test_media_store import ORIGINAL, RECIPE, VARIANT, grant, queued, ready, row

from central.catalog import CatalogSnapshot
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
from media.worker import MediaWorker, WorkerLimits, load_connections

SECRET = "PUBLIC_SYNTHETIC_PRIVATE_KEY"


def configuration():
    return dict(connection_id="fixture", base_url="https://immich.invalid/api", owner_id=str(UUID(int=1)), api_key=SECRET)


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


@pytest.mark.parametrize("fault", ["mode", "symlink", "owner", "oversize", "empty", "json", "duplicate_key",
                                  "duplicate_id", "schema", "unknown", "bad_secret", "nonfinite", "fifo"])
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
        path.write_text(json.dumps({"schema": 1, "connections": [{**configuration(), "api_key": "x\n" + SECRET}]}))
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
        self.entered = asyncio.Event()
        self.cancelled = False

    async def refresh(self, spec):
        self.refreshes += 1
        return RefreshResult(snapshot=CatalogSnapshot(source_ref=spec.source_ref, refreshed_at=self.clock.utc(),
            candidates=tuple(a.candidate for a in self.assets)), assets=self.assets)

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
            sha1=hashlib.sha1(ORIGINAL).hexdigest(), sha256=hashlib.sha256(ORIGINAL).hexdigest())

    async def close(self):
        self.closed = True


class FakePreparer:
    def __init__(self):
        from contracts.models import Variant
        self.variant = Variant(sha256=hashlib.sha256(VARIANT).hexdigest(), size=len(VARIANT),
                               media_type="image/jpeg", width=1080, height=1920)
        self.build = BuildIdentity(preparation_sha256="a" * 64, ffmpeg_sha256="b" * 64, ffprobe_sha256="c" * 64,
            ffmpeg_version="synthetic", ffprobe_version="synthetic", python_version="synthetic",
            pillow_version="synthetic", littlecms_version="synthetic", jpeg_version="synthetic",
            zlib_version="synthetic", platform="synthetic", memory_limit_enforced=False)
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
        return PreparedMedia(path=destination, variant=self.variant, recipe_id=self.recipe, build=self.build,
                              original_sha256=hashlib.sha256(source.read_bytes()).hexdigest())


@pytest.fixture
def worker_storage(registry, tmp_path):
    repository = MediaRepository(registry.db, registry.clock, StoreLimits(
        max_bytes=10000, max_original_bytes=1000, max_image_bytes=1000, max_video_bytes=2000))
    repository.set_recipe(RECIPE)
    return MediaStore(repository, tmp_path / "media")


def worker(storage, source, preparer=None, **kwargs):
    return MediaWorker(storage.repository, storage, {"fixture": ConnectionConfig(**configuration())},
                       preparer=preparer or FakePreparer(), client_factory=lambda _: source, **kwargs)


def configure_source(storage):
    storage.repository.configure_source(SourceSpec(source_ref="library:1", connection_ref="fixture"))


def test_refresh_discovers_assets_without_enqueuing_any_job(worker_storage):
    configure_source(worker_storage)
    source = FakeSource(worker_storage.clock, (original(),))
    report = asyncio.run(worker(worker_storage, source).run(max_cycles=1))
    assert report.refreshes == 1 and report.jobs_ready == 0
    with worker_storage.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM asset_revisions").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM media_jobs").fetchone()["n"] == 0
    assert source.closed


def test_requested_job_downloads_prepares_and_publishes_real_local_bytes(worker_storage):
    asset = queued(worker_storage)
    configure_source(worker_storage)
    source = FakeSource(worker_storage.clock, (asset,))
    report = asyncio.run(worker(worker_storage, source).run(max_cycles=1))
    assert (report.cycles, report.refreshes, report.jobs_ready, report.jobs_failed) == (1, 1, 1, 0)
    assert row(worker_storage, "media_jobs", "id", "job-1")["state"] == "ready"
    assert (worker_storage.root / "blobs" / hashlib.sha256(VARIANT).hexdigest()).read_bytes() == VARIANT
    assert worker_storage.repository.health()["accounted_bytes"] == len(VARIANT)
    assert SECRET not in report.model_dump_json() and source.closed


@pytest.mark.parametrize("failure,state", [
    (MediaError("unsupported_color", "incompatible"), "failed"),
    (MediaError("asset_integrity"), "failed"), (MediaError("asset_oversize", "incompatible"), "failed"),
    (MediaError("asset_unavailable"), "retry"), (MediaError("asset_missing"), "retry"),
    (MediaError("unsupported_version", "incompatible"), "retry"),
    (MediaError("upstream_schema", "incompatible"), "retry"),
    (MediaError("upstream_permission", "permission"), "retry"), (RuntimeError(SECRET), "retry"),
])
def test_failures_use_permanent_or_bounded_retry_policy_without_leaking(worker_storage, failure, state):
    queued(worker_storage)
    source = FakeSource(worker_storage.clock)
    source.fault = failure
    report = asyncio.run(worker(worker_storage, source).run(max_cycles=1))
    job = row(worker_storage, "media_jobs", "id", "job-1")
    assert job["state"] == state and job["reserved_bytes"] == 0
    assert job["retry_at"] == (1005 if state == "retry" else 0)
    assert report.jobs_failed == 1 and source.closed
    assert SECRET not in report.model_dump_json()
    assert not list((worker_storage.root / "staging").glob("*/*/*"))


def test_retry_attempts_are_persisted_and_later_service_recovery_succeeds(worker_storage):
    queued(worker_storage)
    source = FakeSource(worker_storage.clock)
    source.fault = MediaError("upstream_unavailable")
    instance = worker(worker_storage, source)
    for attempt, delay in enumerate((5, 15, 60), start=1):
        report = asyncio.run(instance.run(max_cycles=1))
        job = row(worker_storage, "media_jobs", "id", "job-1")
        assert report.jobs_failed == 1 and job["attempt"] == attempt
        assert job["retry_at"] == worker_storage.clock.utc() + delay
        worker_storage.clock.advance(delay)
    source.fault = None
    assert asyncio.run(instance.run(max_cycles=1)).jobs_ready == 1
    assert worker_storage.repository.health()["worker_error"] is None


def test_unknown_connection_is_coded_without_echoing_private_identifier(worker_storage):
    worker_storage.repository.configure_source(SourceSpec(source_ref="library:1", connection_ref="private-unknown-id"))
    report = asyncio.run(worker(worker_storage, FakeSource(worker_storage.clock)).run(max_cycles=1))
    assert report.last_error == "connection_unknown"
    assert "private-unknown-id" not in report.model_dump_json()
    source = worker_storage.repository.sources()[0]
    assert source["status"] == "unavailable" and source["diagnostics"] == [{"code": "connection_unknown", "asset_id": None}]


async def wait_until(predicate, seconds=5):
    async with asyncio.timeout(seconds):
        while not await asyncio.to_thread(predicate):
            await asyncio.sleep(.01)


def test_refresh_and_heartbeat_continue_during_held_download(worker_storage):
    asset = queued(worker_storage)
    configure_source(worker_storage)
    source = FakeSource(worker_storage.clock, (asset,))
    async def exercise():
        source.gate = asyncio.Event()
        instance = worker(worker_storage, source, limits=WorkerLimits(poll_seconds=.02, heartbeat_seconds=.02))
        task = asyncio.create_task(instance.run(max_cycles=1))
        try:
            await asyncio.wait_for(source.entered.wait(), 5)
            worker_storage.clock.advance(31)
            await wait_until(lambda: source.refreshes >= 2)
            await wait_until(lambda: worker_storage.repository.health()["worker_seen"] == 1031)
            assert not task.done()
            source.gate.set()
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    report = asyncio.run(exercise())
    assert report.refreshes >= 2 and report.jobs_ready == 1


@pytest.mark.parametrize("stage", ["download", "prepare"])
def test_cancellation_reaps_injected_activity_cleans_bytes_and_releases_writer(worker_storage, stage):
    queued(worker_storage)
    source, preparation = FakeSource(worker_storage.clock), FakePreparer()
    blocked = source if stage == "download" else preparation
    async def exercise():
        blocked.gate = asyncio.Event()
        task = asyncio.create_task(worker(worker_storage, source, preparation).run())
        try:
            await asyncio.wait_for(blocked.entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())
    assert blocked.cancelled and source.closed
    assert row(worker_storage, "media_jobs", "id", "job-1")["state"] == "retry"
    assert worker_storage.repository.health()["accounted_bytes"] == 0
    with MediaStore(worker_storage.repository, worker_storage.root).worker_lock():
        pass


def test_recipe_change_between_claim_and_preparation_never_publishes(worker_storage):
    queued(worker_storage)
    preparation = FakePreparer()
    preparation.recipe = "f" * 64
    report = asyncio.run(worker(worker_storage, FakeSource(worker_storage.clock), preparation).run(max_cycles=1))
    assert report.last_error == "recipe_changed"
    assert row(worker_storage, "media_jobs", "id", "job-1")["state"] == "retry"
    assert worker_storage.repository.health()["accounted_bytes"] == 0


def test_pressure_collects_unused_cache_then_processes_current_request(worker_storage):
    with worker_storage.worker_lock():
        ready(worker_storage)
    queued(worker_storage, 2)
    worker_storage.set_quota(2010)
    report = asyncio.run(worker(worker_storage, FakeSource(worker_storage.clock)).run(max_cycles=1))
    assert report.jobs_ready == 1
    assert row(worker_storage, "media_jobs", "id", "job-2")["state"] == "ready"
    evicted = row(worker_storage, "media_jobs", "id", "job-1")
    assert evicted["state"] == "evicted" and evicted["variant_sha"] is None
    assert evicted["result"] is not None


def test_pressure_cannot_collect_secured_content_or_allocate_more_bytes(worker_storage, registry):
    with worker_storage.worker_lock():
        _, _, prepared = ready(worker_storage)
        grant(worker_storage, registry, prepared.variant, secured=True)
    queued(worker_storage, 2)
    worker_storage.set_quota(2010)
    source = FakeSource(worker_storage.clock)
    report = asyncio.run(worker(worker_storage, source).run(max_cycles=1))
    assert report.pressure == 1 and report.jobs_ready == 0 and source.downloads == 0
    assert row(worker_storage, "media_jobs", "id", "job-2")["state"] == "queued"
    assert worker_storage.repository.health()["accounted_bytes"] == len(VARIANT)


def test_stop_event_finishes_with_cleanup_report(worker_storage):
    async def exercise():
        stop = asyncio.Event()
        stop.set()
        return await worker(worker_storage, FakeSource(worker_storage.clock)).run(stop=stop)
    report = asyncio.run(exercise())
    assert report.stopped


def test_evicted_job_only_requeues_on_new_request_and_obeys_active_capacity(worker_storage):
    with worker_storage.worker_lock():
        lease, _, _ = ready(worker_storage)
        worker_storage.collect(target_bytes=0)
    queued(worker_storage, 2)
    worker_storage.repository.limits = worker_storage.repository.limits.model_copy(update={"max_jobs": 1})
    request = AcquisitionRequest(asset_id=lease.asset.asset_id, assignment_ids=("new-assignment",), earliest_start=1100)
    with pytest.raises(RegistryError, match="job_capacity"):
        worker_storage.repository.request_acquisitions((request,))
    assert row(worker_storage, "media_jobs", "id", "job-1")["state"] == "evicted"
    with worker_storage.repository.transaction() as conn:
        conn.execute("UPDATE media_jobs SET state='failed' WHERE id='job-2'")
    assert worker_storage.repository.request_acquisitions((request,)) == 1
    result = row(worker_storage, "media_jobs", "id", "job-1")
    assert result["state"] == "queued" and result["earliest_start"] == 1100
    assert result["result"] is not None and result["variant_sha"] is None


def test_cancellation_during_claim_waits_then_cleans_the_reserved_attempt(worker_storage, monkeypatch):
    queued(worker_storage)
    claimed, release = threading.Event(), threading.Event()
    real_claim = worker_storage.repository.claim_job
    def delayed_claim():
        lease = real_claim()
        claimed.set()
        assert release.wait(5)
        return lease
    monkeypatch.setattr(worker_storage.repository, "claim_job", delayed_claim)
    async def exercise():
        task = asyncio.create_task(worker(worker_storage, FakeSource(worker_storage.clock)).run())
        try:
            assert await asyncio.to_thread(claimed.wait, 5)
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done()
            with pytest.raises(RegistryError, match="media_writer_active"):
                with MediaStore(worker_storage.repository, worker_storage.root).worker_lock():
                    pytest.fail("writer lock released beneath the active claim")
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())
    assert row(worker_storage, "media_jobs", "id", "job-1")["state"] == "retry"
    assert worker_storage.repository.health()["accounted_bytes"] == 0


def test_stale_failure_cleanup_cannot_overwrite_a_newer_attempt(worker_storage):
    queued(worker_storage)
    source = FakeSource(worker_storage.clock)
    async def exercise():
        source.gate = asyncio.Event()
        task = asyncio.create_task(worker(worker_storage, source).run())
        try:
            await asyncio.wait_for(source.entered.wait(), 5)
            with worker_storage.repository.transaction() as conn:
                conn.execute("UPDATE media_jobs SET attempt_token='replacement' WHERE id='job-1'")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())
    job = row(worker_storage, "media_jobs", "id", "job-1")
    assert job["attempt_token"] == "replacement" and job["state"] == "running"
    assert job["reserved_bytes"] == 2000


def test_outer_job_deadline_cancels_partial_download_and_keeps_retry_bounded(worker_storage):
    queued(worker_storage)
    source = FakeSource(worker_storage.clock)
    async def exercise():
        source.gate = asyncio.Event()
        return await worker(worker_storage, source, limits=WorkerLimits(job_seconds=.2)).run(max_cycles=1)
    report = asyncio.run(exercise())
    assert report.last_error == "worker_timeout" and report.jobs_failed == 1
    assert source.cancelled and source.closed
    assert row(worker_storage, "media_jobs", "id", "job-1")["retry_at"] == 1005
    assert worker_storage.repository.health()["accounted_bytes"] == 0


def test_nonfinite_clock_cannot_write_invalid_worker_heartbeat(worker_storage):
    worker_storage.clock.wall = float("nan")
    with pytest.raises(MediaError, match="clock_invalid"):
        asyncio.run(worker(worker_storage, FakeSource(worker_storage.clock)).run(max_cycles=1))
    with worker_storage.db.transaction() as conn:
        assert conn.execute("SELECT worker_seen FROM media_settings WHERE singleton").fetchone()["worker_seen"] == 1000


def test_hung_client_close_is_bounded_and_does_not_hold_writer_lock(worker_storage):
    configure_source(worker_storage)
    source = FakeSource(worker_storage.clock)
    async def hang():
        await asyncio.Event().wait()
    source.close = hang
    report = asyncio.run(worker(worker_storage, source, limits=WorkerLimits(close_seconds=.05)).run(max_cycles=1))
    assert report.last_error == "connection_close"
    with MediaStore(worker_storage.repository, worker_storage.root).worker_lock():
        pass


def test_command_startup_error_contains_no_environment_or_raw_exception(monkeypatch, capsys):
    import media.worker as module
    monkeypatch.setattr(module.sys, "argv", ["media.worker", "--once"])
    monkeypatch.delenv("PHOTO_WALL_DATABASE_URL", raising=False)
    assert module.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.err) == {"error": "worker_config"}
    assert not output.out
