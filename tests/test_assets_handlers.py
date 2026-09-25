"""The assets fetch and prefetch handlers: a fake origin, a real cache dir, and the real
Asset records (PostgreSQL, the `registry` fixture)."""

from __future__ import annotations

import asyncio
import hashlib
import io
import tarfile
import threading
from pathlib import Path

import pytest
from content_db import RecordingTransactions
from fakes.catalog import StaticContentCatalog
from fakes.origin import FakeReleaseOrigin
from fakes.publisher import RecordingPublisher
from fakes.transactions import FakeTransactions

from central.assets import handlers
from central.assets.handlers import (
    MAX_PACKAGE_BYTES,
    MAX_TARBALL_BYTES,
    FetchOsImageHandler,
    FetchPackageHandler,
    PrefetchHandler,
)
from central.assets.layout import TEMP_PREFIX, CacheLayout
from central.assets.production import AssetProduction
from central.assets.store import CacheStore
from central.infra.asset_records import PgAssetRecords
from central.kernel.assets import AssetKey, AssetKind, AssetReady, AssetReference, OriginLocator
from central.kernel.handling import (
    OriginRejected,
    OriginUnavailable,
    TerminalFailure,
    handler_job_type,
)
from central.kernel.job_types import FetchOsImage, FetchPackage, Prefetch
from central.kernel.ports import ReleaseListing
from contracts.time import ManualClock

SQUASHFS = b"squashfs image bytes " * 64
DEB = b"player deb bytes " * 64


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def facts(data: bytes) -> AssetReady:
    return AssetReady(size=len(data), sha256=sha(data))


def tarball(squashfs: bytes = SQUASHFS, listed: bytes | None = None) -> bytes:
    sums = f"{sha(listed if listed is not None else squashfs)}  ./photo-wall-base.squashfs\n"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in (("photo-wall-base/photo-wall-base.squashfs", squashfs),
                           ("photo-wall-base/SHA256SUMS", sums.encode())):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class SpyOrigin(FakeReleaseOrigin):
    """Records the `max_bytes` each download was given."""

    def __init__(self, blobs) -> None:
        super().__init__(ReleaseListing((), etag=None, unchanged=True), blobs)
        self.max_bytes: list[int] = []

    async def download(self, locator, into, *, max_bytes):
        self.max_bytes.append(max_bytes)
        await super().download(locator, into, max_bytes=max_bytes)


class World:
    def __init__(self, registry, tmp_path, blobs=None) -> None:
        self.store = CacheStore(CacheLayout(tmp_path))
        self.clock = ManualClock(1000.0)
        self.records = PgAssetRecords(self.clock)
        self.transactions = RecordingTransactions(registry.db)
        self.origin = SpyOrigin(blobs or {})
        self.production = AssetProduction(store=self.store, records=self.records,
                                          transactions=self.transactions)

    def reference(self, key, owner, locator, expected: AssetReady | None = None) -> None:
        """Each reference is added one second after the previous one: the newest comes first."""
        self.clock.advance(1)
        with self.transactions.begin() as tx:
            self.records.reference(tx, key, AssetReference(
                owner, locator, expected_size=expected.size if expected else None,
                expected_sha256=expected.sha256 if expected else None))

    def record_produced(self, key, facts: AssetReady) -> None:
        with self.transactions.begin() as tx:
            self.records.record_produced(tx, key, facts)

    def temps(self) -> list[str]:
        root = self.store.layout
        return [p.name for kind in AssetKind if root.directory(kind).exists()
                for p in root.directory(kind).iterdir() if p.name.startswith(TEMP_PREFIX)]


@pytest.fixture
def world_at(registry, tmp_path):
    return lambda blobs=None: World(registry, tmp_path, blobs)


# -- FetchOsImageHandler --------------------------------------------------------------------------

TAG = "v2.0.0"
BLOB = tarball()  # built once: its sha256 is the OS image's key
OS_JOB = FetchOsImage(tarball_sha256=sha(BLOB))
OS_KEY = AssetKey(AssetKind.OS_IMAGE, sha(BLOB))


def os_locator(data: bytes, url: str = "https://example.test/base.tar.gz", sized=True):
    return OriginLocator(url, sha256=sha(data), size=len(data) if sized else None)


def test_os_image_handler_downloads_extracts_off_the_loop_and_installs(world_at, monkeypatch):
    blob = BLOB
    world = world_at({"https://example.test/base.tar.gz": blob})
    world.reference(OS_KEY, TAG, os_locator(blob))
    threads: list[bool] = []
    real_extract = handlers.extract_squashfs

    def spy(tar, into):
        threads.append(threading.current_thread() is threading.main_thread())
        real_extract(tar, into)

    monkeypatch.setattr(handlers, "extract_squashfs", spy)
    handler = FetchOsImageHandler(production=world.production, origin=world.origin,
                                  store=world.store)
    assert asyncio.run(handler.handle(OS_JOB)) == facts(SQUASHFS)
    assert threads == [False]  # extraction ran in a worker thread, not on the event loop
    assert world.store.layout.path(OS_KEY).read_bytes() == SQUASHFS
    assert world.origin.max_bytes == [len(blob)]
    assert world.temps() == []  # the tarball temp is always discarded


