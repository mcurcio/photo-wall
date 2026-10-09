"""`ReleaseCatalog` against the real repositories and the `RecordingPublisher`.

PostgreSQL (the `registry` fixture; CI runs it): the releases and Asset records are the real
tables, and disk presence is `DiskStoredAssets` over a `tmp_path` cache.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from content_db import Reads, RecordingTransactions, seed_releases, sha, store_deb
from fakes.publisher import PublishedCall, RecordingPublisher

from central.assets.layout import CacheLayout
from central.assets.store import CacheStore
from central.content_catalog.catalog import (
    CatalogError,
    DevicePackage,
    ManifestRefusal,
    ReleaseCatalog,
    ReleaseView,
    device_id_for_serial,
    sanitize_serial,
)
from central.content_catalog.ports import Promotion, ReleaseRow, StoredEtag
from central.infra.asset_records import PgAssetRecords
from central.infra.catalog_records import PgReleaseRecords
from central.infra.stored_assets import DiskStoredAssets
from central.kernel.assets import OriginLocator
from central.kernel.job_types import FetchPackage, Prefetch, SyncReleases
from central.kernel.ports import Candidates, ContentCatalog, PackageRequest, Unknown
from contracts.equipment import equipment_device_id
from contracts.time import ManualClock

T1, T, T2 = "v0.0.1", "v0.0.2", "v0.0.3"
SERIAL = "10000000abcd0010"
DEVICE_ID = equipment_device_id("pi", SERIAL.encode())


def release(tag: str, *, pre: bool = False, deb: bool = True, image: bool = True) -> ReleaseRow:
    package = OriginLocator(f"https://example.test/{tag}.deb", sha("deb" + tag), 10) if deb else None
    os_image = (OriginLocator(f"https://example.test/{tag}.tgz", sha("img" + tag), 64)
                if image else None)
    return ReleaseRow(tag, pre, package, os_image)


def deb_sha(tag: str) -> str:
    return sha("deb" + tag)


@dataclass
class World:
    catalog: ReleaseCatalog
    transactions: RecordingTransactions
    publisher: RecordingPublisher
    clock: ManualClock
    reads: Reads


@pytest.fixture
def world(registry, tmp_path):
    def build(releases=(), *, promoted=None, promoted_by="operator", last_good=None,
              on_disk=(), etag=None) -> World:
        clock = ManualClock(1000.0)
        seeding = RecordingTransactions(registry.db)
        seed_releases(seeding, releases, promoted=promoted, promoted_by=promoted_by,
                      last_good=last_good, etag=etag)
        assets = PgAssetRecords(clock)
        store = CacheStore(CacheLayout(tmp_path))
        by_tag = {row.tag: row for row in releases}
        for tag in on_disk:
            store_deb(seeding, assets, store, deb_sha(tag), by_tag[tag].package.size)
        transactions = RecordingTransactions(registry.db)
        publisher = RecordingPublisher(clock)
        catalog = ReleaseCatalog(releases=PgReleaseRecords(),
                                 stored=DiskStoredAssets(records=assets, store=store),
                                 transactions=transactions, publisher=publisher, clock=clock)
        return World(catalog, transactions, publisher, clock,
                     Reads(RecordingTransactions(registry.db)))

    return build


def run(coro):
    return asyncio.run(coro)


def test_release_catalog_is_a_content_catalog(world):
    catalog: ContentCatalog = world().catalog  # static conformance
    assert callable(catalog.resolve) and callable(catalog.desired_assets)


def test_sanitize_serial_is_the_ported_safe_charset():
    assert sanitize_serial(SERIAL) == SERIAL
    assert sanitize_serial("a:b_c.d-e") == "a:b_c.d-e"
    assert sanitize_serial("bad serial") is None
    assert sanitize_serial(None) is None
    assert sanitize_serial("x" * 129) is None


def test_the_device_id_is_derived_from_a_safe_serial_only():
    assert device_id_for_serial(SERIAL) == DEVICE_ID
    assert device_id_for_serial("ABC:DEF") is None  # safe charset, refused by the derivation


# -- resolve: package ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", [{"promoted": T1}, {"promoted": T2, "last_good": T1}])
def test_package_request_for_a_policy_deb_resolves(policy, world):
    w = world([release(T1), release(T2)], **policy)
    assert run(w.catalog.resolve(PackageRequest(deb_sha(T1)))) == Candidates(
        (FetchPackage(sha256=deb_sha(T1)),), pinned=True)
    assert FetchPackage(sha256=deb_sha(T1)) in run(w.catalog.desired_assets())  # same rule


def test_package_request_for_a_known_undesired_sha_not_on_disk_is_unknown(world):
    # P1: an unauthenticated client must not make Central fetch any historical .deb.
    w = world([release(T1), release(T2)], promoted=T2)
    assert run(w.catalog.resolve(PackageRequest(deb_sha(T1)))) == Unknown("unknown_package")
    assert w.publisher.calls == []


def test_package_request_for_a_known_undesired_sha_on_disk_is_served(world):
    w = world([release(T1), release(T2)], on_disk=[T1])
    assert run(w.catalog.resolve(PackageRequest(deb_sha(T1)))) == Candidates(
        (FetchPackage(sha256=deb_sha(T1)),), pinned=True)


def test_package_request_for_an_unknown_sha_is_unknown_package(world):
    w = world([release(T1)])
    assert run(w.catalog.resolve(PackageRequest("f" * 64))) == Unknown("unknown_package")


# -- desired_assets -----------------------------------------------------------------------------


def test_desired_assets_is_the_promoted_and_last_good_debs_and_no_release_os_image(world):
    # A release's own OS image is desired by no V1 netboot any more: node deployments name the
    # bases they boot (`fleet_desired_assets`).
    w = world([release(T1), release(T), release(T2)], promoted=T2, last_good=T1)
    assert run(w.catalog.desired_assets()) == frozenset(
        [FetchPackage(sha256=deb_sha(T1)), FetchPackage(sha256=deb_sha(T2))])


def test_desired_assets_skips_a_policy_tag_without_a_deb(world):
    w = world([release(T1, deb=False)], promoted=T1)
    assert run(w.catalog.desired_assets()) == frozenset()


def test_desired_assets_of_an_empty_catalog_is_empty(world):
    assert run(world().catalog.desired_assets()) == frozenset()


# -- promoted_package ---------------------------------------------------------------------------


@pytest.mark.parametrize("deb,promoted,expected", [
    (True, T1, DevicePackage(T1, T1, deb_sha(T1), 10)),
    (True, None, ManifestRefusal("app_unconfigured")),
    (False, T1, ManifestRefusal("app_unconfigured")),
])
def test_promoted_package(deb, promoted, expected, world):
    w = world([release(T1, deb=deb)], promoted=promoted)
    assert run(w.catalog.promoted_package()) == expected


@pytest.mark.parametrize("on_disk,expected", [
    ((T2, T1), T2),   # the promoted .deb is on disk: it is served
    ((T1,), T1),      # promoted still fetching / failed: the last-good keeps serving (main)
    ((), T2),         # neither on disk: the promoted one (the package route fetches it)
])
def test_promoted_package_falls_back_to_the_last_good_while_the_promoted_is_absent(
        on_disk, expected, world):
    w = world([release(T1), release(T2)], promoted=T2, last_good=T1, on_disk=on_disk)
    assert run(w.catalog.promoted_package()) == DevicePackage(
        expected, expected, deb_sha(expected), 10)


@pytest.mark.parametrize("outgoing_on_disk,last_good", [(True, T1), (False, "v0.0.0")])
def test_promote_keeps_the_outgoing_tag_as_last_good_only_when_its_deb_is_on_disk(
        outgoing_on_disk, last_good, world):
    w = world([release("v0.0.0"), release(T1), release(T2)], promoted=T1, last_good="v0.0.0",
              on_disk=[T1] if outgoing_on_disk else [])
    run(w.catalog.promote(T2))
    assert (w.reads.promoted(), w.reads.last_good()) == (T2, last_good)


# -- operator actions ---------------------------------------------------------------------------


def _refused(w: World, action, code: str, kind: str) -> None:
    with pytest.raises(CatalogError) as caught:
        run(action)
    assert (caught.value.code, caught.value.kind) == (code, kind)
    assert w.publisher.calls == []


def test_promote_refusals_write_nothing(world):
    w = world([release(T1, deb=False)], promoted=None)
    _refused(w, w.catalog.promote("v9.9.9"), "release_not_found", "not_found")
    _refused(w, w.catalog.promote(T1), "release_undeployable", "conflict")
    _refused(w, w.catalog.promote("nope"), "invalid_tag", "invalid")
    assert w.reads.promoted() is None


def test_promote_sets_the_pointer_and_fetches_its_deb(world):
    # A promotion the retired auto-promote recorded is the operator's once promoted again.
    w = world([release(T1), release(T2)], promoted=T2, promoted_by="auto")
    run(w.catalog.promote(T1))
    assert w.reads.promotion() == Promotion(T1, "operator")
    (tx,) = w.transactions.begun
    assert w.publisher.calls == [
        PublishedCall(FetchPackage(sha256=deb_sha(T1)), True, tx),
        PublishedCall(Prefetch(), False, tx),
    ]


def test_refresh_clears_the_etag_and_publishes_a_sync_retrying_a_terminal_outcome(world):
    # One transaction clears the ETag and publishes the sync, so the sync lists in full
    # (design §6.4: the operator's repair of a stale equal-version observation).
    w = world(etag='W/"e1"')
    run(w.catalog.refresh())
    (tx,) = w.transactions.begun
    assert tx.state == "committed"
    assert w.reads.stored_etag() == StoredEtag(None, w.clock.utc())
    assert w.publisher.calls == [
        PublishedCall(SyncReleases(), True, tx),
        PublishedCall(Prefetch(), False, None),  # publish_now owns its own transaction
    ]


# -- views --------------------------------------------------------------------------------------


@pytest.mark.parametrize("by", ["operator", "auto"])
def test_releases_view_is_semver_desc_with_flags_and_who_promoted(world, by):
    w = world([release("v0.9.0"), release("v0.10.0", deb=False),
               release("v0.10.0-rc.1", pre=True, image=False)], promoted="v0.9.0",
              promoted_by=by)
    views = run(w.catalog.releases_view())
    assert views == (
        ReleaseView("v0.10.0", False, False, None, True),
        ReleaseView("v0.10.0-rc.1", True, True, None, False),
        ReleaseView("v0.9.0", False, True, by, True),
    )
    assert [view.promoted for view in views] == [False, False, True]
