"""`SyncReleasesHandler` against `FakeReleaseOrigin` and the real repositories.

PostgreSQL (the `registry` fixture; CI runs it). The origin is the only fake besides the
`RecordingPublisher`, which records what the sync publishes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from content_db import (
    Reads,
    RecordingTransactions,
    facts_of,
    record_produced,
    seed_releases,
    sha,
    store_deb,
)
from fakes.origin import FakeReleaseOrigin
from fakes.publisher import PublishedCall, RecordingPublisher

from central.assets.layout import CacheLayout
from central.assets.production import AssetProduction
from central.assets.store import CacheStore
from central.content_catalog.catalog import ReleaseCatalog
from central.content_catalog.sync import SyncReleasesHandler
from central.infra.asset_records import PgAssetRecords
from central.infra.catalog_records import PgReleaseRecords
from central.infra.node_releases import PgNodeReleaseRecords
from central.infra.stored_assets import DiskStoredAssets
from central.kernel.assets import AssetKey, AssetKind, AssetReference, OriginLocator
from central.kernel.handling import OriginUnavailable, handler_job_type
from central.kernel.job_types import FetchOsImage, Prefetch, SyncReleases
from central.kernel.ports import (
    PublishedRelease,
    ReleaseListing,
    ReleaseOrigin,
    UpstreamVersion,
)
from contracts.time import ManualClock

T1, T, T2 = "v0.0.1", "v0.0.2", "v0.0.3"
NOW = 10_000.0


def deb(tag: str, cut: str = "") -> OriginLocator:
    """A Player `.deb` locator, as the netboot tracer seeds one (no sync reads a `.deb`)."""
    return OriginLocator(f"https://example.test/{tag}{cut}.deb", sha("deb" + tag + cut), 10)


def image(tag: str, cut: str = "") -> OriginLocator:
    return OriginLocator(f"https://example.test/{tag}.tgz", sha("img" + tag + cut), 64)


def version(at: float) -> UpstreamVersion:
    """An upstream version: the manifest asset's `updated_at` (`at`), with one asset id."""
    return UpstreamVersion(float(at), 1)


def published(tag: str, *, pre: bool = False, os_image: OriginLocator | None | str = "default",
              at: float | None = 1) -> PublishedRelease:
    """`tag` observed at upstream version `at` (None: the manifest body was not read). A re-cut
    upstream is a newer version."""
    base = image(tag) if os_image == "default" else os_image
    return PublishedRelease(tag, pre, base, None if at is None else version(at))


def listing(*releases: PublishedRelease, etag: str | None = "e1") -> ReleaseListing:
    return ReleaseListing(tuple(releases), etag, unchanged=False)


class RecordingOrigin(FakeReleaseOrigin):
    """Records every ETag it is sent. With `honour_etag`, it answers as GitHub does: unchanged
    (304) when sent the ETag of the listing it holds."""

    def __init__(self, listing_or_error, *, honour_etag: bool = False) -> None:
        super().__init__(listing_or_error, {})
        self.etags: list[str | None] = []
        self.honour_etag = honour_etag

    async def list_releases(self, *, etag: str | None) -> ReleaseListing:
        self.etags.append(etag)
        held = self.listing
        if (self.honour_etag and etag is not None and isinstance(held, ReleaseListing)
                and held.etag == etag):
            return ReleaseListing((), etag, unchanged=True)
        return await super().list_releases(etag=etag)


@dataclass
class World:
    handler: SyncReleasesHandler
    origin: ReleaseOrigin  # a `RecordingOrigin` unless the test passed its own
    assets: PgAssetRecords
    transactions: RecordingTransactions
    publisher: RecordingPublisher
    store: CacheStore
    reads: Reads
    db: object
    catalog: ReleaseCatalog  # the handler's: the operator route runs through the same one
    clock: ManualClock
    stored: DiskStoredAssets

    def another_handler(self, origin: ReleaseOrigin) -> SyncReleasesHandler:
        """A second sync (another worker) over the same database, publisher and clock, with its
        own records and transactions."""
        return SyncReleasesHandler(origin=origin, releases=PgReleaseRecords(),
                                   assets=self.assets,
                                   transactions=RecordingTransactions(self.db),
                                   publisher=self.publisher, catalog=self.catalog,
                                   clock=self.clock, node_releases=PgNodeReleaseRecords(self.clock),
                                   readiness=self.stored)

    def updated_at(self) -> dict[str, float]:
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT tag, updated_at FROM app_releases").fetchall()
        return {row["tag"]: row["updated_at"] for row in rows}


