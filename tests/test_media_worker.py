"""The media worker's media maker (`PrepareMedia`), measurements, maintenance and process entry
against real PostgreSQL, with fakes of the library client and the preparer."""

import asyncio
import hashlib
import inspect
import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from media_queue import RecordingMediaQueue
from PIL import Image
from public_media import MediaCache, request_photo
from support.frame_media import a_frame
from support.library_store_fake import FakeLibraryStore, RevisionRecord
from support.photo_pools import photo, seed_pool

from central.assets.layout import CacheLayout
from central.assets.store import CacheStore
from central.db import Database, ProcessTransactionClock
from central.infra.key_content_store import PgKeyContent
from central.infra.transactions import PgTransactions
from central.installation_models import FrameOutput
from central.kernel.assets import MediaReady
from central.kernel.handling import TerminalFailure, TransientFailure
from central.kernel.job_types import PrepareMedia
from central.media_queue import MEDIA_QUEUE, ProcrastinateMediaQueue
from central.media_repository import MediaRepository, StoreLimits
from central.photo_library.model import LibraryAsset, MeasuredCopy, Unreachable
from contracts.models import FrameProfile
from contracts.time import Clock
from media.library_client import FaceBox, FetchedRendition, LibraryUnavailable
from media.models import MediaError
from media.prepare import BuildIdentity, PreparationRecipe, PreparedMedia
from media.task_queue import (
    ProcrastinateCheckQueue,
    RetryableMediaTask,
    create_worker_app,
)
from media.worker import MediaWorker, WorkerLimits, _media_loop

SECRET = "PUBLIC_SYNTHETIC_PRIVATE_KEY"
PORTRAIT = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)
PREVIEW = MeasuredCopy(name="preview", media_type="image/jpeg", width=810, height=1440)
ORIGINAL = b"public synthetic original bytes"
VARIANT = b"public synthetic prepared bytes"


@dataclass(frozen=True)
class WorkerStorage:
    repository: MediaRepository
    db: Database
    clock: Clock
    root: Path


def test_no_environment_variable_names_a_connections_file(worker_storage):
    """Roadmap 4: connections and keys are the store's; the worker reads no file and reports no
    connection list (the photo library's own view replaces it)."""
    import media.worker as module
    source = inspect.getsource(module)
    assert "PHOTO_WALL_CONNECTIONS_FILE" not in source and "load_connections" not in source
    instance = worker(worker_storage, FakeLibrary(worker_storage.clock))
    asyncio.run(instance.maintain())
    assert "connection_ids" not in worker_storage.repository.health()


class FakeLibrary:
    """The connection's LibraryClient: fetches write the fetched rendition's bytes."""

    def __init__(self, clock):
        self.clock = clock
        self.downloads, self.closed = 0, False
        self.fetches = []   # (plan, needs) of each fetch
        self.fault = None
        self.gate = None
        self.entered = asyncio.Event()
        self.cancelled = False

    async def fetch_rendition(self, asset, *, plan, needs, destination):
        self.downloads += 1
        self.fetches.append((plan, needs))
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
        return FetchedRendition(path=destination, rendition="original", media_type="image/jpeg",
                                size=len(ORIGINAL), sha256=hashlib.sha256(ORIGINAL).hexdigest(),
                                width=1080, height=1920, measured=(PREVIEW,))

    async def close(self):
        self.closed = True


class FakePreparer:
    def __init__(self):
        self.build = BuildIdentity(preparation_sha256="a" * 64, ffmpeg_sha256="b" * 64,
            ffprobe_sha256="c" * 64, ffmpeg_version="synthetic", ffprobe_version="synthetic",
            python_version="synthetic", pillow_version="synthetic", littlecms_version="synthetic",
            libheif_version="synthetic",
            jpeg_version="synthetic", zlib_version="synthetic", platform="synthetic",
            memory_limit_enforced=False)
        self.fault = None
        self.prepared = []
        self.recipes = []

    async def prepare_rendition(self, asset, rendition, destination, *, recipe=None):
        self.prepared.append(asset)
        self.recipes.append(recipe)
        source = rendition.path
        destination.write_bytes(VARIANT)
        if self.fault:
            raise self.fault
        return PreparedMedia(path=destination, build=self.build,
            original_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            ready=MediaReady(sha256=hashlib.sha256(VARIANT).hexdigest(), size=len(VARIANT),
                             media_type="image/jpeg", width=1080, height=1920, duration=None,
                             made_from=rendition.rendition))