def test_os_image_handler_uses_the_newest_reference_and_caps_unsized_downloads(world_at):
    blob = BLOB
    world = world_at({"https://example.test/new.tar.gz": blob})
    world.reference(OS_KEY, "old", os_locator(blob, "https://example.test/old.tar.gz"))
    world.reference(OS_KEY, "new", os_locator(blob, "https://example.test/new.tar.gz", sized=False))
    handler = FetchOsImageHandler(production=world.production, origin=world.origin,
                                  store=world.store)
    asyncio.run(handler.handle(OS_JOB))
    assert [loc.url for loc in world.origin.downloads] == ["https://example.test/new.tar.gz"]
    assert world.origin.max_bytes == [MAX_TARBALL_BYTES]


def test_os_image_handler_hostile_archive_is_terminal_and_leaves_no_temp(world_at):
    blob = tarball(listed=b"not the squashfs")
    key = AssetKey(AssetKind.OS_IMAGE, sha(blob))  # the hostile tarball is its own key
    world = world_at({"https://example.test/base.tar.gz": blob})
    world.reference(key, TAG, os_locator(blob))
    handler = FetchOsImageHandler(production=world.production, origin=world.origin,
                                  store=world.store)
    with pytest.raises(TerminalFailure) as raised:
        asyncio.run(handler.handle(FetchOsImage(tarball_sha256=sha(blob))))
    assert raised.value.reason == "base_digest_mismatch"
    assert world.temps() == []
    assert not world.store.layout.path(key).exists()


def test_os_image_handler_download_failure_leaves_no_temp(world_at):
    blob = BLOB
    world = world_at({"https://example.test/base.tar.gz": OriginUnavailable("http_503")})
    world.reference(OS_KEY, TAG, os_locator(blob))
    handler = FetchOsImageHandler(production=world.production, origin=world.origin,
                                  store=world.store)
    with pytest.raises(OriginUnavailable):
        asyncio.run(handler.handle(OS_JOB))
    assert world.temps() == []


# -- FetchPackageHandler --------------------------------------------------------------------------

DEB_KEY = AssetKey(AssetKind.PLAYER_DEB, sha(DEB))


def deb_locator(url: str, data: bytes = DEB) -> OriginLocator:
    return OriginLocator(url, sha256=sha(data), size=len(data))


def package_world(world_at, blobs, owners=("v1.0.0", "v1.1.0")) -> World:
    world = world_at(blobs)
    for owner in owners:  # added in order, so the last is the newest
        world.reference(DEB_KEY, owner, deb_locator(f"https://example.test/{owner}.deb"),
                        expected=facts(DEB))
    return world


def run_package(world: World):
    handler = FetchPackageHandler(production=world.production, origin=world.origin)
    return asyncio.run(handler.handle(FetchPackage(sha256=sha(DEB))))


def test_package_handler_two_tags_share_one_file_and_one_producer(world_at):
    world = package_world(world_at, {"https://example.test/v1.1.0.deb": DEB})
    assert run_package(world) == facts(DEB)
    assert world.store.layout.path(DEB_KEY).read_bytes() == DEB
    assert [loc.url for loc in world.origin.downloads] == ["https://example.test/v1.1.0.deb"]
    assert world.origin.max_bytes == [len(DEB)]
    # the second tag's request is the same job, and the verified file answers it without fetching
    assert run_package(world) == facts(DEB)
    assert len(world.origin.downloads) == 1


# -- every reference is tried, newest first (AssetProduction), for both kinds ---------------------

OK = object()  # the kind's good bytes at that owner's URL


def fallthrough(world_at, kind: str, blobs: dict[str, object]):
    """Two references (v1.0.0, then the newest v1.1.0) to one asset of `kind`, each at its own URL;
    `blobs` maps an owner to OK or the exception its URL raises (unmapped: 404)."""
    data, key = (DEB, DEB_KEY) if kind == "deb" else (BLOB, OS_KEY)
    world = world_at({f"https://example.test/{owner}.{kind}": data if blob is OK else blob
                      for owner, blob in blobs.items()})
    for owner in ("v1.0.0", "v1.1.0"):
        locator = OriginLocator(f"https://example.test/{owner}.{kind}", sha256=sha(data),
                                size=len(data))
        world.reference(key, owner, locator, expected=facts(DEB) if kind == "deb" else None)
    if kind == "deb":
        handler = FetchPackageHandler(production=world.production, origin=world.origin)
        return world, lambda: asyncio.run(handler.handle(FetchPackage(sha256=sha(DEB)))), DEB
    os_handler = FetchOsImageHandler(production=world.production, origin=world.origin,
                                     store=world.store)
    return world, lambda: asyncio.run(os_handler.handle(OS_JOB)), SQUASHFS