def make_world(registry, tmp_path):
    """The `world` fixture's builder, for another test module's own fixture."""
    def build(origin_listing, *, releases=(), promoted=None, promoted_by="operator",
              etag=None, assets: PgAssetRecords | None = None, on_disk=(),
              origin: ReleaseOrigin | None = None, honour_etag: bool = False) -> World:
        clock = ManualClock(NOW)
        seeding = RecordingTransactions(registry.db)
        seed_releases(seeding, releases, promoted=promoted, promoted_by=promoted_by, etag=etag,
                      etag_stored_at=NOW)  # fresh: the sync sends it
        assets = assets or PgAssetRecords(clock)
        store = CacheStore(CacheLayout(tmp_path))
        for locator in on_disk:
            store_deb(seeding, assets, store, locator.sha256, locator.size)
        transactions = RecordingTransactions(registry.db)
        publisher = RecordingPublisher(clock)
        stored = DiskStoredAssets(records=assets, store=store)
        catalog = ReleaseCatalog(releases=PgReleaseRecords(), stored=stored,
                                 transactions=transactions, publisher=publisher, clock=clock)
        origin = origin or RecordingOrigin(origin_listing, honour_etag=honour_etag)
        handler = SyncReleasesHandler(origin=origin, releases=PgReleaseRecords(), assets=assets,
                                      transactions=transactions, publisher=publisher,
                                      catalog=catalog, clock=clock,
                                      node_releases=PgNodeReleaseRecords(clock),
                                      readiness=stored)
        return World(handler, origin, assets, transactions, publisher, store,
                     Reads(RecordingTransactions(registry.db)), registry.db, catalog, clock,
                     stored)

    return build


@pytest.fixture
def world(registry, tmp_path):
    return make_world(registry, tmp_path)


def sync(w: World) -> None:
    asyncio.run(w.handler.handle(SyncReleases()))


def image_key(tag: str, cut: str = "") -> AssetKey:
    """The OS image's key: the sha256 of `image(tag, cut)`'s tarball."""
    return AssetKey(AssetKind.OS_IMAGE, image(tag, cut).sha256)


def os_job(tag: str, cut: str = "") -> FetchOsImage:
    return FetchOsImage(tarball_sha256=image(tag, cut).sha256)


def deb_key(locator: OriginLocator) -> AssetKey:
    return AssetKey(AssetKind.PLAYER_DEB, locator.sha256)


def test_handler_serves_sync_releases(world):
    assert handler_job_type(world(listing()).handler) is SyncReleases


def test_first_sync_records_releases_and_their_os_images_and_no_deb(world):
    # The OS image is referenced; nothing is fetched for it (no V1 netboot desires it) and no
    # Player `.deb` is read, referenced or promoted.
    w = world(listing(published(T1), published(T2, pre=True, os_image=None)), etag="e0")
    sync(w)
    assert w.origin.etags == ["e0"]
    assert w.reads.etag() == "e1"
    assert w.updated_at() == {T1: NOW, T2: NOW}
    assert w.reads.asset(image_key(T1)).references == (
        AssetReference(T1, image(T1), None, None),)
    assert {tag: row.package for tag, row in w.reads.releases().items()} == {T1: None, T2: None}
    assert w.reads.promoted() is None
    # Load etag, one per release, store etag, tail: each its own committed transaction.
    assert [tx.state for tx in w.transactions.begun] == ["committed"] * 5
    assert w.publisher.calls == [PublishedCall(Prefetch(), False, w.transactions.begun[-1])]
    with w.db.transaction() as conn:
        assert conn.execute("SELECT kind FROM assets WHERE kind<>'os-image'").fetchall() == []


def test_a_sync_keeps_a_seeded_deb_and_its_promotion(world):
    # The tracer's state: a release whose `.deb` was recorded and promoted before. The sync
    # writes no package column, so a new observation of the tag keeps both.
    seeded = PublishedRelease(T1, False, image(T1), version(1))
    w = world(listing(published(T1, at=2)), releases=[seeded], promoted=T1)
    with w.db.transaction() as conn:
        conn.execute("UPDATE app_releases SET asset_url=%s,asset_sha256=%s,asset_size=%s "
                     "WHERE tag=%s", (deb(T1).url, deb(T1).sha256, deb(T1).size, T1))
    sync(w)
    assert w.reads.release(T1).package == deb(T1)
    assert w.reads.promoted() == T1