@pytest.fixture
def worker_storage(registry, tmp_path):
    repository = MediaRepository(registry.db, registry.clock, StoreLimits(
        max_bytes=10000, max_original_bytes=1000, max_image_bytes=1000,
        max_video_bytes=2000), queue=RecordingMediaQueue(),
        times=ProcessTransactionClock(registry.clock))
    return WorkerStorage(repository, registry.db, registry.clock, tmp_path)


def library_store(clock):
    library = FakeLibraryStore(clock)
    library.add_connection("fixture", "https://immich.invalid/api", SECRET)
    library.outputs = (
        FrameOutput(frame_id="hall", profile=PORTRAIT, output_px=(1080, 1920),
                    display_px=(1080, 1920)),
        FrameOutput(frame_id="lounge", profile=PORTRAIT, output_px=None, display_px=None))
    return library


def worker(storage, source, preparer=None, **kwargs):
    return MediaWorker(storage.repository, library_store(storage.clock),
                       preparer=preparer or FakePreparer(),
                       client_factory=lambda _: source, **kwargs)


async def close(instance):
    await instance._close_clients()


# The media maker: one PrepareMedia, made from the variant's name ----------------------------------


def planned(storage, registry):
    """A Frame (the wall's need), one pool photo, and its variant planned (record and job)."""
    a_frame(registry)
    picture = photo("photo-1", taken_at=900)
    seed_pool(registry, "source:1", (picture,))
    return picture, request_photo(MediaCache(storage.repository, storage.root), picture.asset_id)


def maker(storage, instance):
    return instance.media_maker(store=CacheStore(CacheLayout(storage.root)),
                                transactions=PgTransactions(storage.db))


def test_a_planned_photo_is_fetched_for_its_recipes_need_prepared_and_recorded(
        worker_storage, registry):
    """The fetch holds the variant's need (the Frame's output), the measured copies are
    recorded, and the facts name the copy it was made from; the file lands at its digest."""
    picture, key = planned(worker_storage, registry)
    source = FakeLibrary(worker_storage.clock)
    preparer = FakePreparer()
    instance = worker(worker_storage, source, preparer)
    # The worker's store knows the revision (the PostgreSQL pool seeded above is the variant's).
    instance.library.revisions[picture.asset_id] = RevisionRecord(
        picture.connection_ref, picture.upstream_id, picture.revision, picture.width,
        picture.height, None, None, 0.0)

    async def exercise():
        try:
            return await maker(worker_storage, instance).handle(PrepareMedia(key=key))
        finally:
            await close(instance)
    facts = asyncio.run(exercise())
    [(plan, needs)] = source.fetches
    assert [(need.width, need.height) for need in needs] == [(1080, 1920)]
    assert plan == ("preview", "original")   # a JPEG original (the copy ladder)
    assert isinstance(preparer.prepared[0], LibraryAsset)
    # One copy for the wall (follow-up S1): the 1080 x 1920 Frame's need is the 1920 step.
    assert preparer.recipes == [PreparationRecipe(image_long_edge=1920)]
    assert [(copy.name, copy.width) for copy in
            instance.library.copy_sizes(picture.asset_id).values()] == [("preview", 810)]
    assert (facts.sha256, facts.size, facts.made_from) == (
        hashlib.sha256(VARIANT).hexdigest(), len(VARIANT), "original")
    blobs = worker_storage.root / "media" / "blobs"
    assert [path.name for path in blobs.iterdir()] == [facts.sha256]  # no temp left behind