@pytest.mark.parametrize("kind", ["deb", "os"])
def test_a_rejected_reference_falls_through_to_the_next(world_at, kind):
    world, run, produced = fallthrough(world_at, kind, {"v1.0.0": OK})  # newest -> 404
    assert run() == facts(produced)
    assert [loc.url for loc in world.origin.downloads] == [
        f"https://example.test/v1.1.0.{kind}", f"https://example.test/v1.0.0.{kind}"]
    assert world.temps() == []


@pytest.mark.parametrize("kind", ["deb", "os"])
def test_the_last_unavailable_is_raised_after_trying_all(world_at, kind):
    world, run, _ = fallthrough(world_at, kind, {
        "v1.1.0": OriginUnavailable("http_503"),
        "v1.0.0": OriginRejected("not_found"),
    })
    with pytest.raises(OriginUnavailable) as raised:
        run()
    assert raised.value.reason == "http_503"
    assert len(world.origin.downloads) == 2
    assert world.temps() == []


@pytest.mark.parametrize("kind", ["deb", "os"])
def test_all_references_rejected_is_terminal(world_at, kind):
    world, run, _ = fallthrough(world_at, kind, {})
    with pytest.raises(TerminalFailure) as raised:
        run()
    assert raised.value.reason == "all_references_rejected"
    assert type(raised.value) is TerminalFailure
    assert world.temps() == []


def test_package_handler_caps_an_unsized_locator(world_at):
    world = world_at({"https://example.test/x.deb": DEB})
    world.reference(DEB_KEY, "v1.0.0",
                    OriginLocator("https://example.test/x.deb", sha256=sha(DEB), size=None))
    run_package(world)
    assert world.origin.max_bytes == [MAX_PACKAGE_BYTES]


def test_handlers_declare_their_job_types():
    store = CacheStore(CacheLayout(Path("/nonexistent")))
    production = AssetProduction(store=store, records=PgAssetRecords(ManualClock(0.0)),
                                 transactions=FakeTransactions())
    origin = SpyOrigin({})
    assert handler_job_type(FetchOsImageHandler(production=production, origin=origin,
                                                store=store)) is FetchOsImage
    assert handler_job_type(FetchPackageHandler(production=production, origin=origin)) \
        is FetchPackage
    prefetch = PrefetchHandler(catalog=StaticContentCatalog({}),
                               records=PgAssetRecords(ManualClock(0.0)),
                               store=store, transactions=FakeTransactions(),
                               publisher=RecordingPublisher(ManualClock(0.0)))
    assert handler_job_type(prefetch) is Prefetch


# -- PrefetchHandler ------------------------------------------------------------------------------


def test_prefetch_publishes_only_recorded_assets_missing_from_disk(world_at):
    world = world_at()
    present = FetchOsImage(tarball_sha256=sha(b"tarball v1.0.0"))
    absent_file = FetchOsImage(tarball_sha256=sha(b"tarball v1.1.0"))
    never_produced = FetchPackage(sha256=sha(DEB))
    unrecorded = FetchOsImage(tarball_sha256=sha(b"tarball v9.9.9"))
    for job, owner in ((present, "v1.0.0"), (absent_file, "v1.1.0")):
        key = AssetKey(AssetKind.OS_IMAGE, job.tarball_sha256)
        world.reference(key, owner, OriginLocator("https://example.test/b.tgz",
                                                  sha256=job.tarball_sha256, size=None))
        world.record_produced(key, facts(SQUASHFS))
    world.reference(DEB_KEY, "v1.0.0", deb_locator("https://example.test/a.deb"), facts(DEB))
    path = world.store.layout.path(AssetKey(AssetKind.OS_IMAGE, present.tarball_sha256))
    path.parent.mkdir(parents=True)
    path.write_bytes(SQUASHFS)
    wrong_size = world.store.layout.path(
        AssetKey(AssetKind.OS_IMAGE, absent_file.tarball_sha256))
    wrong_size.write_bytes(SQUASHFS[:-1])  # present on disk but not the produced file

    publisher = RecordingPublisher(ManualClock(1000.0))
    catalog = StaticContentCatalog(
        {}, desired=frozenset({present, absent_file, never_produced, unrecorded}))
    handler = PrefetchHandler(catalog=catalog, records=world.records, store=world.store,
                              transactions=world.transactions, publisher=publisher)
    assert asyncio.run(handler.handle(Prefetch())) is None
    assert {call.job for call in publisher.calls} == {absent_file, never_produced}
    assert all(call.retry_terminal is False and call.within is None for call in publisher.calls)
    assert all(tx.state == "committed" for tx in world.transactions.begun)