def test_unchanged_listing_writes_no_release_but_still_runs_the_tail(world):
    w = world(ReleaseListing((), "e1", unchanged=True), releases=[published(T1)], etag="e1")
    sync(w)
    assert w.updated_at() == {T1: 1000.0} and w.reads.etag() == "e1"  # the seed's write only
    assert len(w.transactions.begun) == 2  # load etag + tail
    assert w.publisher.calls == [PublishedCall(Prefetch(), False, w.transactions.begun[-1])]


def test_resync_of_identical_facts_publishes_no_fetch(world):
    w = world(listing(published(T1)))
    sync(w)
    first = len(w.publisher.calls)
    w.origin.listing = listing(published(T1), etag="e2")
    sync(w)
    assert [call.job for call in w.publisher.calls[first:]] == [Prefetch()]
    assert w.reads.etag() == "e2"


def writing(data: bytes):
    async def write(temp, locator) -> None:
        temp.write_bytes(data)
    return write


def test_a_recut_os_image_is_a_new_key_and_the_old_keeps_its_facts(world):
    # pipeline.yml re-uploads a rebuilt image under its tag (--clobber): another tarball, so
    # another key. The tag moves its reference; the old key's facts are never cleared, and the
    # new build is produced and recorded under its own key (PR #22 review P1: no
    # `not_reproducible` for good).
    w = world(listing(published(T1)))
    sync(w)
    old, new = image_key(T1), image_key(T1, cut="-rebuilt")
    production = AssetProduction(store=w.store, records=w.assets, transactions=w.transactions)
    built = asyncio.run(production.produce(os_job(T1), writing(b"build-1")))
    record_produced(w.transactions, w.assets, old, built)  # what the runtime records
    first = len(w.publisher.calls)
    w.origin.listing = listing(published(T1, os_image=image(T1, cut="-rebuilt"), at=2),
                               etag="e2")
    sync(w)
    assert w.reads.asset(old) is None  # no reference left: not an asset
    assert w.reads.facts(old) == built  # but its row and facts stay
    asset = w.reads.asset(new)
    assert asset.produced is None
    assert asset.references == (AssetReference(T1, image(T1, cut="-rebuilt"), None, None),)
    assert [call.job for call in w.publisher.calls[first:]] == [Prefetch()]  # not desired
    rebuilt = asyncio.run(production.produce(os_job(T1, cut="-rebuilt"), writing(b"build-2")))
    assert rebuilt == facts_of(b"build-2")
    record_produced(w.transactions, w.assets, new, rebuilt)  # no ProducedFactsConflict
    assert w.reads.asset(new).produced == rebuilt
    assert w.reads.facts(old) == built


@pytest.mark.parametrize("changed", [
    image(T1),  # identical facts
    OriginLocator("https://mirror.test/v0.0.1.tgz", image(T1).sha256, image(T1).size),  # moved
])
def test_an_os_image_whose_bytes_did_not_change_keeps_its_produced_facts(world, changed):
    w = world(listing(published(T1)))
    sync(w)
    facts = facts_of(b"build-1")
    record_produced(w.reads.transactions, w.assets, image_key(T1), facts)
    w.origin.listing = listing(published(T1, os_image=changed, at=2), etag="e2")
    sync(w)
    assert w.reads.asset(image_key(T1)).produced == facts


def test_a_dropped_image_retires_its_reference(world):
    w = world(listing(published(T1)))
    sync(w)
    record_produced(w.reads.transactions, w.assets, image_key(T1), facts_of(b"build-1"))
    w.origin.listing = listing(published(T1, os_image=None, at=2), etag="e2")
    sync(w)
    assert w.reads.owners(image_key(T1)) is None
    assert w.reads.facts(image_key(T1)) == facts_of(b"build-1")  # retiring never clears facts


def test_origin_failure_propagates_and_writes_nothing(world):
    w = world(OriginUnavailable("github_down"), promoted=None)
    with pytest.raises(OriginUnavailable):
        sync(w)
    assert len(w.transactions.begun) == 1  # only the etag read
    assert w.reads.releases() == {} and w.publisher.calls == []


def test_a_failing_release_transaction_keeps_the_etag_so_the_next_sync_retries(world):
    class Boom(PgAssetRecords):
        def reference(self, tx, key, ref):
            raise RuntimeError("db down")

    w = world(listing(published(T1)), etag="e0", assets=Boom(ManualClock(NOW)))
    with pytest.raises(RuntimeError):
        sync(w)
    assert w.reads.etag() == "e0"
    assert w.transactions.begun[-1].state == "rolled_back"
