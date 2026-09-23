"""`SyncReleasesHandler` against `FakeReleaseOrigin` and the real repositories.

PostgreSQL (the `registry` fixture; CI runs it). The origin is the only fake besides the
`RecordingPublisher`, which records what the sync publishes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

import pytest
from content_db import (
    Reads,
    RecordingTransactions,
    bind_a_player,
    facts_of,
    insert_device,
    record_produced,
    reference,
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
from central.infra.catalog_records import PgDeviceRecords, PgReleaseRecords
from central.infra.stored_assets import DiskStoredAssets
from central.kernel.assets import AssetKey, AssetKind, AssetReady, AssetReference, OriginLocator
from central.kernel.handling import OriginUnavailable, handler_job_type
from central.kernel.job_types import FetchOsImage, FetchPackage, Prefetch, SyncReleases
from central.kernel.ports import PublishedRelease, ReleaseListing
from contracts.time import ManualClock

T1, T, T2 = "v0.0.1", "v0.0.2", "v0.0.3"
NOW = 10_000.0
THRESHOLD = NOW - 15 * 60  # last_served_at < this => stale


def deb(tag: str, cut: str = "") -> OriginLocator:
    return OriginLocator(f"https://example.test/{tag}{cut}.deb", sha("deb" + tag + cut), 10)


def image(tag: str, cut: str = "") -> OriginLocator:
    return OriginLocator(f"https://example.test/{tag}.tgz", sha("img" + tag + cut), 64)


def published(tag: str, *, pre: bool = False, package: OriginLocator | None | str = "default",
              os_image: OriginLocator | None | str = "default") -> PublishedRelease:
    locator = deb(tag) if package == "default" else package
    base = image(tag) if os_image == "default" else os_image
    return PublishedRelease(tag, pre, locator, None if locator else "no_player_asset", base)


def listing(*releases: PublishedRelease, etag: str | None = "e1") -> ReleaseListing:
    return ReleaseListing(tuple(releases), etag, unchanged=False)


class RecordingOrigin(FakeReleaseOrigin):
    def __init__(self, listing_or_error) -> None:
        super().__init__(listing_or_error, {})
        self.etags: list[str | None] = []

    async def list_releases(self, *, etag: str | None) -> ReleaseListing:
        self.etags.append(etag)
        return await super().list_releases(etag=etag)


@dataclass
class World:
    handler: SyncReleasesHandler
    origin: RecordingOrigin
    assets: PgAssetRecords
    transactions: RecordingTransactions
    publisher: RecordingPublisher
    store: CacheStore
    reads: Reads
    db: object

    def updated_at(self) -> dict[str, float]:
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT tag, updated_at FROM app_releases").fetchall()
        return {row["tag"]: row["updated_at"] for row in rows}


@pytest.fixture
def world(registry, tmp_path):
    def build(origin_listing, *, releases=(), devices=(), promoted=None, etag=None, bound=False,
              assets: PgAssetRecords | None = None, on_disk=(), **handler_options) -> World:
        clock = ManualClock(NOW)
        seeding = RecordingTransactions(registry.db)
        seed_releases(seeding, releases, promoted=promoted, etag=etag)
        for fields in devices:
            insert_device(registry.db, **fields)
        if bound:
            bind_a_player(registry)
        assets = assets or PgAssetRecords(clock)
        store = CacheStore(CacheLayout(tmp_path))
        for locator in on_disk:
            store_deb(seeding, assets, store, locator.sha256, locator.size)
        transactions = RecordingTransactions(registry.db)
        publisher = RecordingPublisher(clock)
        catalog = ReleaseCatalog(releases=PgReleaseRecords(), devices=PgDeviceRecords(),
                                 stored=DiskStoredAssets(records=assets, store=store),
                                 transactions=transactions, publisher=publisher, clock=clock)
        origin = RecordingOrigin(origin_listing)
        handler = SyncReleasesHandler(origin=origin, releases=PgReleaseRecords(),
                                      devices=PgDeviceRecords(), assets=assets,
                                      transactions=transactions, publisher=publisher,
                                      catalog=catalog, clock=clock, **handler_options)
        return World(handler, origin, assets, transactions, publisher, store,
                     Reads(RecordingTransactions(registry.db)), registry.db)

    return build


def dev(device_id: str, **fields) -> dict:
    return {"device_id": device_id, **fields}


def sync(w: World) -> None:
    asyncio.run(w.handler.handle(SyncReleases()))


def image_key(tag: str) -> AssetKey:
    return AssetKey(AssetKind.OS_IMAGE, tag)


def deb_key(locator: OriginLocator) -> AssetKey:
    return AssetKey(AssetKind.PLAYER_DEB, locator.sha256)


def produced(w: World, locator: OriginLocator) -> None:
    """The worker's effect on the record: the `.deb`'s produced facts (it was mirrored)."""
    seeding = w.reads.transactions  # not the handler's: its transactions are asserted on
    reference(seeding, w.assets, deb_key(locator), "seed", locator,
              expected=AssetReady(locator.size, locator.sha256))
    record_produced(seeding, w.assets, deb_key(locator), AssetReady(locator.size, locator.sha256))


def test_handler_serves_sync_releases(world):
    assert handler_job_type(world(listing()).handler) is SyncReleases


def test_first_sync_records_releases_references_promotes_and_fetches_desired_changes(world):
    w = world(listing(published(T1), published(T2, pre=True, os_image=None)), etag="e0")
    sync(w)
    assert w.origin.etags == ["e0"]
    assert w.reads.etag() == "e1"
    assert w.updated_at() == {T1: NOW, T2: NOW}
    # References: the image carries no expected facts; the .deb's equal its locator's.
    assert w.reads.asset(image_key(T1)).references == (
        AssetReference(T1, image(T1), None, None),)
    assert w.reads.asset(deb_key(deb(T1))).references == (
        AssetReference(T1, deb(T1), 10, deb(T1).sha256),)
    assert w.reads.owners(deb_key(deb(T2))) == [T2]
    # Auto-promote: nothing promoted -> the newest full release with a .deb (not the rc).
    assert w.reads.promoted() == T1
    # Load etag, one per release, store etag, tail: each its own committed transaction.
    assert [tx.state for tx in w.transactions.begun] == ["committed"] * 5
    tail = w.transactions.begun[-1]
    # Changed AND desired (bootstrap T1 + its .deb); the rc's .deb changed but is not desired.
    assert w.publisher.calls == [
        PublishedCall(FetchOsImage(tag=T1), True, tail),
        PublishedCall(FetchPackage(sha256=deb(T1).sha256), True, tail),
        PublishedCall(Prefetch(), False, tail),
    ]


def test_unchanged_listing_writes_no_release_but_still_runs_the_tail(world):
    w = world(ReleaseListing((), "e1", unchanged=True), releases=[published(T1)],
              promoted=T1, etag="e1", bound=True)
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


def test_recut_deb_retires_the_old_sha_and_fetches_the_new_one(world):
    w = world(listing(published(T1)))
    sync(w)
    first = len(w.publisher.calls)
    recut = deb(T1, cut="-recut")
    w.origin.listing = listing(published(T1, package=recut), etag="e2")
    sync(w)
    assert w.reads.owners(deb_key(deb(T1))) is None  # its last reference retired -> row gone
    assert w.reads.owners(deb_key(recut)) == [T1]
    assert [call.job for call in w.publisher.calls[first:]] == [
        FetchPackage(sha256=recut.sha256), Prefetch()]


def test_recut_of_a_produced_deb_freezes_the_tag_as_divergent(world):
    # Main froze a mirrored tag re-cut upstream (`upsert_discovered`: mirrored -> divergent):
    # the served bytes stay, the new sha is neither referenced nor fetched.
    w = world(listing(published(T1)))
    sync(w)
    produced(w, deb(T1))
    first = len(w.publisher.calls)
    recut = deb(T1, cut="-recut")
    w.origin.listing = listing(published(T1, package=recut), etag="e2")
    sync(w)
    row = w.reads.release(T1)
    assert row.package == deb(T1) and row.divergent
    assert T1 in w.reads.owners(deb_key(deb(T1))) and w.reads.owners(deb_key(recut)) is None
    assert [call.job for call in w.publisher.calls[first:]] == [Prefetch()]
    # Frozen for good: a later sync, even one dropping the .deb, keeps the produced facts.
    w.origin.listing = listing(published(T1, package=None), etag="e3")
    sync(w)
    assert w.reads.release(T1).package == deb(T1)
    assert T1 in w.reads.owners(deb_key(deb(T1)))


def test_recut_keeps_a_deb_another_tag_still_ships(world):
    shared = deb(T1)
    w = world(listing(published(T1), published(T2, package=shared)))
    sync(w)
    assert sorted(w.reads.owners(deb_key(shared))) == [T1, T2]
    w.origin.listing = listing(published(T1, package=deb(T1, cut="-recut")),
                               published(T2, package=shared), etag="e2")
    sync(w)
    assert w.reads.owners(deb_key(shared)) == [T2]


def writing(data: bytes):
    async def write(temp, asset) -> None:
        temp.write_bytes(data)
    return write


def test_a_recut_os_image_is_produced_again_after_a_cache_wipe(world):
    # PR #22 review P1: release.yml re-uploads a rebuilt image under its tag (--clobber). The
    # write-once produced facts then failed every later production `not_reproducible` once the
    # cache was wiped, so the tag was unservable for good (and each Pi boot re-fetched ~1 GB).
    w = world(listing(published(T1)))
    sync(w)
    key, job = image_key(T1), FetchOsImage(tag=T1)
    production = AssetProduction(store=w.store, records=w.assets, transactions=w.transactions)
    built = asyncio.run(production.produce(job, writing(b"build-1")))
    record_produced(w.transactions, w.assets, key, built)  # what the runtime records
    first = len(w.publisher.calls)
    w.origin.listing = listing(published(T1, os_image=image(T1, cut="-rebuilt")), etag="e2")
    sync(w)
    asset = w.reads.asset(key)
    assert asset.produced is None
    assert asset.references == (AssetReference(T1, image(T1, cut="-rebuilt"), None, None),)
    assert PublishedCall(FetchOsImage(tag=T1), True, w.transactions.begun[-1]) in (
        w.publisher.calls[first:])
    w.store.discard(w.store.layout.path(key))  # the cache is wiped
    rebuilt = asyncio.run(production.produce(job, writing(b"build-2")))
    assert rebuilt == facts_of(b"build-2")
    record_produced(w.transactions, w.assets, key, rebuilt)  # no ProducedFactsConflict
    assert w.reads.asset(key).produced == rebuilt


@pytest.mark.parametrize("changed", [
    image(T1),  # identical facts
    OriginLocator("https://mirror.test/v0.0.1.tgz", image(T1).sha256, image(T1).size),  # moved
])
def test_an_os_image_whose_bytes_did_not_change_keeps_its_produced_facts(world, changed):
    w = world(listing(published(T1)))
    sync(w)
    facts = facts_of(b"build-1")
    record_produced(w.reads.transactions, w.assets, image_key(T1), facts)
    w.origin.listing = listing(published(T1, os_image=changed), etag="e2")
    sync(w)
    assert w.reads.asset(image_key(T1)).produced == facts


def test_dropped_image_and_dropped_deb_retire_their_references(world):
    w = world(listing(published(T1)))
    sync(w)
    w.origin.listing = listing(published(T1, package=None, os_image=None), etag="e2")
    sync(w)
    assert w.reads.owners(image_key(T1)) is None
    assert w.reads.owners(deb_key(deb(T1))) is None


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


# -- tail: the stale-pending sweep (ports recovery (f)) -----------------------------------------


def test_tail_sweeps_stale_pending_boots_with_the_e2_and_e2b_rules(world):
    old, recent = THRESHOLD - 1, THRESHOLD + 1
    w = world(ReleaseListing((), None, unchanged=True),
              releases=[published(t) for t in (T1, T, T2)], devices=[
        dev("device-stick", boot_outcome="pending", failed_tag=T, last_served_tag=T1,
               last_served_at=old),
        dev("device-served-lower", boot_outcome="pending", last_served_tag=T,
               last_served_at=old),
        dev("device-pinned", boot_outcome="pending", attached_tag=T1, last_served_tag=T,
               last_served_at=old),
        dev("device-never", boot_outcome="pending", last_served_at=old),
        dev("device-recent", boot_outcome="pending", last_served_tag=T2,
               last_served_at=recent),
        dev("device-retired", boot_outcome="pending", last_served_tag=T2,
               last_served_at=old, retired=True),
        dev("frontier", known_good_tag=T2),
    ])
    sync(w)
    rows = {d: w.reads.device(d) for d in (
        "device-stick", "device-served-lower", "device-pinned", "device-never", "device-recent",
        "device-retired")}
    assert (rows["device-stick"].failed_tag, rows["device-stick"].boot_outcome) == (T, "failed")
    assert rows["device-served-lower"].failed_tag == T  # the served tag, not the frontier T2
    assert rows["device-pinned"].failed_tag == T1  # COALESCE(attached_tag, last_served_tag)
    assert (rows["device-never"].failed_tag, rows["device-never"].boot_outcome) == (
        None, "failed")
    assert rows["device-recent"].boot_outcome == "pending"
    assert rows["device-retired"].boot_outcome == "pending"


def test_pending_health_timeout_is_configurable(world):
    w = world(ReleaseListing((), None, unchanged=True), releases=[published(T)], devices=[
        dev("d", boot_outcome="pending", last_served_tag=T, last_served_at=NOW - 61)],
        pending_health_timeout=timedelta(minutes=1))
    sync(w)
    assert w.reads.device("d").boot_outcome == "failed"


# -- tail: auto-promote -------------------------------------------------------------------------


@pytest.mark.parametrize("promoted,bound,cached,expected", [
    # Main's `_autopull_deb` (central/app_release_boot.py): suppress iff cached > 0 AND bound > 0.
    (None, True, False, T),   # nothing cached: pull, even with bound players
    (T1, True, False, T),
    (T1, False, True, T),     # no bound player: follow the newest
    (None, False, True, T),
    (T1, True, True, T1),     # a configured fleet keeps the operator's choice
    (None, True, True, None),  # ... and is never promoted behind the operator's back (P1)
])
def test_auto_promote(promoted, bound, cached, expected, world):
    rows = [published(t) for t in (T, T1)]
    rows.append(published(T2, pre=True))  # a newer rc is never auto-promoted
    rows.append(published("v0.1.0", package=None))  # nor a release without a .deb
    w = world(ReleaseListing((), None, unchanged=True), releases=rows, promoted=promoted,
              bound=bound)
    if cached:
        produced(w, deb(T1))
    sync(w)
    assert w.reads.promoted() == expected


def test_suppressed_auto_promote_with_nothing_promoted_says_why(caplog, world):
    w = world(ReleaseListing((), None, unchanged=True), releases=[published(T1)],
              bound=True)
    produced(w, deb(T1))
    with caplog.at_level("WARNING", logger="central.content_catalog.sync"):
        sync(w)
    assert w.reads.promoted() is None
    assert "app_unconfigured until an operator promotes a release" in caplog.text


@pytest.mark.parametrize("include,expected", [(False, T), (True, T2)])
def test_auto_promote_honours_release_prereleases(include, expected, world):
    # Main: `select_latest_deployable(..., include_prereleases=PHOTO_WALL_RELEASE_PRERELEASES)`.
    rows = [published(T), published(T2, pre=True)]
    w = world(ReleaseListing((), None, unchanged=True), releases=rows,
              include_prereleases=include)
    sync(w)
    assert w.reads.promoted() == expected


def test_auto_promote_keeps_the_outgoing_tag_as_last_good_when_its_deb_is_on_disk(world):
    w = world(ReleaseListing((), None, unchanged=True),
              releases=[published(t) for t in (T1, T)], promoted=T1,
              on_disk=[deb(T1)])
    sync(w)
    assert (w.reads.promoted(), w.reads.last_good()) == (T, T1)


def test_auto_promote_with_no_deployable_release_leaves_nothing_promoted(world):
    w = world(listing(published(T1, package=None)))
    sync(w)
    assert w.reads.promoted() is None


def test_auto_promoted_deb_is_fetched_when_it_changed(world):
    # A new release lands on a fleet with no bound players: it is promoted in the tail, and its
    # changed .deb is therefore desired in the same transaction and fetched.
    w = world(listing(published(T1), published(T2, os_image=None)),
              releases=[published(T1)], promoted=T1,
              devices=[dev("frontier", known_good_tag=T1)])
    sync(w)
    assert w.reads.promoted() == T2
    assert FetchPackage(sha256=deb(T2).sha256) in [call.job for call in w.publisher.calls]