@pytest.mark.parametrize(("fault", "failure", "reason"), [
    (MediaError("unsupported_media", "incompatible"), TerminalFailure, "unsupported_media"),
    (MediaError("upstream_unavailable"), TransientFailure, "upstream_unavailable"),
    (LibraryUnavailable(Unreachable()), TransientFailure, "unreachable"),
])
def test_failures_are_typed_and_leave_no_bytes(worker_storage, registry, fault, failure, reason):
    _, key = planned(worker_storage, registry)
    source = FakeLibrary(worker_storage.clock)
    source.fault = fault
    instance = worker(worker_storage, source)
    with pytest.raises(failure) as raised:
        asyncio.run(maker(worker_storage, instance).handle(PrepareMedia(key=key)))
    assert raised.value.reason == reason
    assert not any((worker_storage.root / "media" / "blobs").iterdir())


def test_the_job_deadline_cancels_a_partial_download(worker_storage, registry):
    _, key = planned(worker_storage, registry)
    source = FakeLibrary(worker_storage.clock)
    source.gate = asyncio.Event()
    instance = worker(worker_storage, source, limits=WorkerLimits(job_seconds=.2))
    with pytest.raises(TransientFailure, match="worker_timeout"):
        asyncio.run(maker(worker_storage, instance).handle(PrepareMedia(key=key)))
    assert source.cancelled
    assert not any((worker_storage.root / "media" / "blobs").iterdir())


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
    ran = []

    async def queue():
        ran.append(worker_storage.repository.health()["worker_seen"])

    asyncio.run(_media_loop(instance, queue))
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


def test_a_new_key_or_address_gets_a_new_client(worker_storage):
    made = []
    instance = MediaWorker(worker_storage.repository,
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


# Roadmap 5a (slice M1): measuring one photo's key content ---------------------------------------


def _grey_jpeg() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (64, 48), (128, 128, 128)).save(output, "JPEG")
    return output.getvalue()


class FakeFaces:
    """The connection's LibraryClient for a measurement: Immich's faces and its thumbnail."""

    def __init__(self, thumbnail=b"", *, faces=(), faces_fault=None, thumbnail_fault=None,
                 delay=0.0):
        self.thumbnail_bytes, self.boxes, self.delay = thumbnail, faces, delay
        self.faces_fault, self.thumbnail_fault = faces_fault, thumbnail_fault
        self.asked = []

    async def faces(self, upstream_id):
        self.asked.append(("faces", upstream_id))
        if self.faces_fault:
            raise self.faces_fault
        return self.boxes

    async def thumbnail(self, upstream_id, revision):
        self.asked.append(("thumbnail", upstream_id, revision))
        await asyncio.sleep(self.delay)
        if self.thumbnail_fault:
            raise self.thumbnail_fault
        return self.thumbnail_bytes

    async def close(self):
        pass


def _measuring(storage, client, **limits):
    pooled = photo("measured", taken_at=900)
    seed_pool(storage, "family:1", (pooled,))
    store = PgKeyContent(storage.db, times=ProcessTransactionClock(storage.clock))
    instance = worker(storage, client, key_content=store, limits=WorkerLimits(**limits))
    return pooled, store, instance


def _analysis(storage, store, asset_id):
    with storage.db.transaction() as conn:
        return store.analyses_in(conn, [asset_id], now=storage.clock.utc()).get(asset_id)


def test_measure_records_the_faces_and_interest_of_the_photo_at_its_revision(worker_storage):
    face = FaceBox(x1=0, y1=0, x2=20, y2=20, image_width=64, image_height=48)
    client = FakeFaces(_grey_jpeg(), faces=(face,))
    pooled, store, instance = _measuring(worker_storage, client)
    asyncio.run(instance.measure(pooled.asset_id))
    analysis = _analysis(worker_storage, store, pooled.asset_id)
    assert analysis.status == "measured" and len(analysis.key.faces) == 1
    assert client.asked == [("faces", pooled.upstream_id),
                            ("thumbnail", pooled.upstream_id, pooled.revision)]
    asyncio.run(instance.measure("asset-gone"))       # the asset is gone: nothing to do
    assert client.asked[2:] == []


class FakePreview(FakeFaces):
    """A measurement's client that also serves the preview (1440 x 960) when asked."""

    async def fetch_rendition(self, asset, *, plan, needs, destination):
        self.asked.append(("fetch", tuple(plan)))
        destination.write_bytes(b"preview")
        return FetchedRendition(
            path=destination, rendition="preview", media_type="image/jpeg", size=7,
            sha256=hashlib.sha256(b"preview").hexdigest(), width=1440, height=960,
            measured=(MeasuredCopy(name="preview", media_type="image/jpeg", width=1440,
                                   height=960),))


