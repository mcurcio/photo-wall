"""`SyncReleasesHandler` (B6) against `FakeReleaseOrigin`, `InMemoryAssetRecords` and the fakes."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import timedelta

import pytest
from catalog_fakes import (
    InMemoryDeviceRecords,
    InMemoryReleaseRecords,
    InMemoryStoredAssets,
    device,
    release_row,
)
from fakes.asset_records import InMemoryAssetRecords
from fakes.origin import FakeReleaseOrigin
from fakes.publisher import PublishedCall, RecordingPublisher
from fakes.transactions import FakeTransaction, FakeTransactions

from central.content_catalog.catalog import ReleaseCatalog
from central.content_catalog.sync import SyncReleasesHandler
from central.kernel.assets import AssetKey, AssetKind, AssetReady, AssetReference, OriginLocator
from central.kernel.handling import OriginUnavailable, handler_job_type
from central.kernel.job_types import FetchOsImage, FetchPackage, Prefetch, SyncReleases
from central.kernel.ports import PublishedRelease, ReleaseListing
from contracts.time import ManualClock

T1, T, T2 = "v0.0.1", "v0.0.2", "v0.0.3"
NOW = 10_000.0
THRESHOLD = NOW - 15 * 60  # last_served_at < this => stale


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def deb(tag: str, cut: str = "") -> OriginLocator:
    return OriginLocator(f"https://example.test/{tag}{cut}.deb", sha("deb" + tag + cut), 10)


def image(tag: str) -> OriginLocator:
    return OriginLocator(f"https://example.test/{tag}.tgz", sha("img" + tag), 64)


def published(tag: str, *, pre: bool = False, package: OriginLocator | None | str = "default",
              os_image: bool = True) -> PublishedRelease:
    locator = deb(tag) if package == "default" else package
    return PublishedRelease(tag, pre, locator, None if locator else "no_player_asset",
                            image(tag) if os_image else None)


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
    releases: InMemoryReleaseRecords
    devices: InMemoryDeviceRecords
    assets: InMemoryAssetRecords
    transactions: FakeTransactions
    publisher: RecordingPublisher
    stored: InMemoryStoredAssets


def world(origin_listing, *, releases=(), devices=(), promoted=None, etag=None, bound=0,
          assets: InMemoryAssetRecords | None = None, on_disk=(), **handler_options) -> World:
    clock = ManualClock(NOW)
    records = InMemoryReleaseRecords(tuple(releases), promoted=promoted, etag=etag, bound=bound)
    device_records = InMemoryDeviceRecords(tuple(devices))
    assets = assets or InMemoryAssetRecords()
    transactions = FakeTransactions()
    publisher = RecordingPublisher(clock)
    stored = InMemoryStoredAssets(on_disk)
    catalog = ReleaseCatalog(releases=records, devices=device_records, stored=stored,
                             transactions=transactions, publisher=publisher, clock=clock)
    origin = RecordingOrigin(origin_listing)
    handler = SyncReleasesHandler(origin=origin, releases=records, devices=device_records,
                                  assets=assets, transactions=transactions,
                                  publisher=publisher, catalog=catalog, clock=clock,
                                  **handler_options)
    return World(handler, origin, records, device_records, assets, transactions, publisher,
                 stored)


def sync(w: World) -> None:
    asyncio.run(w.handler.handle(SyncReleases()))


def image_key(tag: str) -> AssetKey:
    return AssetKey(AssetKind.OS_IMAGE, tag)


def deb_key(locator: OriginLocator) -> AssetKey:
    return AssetKey(AssetKind.PLAYER_DEB, locator.sha256)


def owners(w: World, key: AssetKey) -> list[str] | None:
    asset = w.assets.assets.get(key)
    return None if asset is None else [ref.owner for ref in asset.references]


def produced(w: World, locator: OriginLocator) -> None:
    """The worker's effect on the record: the `.deb`'s produced facts (it was mirrored)."""
    tx = FakeTransaction()
    w.assets.reference(tx, deb_key(locator), AssetReference(
        "seed", locator, locator.size, locator.sha256))
    w.assets.record_produced(tx, deb_key(locator), AssetReady(locator.size, locator.sha256))


def test_handler_serves_sync_releases():
    assert handler_job_type(world(listing()).handler) is SyncReleases


def test_first_sync_records_releases_references_promotes_and_fetches_desired_changes():
    w = world(listing(published(T1), published(T2, pre=True, os_image=False)), etag="e0")
    sync(w)
    assert w.origin.etags == ["e0"]
    assert w.releases.etag == "e1"
    assert set(w.releases.rows) == {T1, T2}
    assert w.releases.upserted_at == {T1: NOW, T2: NOW}
    # References: the image carries no expected facts; the .deb's equal its locator's.
    assert w.assets.assets[image_key(T1)].references == (
        AssetReference(T1, image(T1), None, None),)
    assert w.assets.assets[deb_key(deb(T1))].references == (
        AssetReference(T1, deb(T1), 10, deb(T1).sha256),)
    assert owners(w, deb_key(deb(T2))) == [T2]
    # Auto-promote: nothing promoted -> the newest full release with a .deb (not the rc).
    assert w.releases.promoted == T1
    # Load etag, one per release, store etag, tail: each its own committed transaction.
    assert [tx.state for tx in w.transactions.begun] == ["committed"] * 5
    tail = w.transactions.begun[-1]
    # Changed AND desired (bootstrap T1 + its .deb); the rc's .deb changed but is not desired.
    assert w.publisher.calls == [
        PublishedCall(FetchOsImage(tag=T1), True, tail),
        PublishedCall(FetchPackage(sha256=deb(T1).sha256), True, tail),
        PublishedCall(Prefetch(), False, tail),
    ]


def test_unchanged_listing_writes_no_release_but_still_runs_the_tail():
    w = world(ReleaseListing((), "e1", unchanged=True), releases=[release_row(published(T1))],
              promoted=T1, etag="e1", bound=2)
    sync(w)
    assert w.releases.upserted_at == {} and w.releases.etag == "e1"
    assert len(w.transactions.begun) == 2  # load etag + tail
    assert w.publisher.calls == [PublishedCall(Prefetch(), False, w.transactions.begun[-1])]


def test_resync_of_identical_facts_publishes_no_fetch():
    w = world(listing(published(T1)))
    sync(w)
    first = len(w.publisher.calls)
    w.origin.listing = listing(published(T1), etag="e2")
    sync(w)
    assert [call.job for call in w.publisher.calls[first:]] == [Prefetch()]
    assert w.releases.etag == "e2"


def test_recut_deb_retires_the_old_sha_and_fetches_the_new_one():
    w = world(listing(published(T1)))
    sync(w)
    first = len(w.publisher.calls)
    recut = deb(T1, cut="-recut")
    w.origin.listing = listing(published(T1, package=recut), etag="e2")
    sync(w)
    assert owners(w, deb_key(deb(T1))) is None  # its last reference retired -> row gone
    assert owners(w, deb_key(recut)) == [T1]
    assert [call.job for call in w.publisher.calls[first:]] == [
        FetchPackage(sha256=recut.sha256), Prefetch()]


def test_recut_of_a_produced_deb_freezes_the_tag_as_divergent():
    # Main froze a mirrored tag re-cut upstream (`upsert_discovered`: mirrored -> divergent):
    # the served bytes stay, the new sha is neither referenced nor fetched.
    w = world(listing(published(T1)))
    sync(w)
    produced(w, deb(T1))
    first = len(w.publisher.calls)
    recut = deb(T1, cut="-recut")
    w.origin.listing = listing(published(T1, package=recut), etag="e2")
    sync(w)
    row = w.releases.rows[T1]
    assert row.package == deb(T1) and row.divergent
    assert T1 in owners(w, deb_key(deb(T1))) and owners(w, deb_key(recut)) is None
    assert [call.job for call in w.publisher.calls[first:]] == [Prefetch()]
    # Frozen for good: a later sync, even one dropping the .deb, keeps the produced facts.
    w.origin.listing = listing(published(T1, package=None), etag="e3")
    sync(w)
    assert w.releases.rows[T1].package == deb(T1)
    assert T1 in owners(w, deb_key(deb(T1)))


def test_recut_keeps_a_deb_another_tag_still_ships():
    shared = deb(T1)
    w = world(listing(published(T1), published(T2, package=shared)))
    sync(w)
    assert sorted(owners(w, deb_key(shared))) == [T1, T2]
    w.origin.listing = listing(published(T1, package=deb(T1, cut="-recut")),
                               published(T2, package=shared), etag="e2")
    sync(w)
    assert owners(w, deb_key(shared)) == [T2]


def test_dropped_image_and_dropped_deb_retire_their_references():
    w = world(listing(published(T1)))
    sync(w)
    w.origin.listing = listing(published(T1, package=None, os_image=False), etag="e2")
    sync(w)
    assert owners(w, image_key(T1)) is None
    assert owners(w, deb_key(deb(T1))) is None


def test_origin_failure_propagates_and_writes_nothing():
    w = world(OriginUnavailable("github_down"), promoted=None)
    with pytest.raises(OriginUnavailable):
        sync(w)
    assert len(w.transactions.begun) == 1  # only the etag read
    assert w.releases.rows == {} and w.publisher.calls == []


def test_a_failing_release_transaction_keeps_the_etag_so_the_next_sync_retries():
    class Boom(InMemoryAssetRecords):
        def reference(self, tx, key, ref):
            raise RuntimeError("db down")

    w = world(listing(published(T1)), etag="e0", assets=Boom())
    with pytest.raises(RuntimeError):
        sync(w)
    assert w.releases.etag == "e0"
    assert w.transactions.begun[-1].state == "rolled_back"


# -- tail: the stale-pending sweep (ports recovery (f)) -----------------------------------------


def test_tail_sweeps_stale_pending_boots_with_the_e2_and_e2b_rules():
    old, recent = THRESHOLD - 1, THRESHOLD + 1
    w = world(ReleaseListing((), None, unchanged=True), devices=[
        device("device-stick", boot_outcome="pending", failed_tag=T, last_served_tag=T1,
               last_served_at=old),
        device("device-served-lower", boot_outcome="pending", last_served_tag=T,
               last_served_at=old),
        device("device-pinned", boot_outcome="pending", attached_tag=T1, last_served_tag=T,
               last_served_at=old),
        device("device-never", boot_outcome="pending", last_served_at=old),
        device("device-recent", boot_outcome="pending", last_served_tag=T2,
               last_served_at=recent),
        device("device-retired", boot_outcome="pending", last_served_tag=T2,
               last_served_at=old, retired=True),
        device("frontier", known_good_tag=T2),
    ])
    sync(w)
    rows = w.devices.rows
    assert (rows["device-stick"].failed_tag, rows["device-stick"].boot_outcome) == (T, "failed")
    assert rows["device-served-lower"].failed_tag == T  # the served tag, not the frontier T2
    assert rows["device-pinned"].failed_tag == T1  # COALESCE(attached_tag, last_served_tag)
    assert (rows["device-never"].failed_tag, rows["device-never"].boot_outcome) == (
        None, "failed")
    assert rows["device-recent"].boot_outcome == "pending"
    assert rows["device-retired"].boot_outcome == "pending"


def test_pending_health_timeout_is_configurable():
    w = world(ReleaseListing((), None, unchanged=True), devices=[
        device("d", boot_outcome="pending", last_served_tag=T, last_served_at=NOW - 61)],
        pending_health_timeout=timedelta(minutes=1))
    sync(w)
    assert w.devices.rows["d"].boot_outcome == "failed"


# -- tail: auto-promote -------------------------------------------------------------------------


@pytest.mark.parametrize("promoted,bound,cached,expected", [
    # Main's `_autopull_deb` (central/app_release_boot.py): suppress iff cached > 0 AND bound > 0.
    (None, 3, False, T),   # nothing cached: pull, even with bound players
    (T1, 3, False, T),
    (T1, 0, True, T),      # no bound player: follow the newest
    (None, 0, True, T),
    (T1, 3, True, T1),     # a configured fleet keeps the operator's choice
    (None, 3, True, None),  # ... and is never promoted behind the operator's back (P1)
])
def test_auto_promote(promoted, bound, cached, expected):
    rows = [release_row(published(t)) for t in (T, T1)]
    rows.append(release_row(published(T2, pre=True)))  # a newer rc is never auto-promoted
    rows.append(release_row(published("v0.1.0", package=None)))  # nor a release without a .deb
    w = world(ReleaseListing((), None, unchanged=True), releases=rows, promoted=promoted,
              bound=bound)
    if cached:
        produced(w, deb(T1))
    sync(w)
    assert w.releases.promoted == expected


def test_suppressed_auto_promote_with_nothing_promoted_says_why(caplog):
    w = world(ReleaseListing((), None, unchanged=True), releases=[release_row(published(T1))],
              bound=2)
    produced(w, deb(T1))
    with caplog.at_level("WARNING", logger="central.content_catalog.sync"):
        sync(w)
    assert w.releases.promoted is None
    assert "app_unconfigured until an operator promotes a release" in caplog.text


@pytest.mark.parametrize("include,expected", [(False, T), (True, T2)])
def test_auto_promote_honours_release_prereleases(include, expected):
    # Main: `select_latest_deployable(..., include_prereleases=PHOTO_WALL_RELEASE_PRERELEASES)`.
    rows = [release_row(published(T)), release_row(published(T2, pre=True))]
    w = world(ReleaseListing((), None, unchanged=True), releases=rows,
              include_prereleases=include)
    sync(w)
    assert w.releases.promoted == expected


def test_auto_promote_keeps_the_outgoing_tag_as_last_good_when_its_deb_is_on_disk():
    w = world(ReleaseListing((), None, unchanged=True),
              releases=[release_row(published(t)) for t in (T1, T)], promoted=T1,
              on_disk=[FetchPackage(sha256=deb(T1).sha256)])
    sync(w)
    assert (w.releases.promoted, w.releases.last_good) == (T, T1)


def test_auto_promote_with_no_deployable_release_leaves_nothing_promoted():
    w = world(listing(published(T1, package=None)))
    sync(w)
    assert w.releases.promoted is None


def test_auto_promoted_deb_is_fetched_when_it_changed():
    # A new release lands on a fleet with no bound players: it is promoted in the tail, and its
    # changed .deb is therefore desired in the same transaction and fetched.
    w = world(listing(published(T1), published(T2, os_image=False)),
              releases=[release_row(published(T1))], promoted=T1,
              devices=[device("frontier", known_good_tag=T1)])
    sync(w)
    assert w.releases.promoted == T2
    assert FetchPackage(sha256=deb(T2).sha256) in [call.job for call in w.publisher.calls]