def test_measuring_a_raw_photo_measures_its_preview_once(worker_storage):
    """Follow-up S1: an unedited RAW photo's readable size is its preview's, so its measurement
    downloads the preview, records its size (rendition.copy_to_measure) and never again; a JPEG
    original is read at its own size, so its measurement downloads no copy."""
    client = FakePreview(_grey_jpeg())
    raw = photo("raw", taken_at=900, width=6000, height=4000, original_type="image/x-sony-arw")
    seed_pool(worker_storage, "family:1", (raw,))
    store = PgKeyContent(worker_storage.db, times=ProcessTransactionClock(worker_storage.clock))
    instance = worker(worker_storage, client, key_content=store)
    # The worker's store knows the revision (the PostgreSQL pool seeded above is the one measured).
    instance.library.revisions[raw.asset_id] = RevisionRecord(
        raw.connection_ref, raw.upstream_id, raw.revision, raw.width, raw.height, None, None, 0.0)
    asyncio.run(instance.measure(raw.asset_id))
    assert ("fetch", ("preview",)) in client.asked
    assert {name: (copy.width, copy.height) for name, copy in
            instance.library.copy_sizes(raw.asset_id).items()} == {"preview": (1440, 960)}
    client.asked.clear()
    asyncio.run(instance.measure(raw.asset_id))
    assert all(entry[0] != "fetch" for entry in client.asked)


@pytest.mark.parametrize(("client", "limits", "reason"), [
    (FakeFaces(b"corrupt thumbnail"), {}, "decode"),
    (FakeFaces(thumbnail_fault=MediaError("thumbnail_unsupported", "incompatible")), {},
     "decode"),
    (FakeFaces(_grey_jpeg(), delay=0.5), {"measure_seconds": 0.05}, "timeout"),
])
def test_a_photo_that_cannot_be_measured_is_recorded_failed(worker_storage, client, limits,
                                                            reason):
    pooled, store, instance = _measuring(worker_storage, client, **limits)
    asyncio.run(instance.measure(pooled.asset_id))
    assert _analysis(worker_storage, store, pooled.asset_id).model_dump() == {
        "status": "failed", "reason": reason}


@pytest.mark.parametrize("client", [
    FakeFaces(_grey_jpeg(), faces_fault=LibraryUnavailable(Unreachable())),
    FakeFaces(_grey_jpeg(), thumbnail_fault=MediaError("asset_changed", "incompatible")),
    FakeFaces(_grey_jpeg(), thumbnail_fault=MediaError("upstream_unavailable")),
])
def test_a_library_failure_is_retried_and_records_nothing(worker_storage, client):
    pooled, store, instance = _measuring(worker_storage, client)
    with pytest.raises(RetryableMediaTask):
        asyncio.run(instance.measure(pooled.asset_id))
    assert _analysis(worker_storage, store, pooled.asset_id) is None


@pytest.mark.parametrize("client", [
    FakeFaces(_grey_jpeg(), faces_fault=LibraryUnavailable(Unreachable())),
    FakeFaces(thumbnail_fault=MediaError("thumbnail_not_ready", "unavailable")),
])
def test_the_last_attempt_records_the_failure_so_lower_ranked_photos_are_measured(
        worker_storage, client):
    """A photo Immich never answers for leaves the Planner's measurement requests until its
    retry instead of holding the top of the list. Mutation probe: raise on the last attempt."""
    pooled, store, instance = _measuring(worker_storage, client)
    asyncio.run(instance.measure(pooled.asset_id, final_attempt=True))
    assert _analysis(worker_storage, store, pooled.asset_id).model_dump() == {
        "status": "failed", "reason": "timeout"}


def test_measure_without_a_key_content_store_is_a_configuration_error(worker_storage):
    instance = worker(worker_storage, FakeFaces())
    with pytest.raises(MediaError, match="media_config"):
        asyncio.run(instance.measure("asset-1"))
