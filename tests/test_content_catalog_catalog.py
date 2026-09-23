"""`ReleaseCatalog` against the real repositories and the `RecordingPublisher`.

PostgreSQL (the `registry` fixture; CI runs it): the releases, devices and Asset records are the
real tables, and disk presence is `DiskStoredAssets` over a `tmp_path` cache.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from content_db import (
    Reads,
    RecordingTransactions,
    insert_device,
    seed_releases,
    sha,
    store_deb,
)
from fakes.publisher import PublishedCall, RecordingPublisher

from central import netboot_base
from central.assets.layout import CacheLayout
from central.assets.store import CacheStore
from central.content_catalog.catalog import (
    CatalogError,
    DevicePackage,
    ManifestRefusal,
    NetbootView,
    ReleaseCatalog,
    ReleaseView,
    device_id_for_serial,
    sanitize_serial,
)
from central.content_catalog.ports import DeviceRow, ReleaseRow
from central.infra.asset_records import PgAssetRecords
from central.infra.catalog_records import PgDeviceRecords, PgReleaseRecords
from central.infra.stored_assets import DiskStoredAssets
from central.kernel.assets import OriginLocator
from central.kernel.job_types import FetchOsImage, FetchPackage, Prefetch, SyncReleases
from central.kernel.ports import (
    Candidates,
    ContentCatalog,
    NetbootBaseRequest,
    PackageRequest,
    Unknown,
)
from contracts.equipment import equipment_device_id
from contracts.time import ManualClock

T1, T, T2 = "v0.0.1", "v0.0.2", "v0.0.3"
SERIAL = "10000000abcd0010"
DEVICE_ID = equipment_device_id("pi", SERIAL.encode())


def release(tag: str, *, pre: bool = False, deb: bool = True, image: bool = True,
            divergent: bool = False) -> ReleaseRow:
    package = OriginLocator(f"https://example.test/{tag}.deb", sha("deb" + tag), 10) if deb else None
    os_image = (OriginLocator(f"https://example.test/{tag}.tgz", sha("img" + tag), 64)
                if image else None)
    return ReleaseRow(tag, pre, package, os_image, divergent)


def deb_sha(tag: str) -> str:
    return sha("deb" + tag)


def device(device_id: str, **fields) -> DeviceRow:
    """The row `insert_device(..., **fields)` stores."""
    return DeviceRow(device_id, fields.get("serial"), fields.get("attached_tag"),
                     fields.get("known_good_tag"), fields.get("last_served_tag"),
                     fields.get("boot_outcome"), fields.get("failed_tag"),
                     fields.get("last_served_at"), fields.get("retired", False))


class Devices(PgDeviceRecords):
    """The real repository, recording which device rows a resolve locked."""

    def __init__(self) -> None:
        self.locked: list[str] = []

    def lock(self, tx, device_id, serial, *, now):
        self.locked.append(device_id)
        return super().lock(tx, device_id, serial, now=now)


@dataclass
class World:
    catalog: ReleaseCatalog
    devices: Devices
    transactions: RecordingTransactions
    publisher: RecordingPublisher
    clock: ManualClock
    reads: Reads
    db: object

    def device_count(self) -> int:
        with self.db.transaction() as conn:
            return conn.execute("SELECT count(*) AS n FROM devices").fetchone()["n"]


@pytest.fixture
def world(registry, tmp_path):
    def build(releases=(), devices=(), *, promoted=None, last_good=None, on_disk=()) -> World:
        clock = ManualClock(1000.0)
        seeding = RecordingTransactions(registry.db)
        seed_releases(seeding, releases, promoted=promoted, last_good=last_good)
        for fields in devices:
            insert_device(registry.db, **fields)
        assets = PgAssetRecords(clock)
        store = CacheStore(CacheLayout(tmp_path))
        by_tag = {row.tag: row for row in releases}
        for tag in on_disk:
            store_deb(seeding, assets, store, deb_sha(tag), by_tag[tag].package.size)
        transactions = RecordingTransactions(registry.db)
        publisher = RecordingPublisher(clock)
        devices_records = Devices()
        catalog = ReleaseCatalog(releases=PgReleaseRecords(), devices=devices_records,
                                 stored=DiskStoredAssets(records=assets, store=store),
                                 transactions=transactions, publisher=publisher, clock=clock)
        return World(catalog, devices_records, transactions, publisher, clock,
                     Reads(RecordingTransactions(registry.db)), registry.db)

    return build


def dev(device_id: str, **fields) -> dict:
    """The keyword form `world(devices=...)` inserts."""
    return {"device_id": device_id, **fields}


def run(coro):
    return asyncio.run(coro)


def netboot(w: World, serial: str | None = SERIAL):
    return run(w.catalog.resolve(NetbootBaseRequest(serial)))


def os_images(*tags: str) -> tuple[FetchOsImage, ...]:
    return tuple(FetchOsImage(tag=tag) for tag in tags)


def test_release_catalog_is_a_content_catalog(world):
    catalog: ContentCatalog = world().catalog  # static conformance
    assert callable(catalog.resolve) and callable(catalog.desired_assets)


# -- B2 resolve: netboot base -------------------------------------------------------------------


def test_empty_catalog_is_unknown_no_release_but_the_device_row_is_upserted(world):
    w = world()
    assert netboot(w) == Unknown("no_release")
    row = w.reads.device(DEVICE_ID)
    assert row.serial == SERIAL
    with w.db.transaction() as conn:
        seen = conn.execute("SELECT first_seen, last_seen FROM devices WHERE device_id=%s",
                            (DEVICE_ID,)).fetchone()
    assert (seen["first_seen"], seen["last_seen"]) == (1000.0, 1000.0)


def test_fresh_device_bootstraps_newest_full_release_with_an_image_in_one_transaction(world):
    w = world([release(T1), release(T), release(T2, pre=True), release("v0.0.4", image=False)])
    assert netboot(w) == Candidates(os_images(T, T1), pinned=False)  # T1: an older substitute
    assert [tx.state for tx in w.transactions.begun] == ["committed"]
    assert w.devices.locked == [DEVICE_ID]


def test_resolve_never_records_served_that_is_the_200_path_only(world):
    # Tracer probe: a (possibly 503) resolve leaves no boot record.
    w = world([release(T)])
    netboot(w)
    row = w.reads.device(DEVICE_ID)
    assert row.last_served_tag is None and row.boot_outcome is None


def test_frontier_of_active_known_goods_beats_bootstrap_and_ignores_retired(world):
    w = world([release(T1), release(T), release(T2)],
              [dev("device-a", known_good_tag=T),
               dev("device-retired", known_good_tag=T2, retired=True)])
    assert netboot(w) == Candidates(os_images(T, T1), pinned=False)


def test_unpinned_device_may_get_the_newest_ready_eligible_version_not_only_its_known_good(world):
    # Owner ruling: unpinned may be served the newest ready eligible version while `desired` is
    # fetched. Eligible = full, with an OS image, older than desired (never the rc, never newer).
    tags = ["v0.1.0", "v0.2.0", "v0.3.0", "v0.4.0"]
    w = world([*(release(t) for t in tags), release("v0.3.5-rc.1", pre=True)],
              [dev("frontier", known_good_tag="v0.3.0"),
               dev(DEVICE_ID, known_good_tag="v0.1.0")])
    assert netboot(w) == Candidates(os_images("v0.3.0", "v0.2.0", "v0.1.0"), pinned=False)


def test_pinned_device_never_gets_a_substitute(world):
    w = world([release(T1), release(T), release(T2)],
              [dev("frontier", known_good_tag=T2), dev(DEVICE_ID, attached_tag=T)])
    assert netboot(w) == Candidates(os_images(T), pinned=True)


def test_unpinned_device_gets_its_known_good_as_a_substitute(world):
    w = world([release(T1), release(T2)],
              [dev("frontier", known_good_tag=T2), dev(DEVICE_ID, known_good_tag=T1)])
    assert netboot(w) == Candidates(os_images(T2, T1), pinned=False)


def test_candidates_without_an_os_image_are_dropped(world):
    w = world([release(T1, image=False), release(T2)],
              [dev("frontier", known_good_tag=T2), dev(DEVICE_ID, known_good_tag=T1)])
    assert netboot(w) == Candidates(os_images(T2), pinned=False)


def test_nothing_left_after_filtering_is_unknown_no_release(world):
    w = world([release(T2, image=False)], [dev("frontier", known_good_tag=T2)])
    assert netboot(w) == Unknown("no_release")


def test_pin_resolves_pinned_to_the_pin(world):
    w = world([release(T1), release(T2)],
              [dev("frontier", known_good_tag=T2), dev(DEVICE_ID, attached_tag=T1)])
    assert netboot(w) == Candidates(os_images(T1), pinned=True)


def test_pin_without_an_os_image_is_unknown_no_release(world):
    w = world([release(T1, image=False), release(T2)], [dev(DEVICE_ID, attached_tag=T1)])
    assert netboot(w) == Unknown("no_release")


def test_detect_writes_the_fence_and_recovers_to_known_good(world):
    w = world([release(T1), release(T)],
              [dev("frontier", known_good_tag=T),
               dev(DEVICE_ID, known_good_tag=T1, last_served_tag=T, boot_outcome="pending",
                      last_served_at=900.0)])
    assert netboot(w) == Candidates(os_images(T1), pinned=False)
    row = w.reads.device(DEVICE_ID)
    assert row.failed_tag == T and row.boot_outcome == "failed"


def test_pin_clears_the_fence(world):
    w = world([release(T1), release(T)],
              [dev(DEVICE_ID, attached_tag=T1, failed_tag=T, known_good_tag=T1)])
    assert netboot(w) == Candidates(os_images(T1), pinned=True)
    assert w.reads.device(DEVICE_ID).failed_tag is None


@pytest.mark.parametrize("serial", [None, "", "bad serial!", "x" * 129, "ABC:DEF"])
def test_absent_or_unsafe_serial_serves_desired_and_creates_no_row(serial, world):
    # "ABC:DEF" passes _SAFE_SERIAL but equipment_device_id refuses it: still no row.
    w = world([release(T)])
    assert netboot(w, serial) == Candidates(os_images(T), pinned=False)
    assert w.device_count() == 0


class _CountingDevices(Devices):
    def active(self, tx):
        raise AssertionError("a request path read every device row")


def test_request_paths_never_read_every_device_row(world):
    # Fake serials mint device rows, so neither netboot nor a package request may scan them all.
    w = world([release(T1), release(T2)], promoted=T2)
    w.catalog._devices = _CountingDevices()
    netboot(w)
    run(w.catalog.resolve(PackageRequest(deb_sha(T1))))
    run(w.catalog.desired_assets())


def test_sanitize_serial_is_the_ported_safe_charset():
    assert sanitize_serial(SERIAL) == SERIAL
    assert sanitize_serial("a:b_c.d-e") == "a:b_c.d-e"
    assert sanitize_serial("bad serial") is None
    assert sanitize_serial(None) is None
    assert sanitize_serial("x" * 129) is None


def test_the_serial_rule_and_device_id_have_one_home():
    # The netboot module kept a second copy that only the e2e tests used (PR #22 review).
    assert device_id_for_serial(SERIAL) == DEVICE_ID
    assert device_id_for_serial("ABC:DEF") is None  # safe charset, refused by the derivation
    assert not hasattr(netboot_base, "sanitize_serial")
    assert not hasattr(netboot_base, "device_id_for_serial")


def test_record_served_records_the_served_tag_pending_at_now(world):
    w = world([release(T1)], [dev(DEVICE_ID)])
    w.clock.advance(5)
    run(w.catalog.record_served(NetbootBaseRequest(SERIAL), FetchOsImage(tag=T1)))
    row = w.reads.device(DEVICE_ID)
    assert (row.last_served_tag, row.boot_outcome, row.last_served_at) == (T1, "pending", 1005.0)


def test_record_served_for_an_unsafe_serial_writes_nothing(world):
    w = world([release(T1)])
    run(w.catalog.record_served(NetbootBaseRequest("bad serial"), FetchOsImage(tag=T1)))
    assert w.device_count() == 0 and w.transactions.begun == []


# -- B2 resolve: package ------------------------------------------------------------------------


def test_package_request_for_a_desired_sha_is_a_pinned_single_candidate(world):
    w = world([release(T1), release(T2)])  # T2 is the bootstrap target: its .deb is desired
    resolution = run(w.catalog.resolve(PackageRequest(deb_sha(T2))))
    assert resolution == Candidates((FetchPackage(sha256=deb_sha(T2)),), pinned=True)


@pytest.mark.parametrize("devices,policy", [
    ([dev("d", attached_tag=T1)], {}),
    ([dev("d", known_good_tag=T1)], {}),
    ([dev("d", last_served_tag=T1)], {}),
    ([], {"promoted": T1}),
    ([], {"promoted": T2, "last_good": T1}),
])
def test_package_request_for_a_sha_desired_by_any_source_resolves(devices, policy, world):
    w = world([release(T1), release(T2)], devices, **policy)
    assert run(w.catalog.resolve(PackageRequest(deb_sha(T1)))) == Candidates(
        (FetchPackage(sha256=deb_sha(T1)),), pinned=True)
    assert FetchPackage(sha256=deb_sha(T1)) in run(w.catalog.desired_assets())  # same rule


def test_package_request_for_a_known_undesired_sha_not_on_disk_is_unknown(world):
    # P1: an unauthenticated client must not make Central fetch any historical .deb.
    w = world([release(T1), release(T2)], [dev("retired", attached_tag=T1, retired=True)])
    assert run(w.catalog.resolve(PackageRequest(deb_sha(T1)))) == Unknown("unknown_package")
    assert w.publisher.calls == []


def test_package_request_for_a_known_undesired_sha_on_disk_is_served(world):
    w = world([release(T1), release(T2)], on_disk=[T1])
    assert run(w.catalog.resolve(PackageRequest(deb_sha(T1)))) == Candidates(
        (FetchPackage(sha256=deb_sha(T1)),), pinned=True)


def test_package_request_for_an_unknown_sha_is_unknown_package(world):
    w = world([release(T1)])
    assert run(w.catalog.resolve(PackageRequest("f" * 64))) == Unknown("unknown_package")


@pytest.mark.parametrize("on_disk", [False, True])
def test_a_frozen_tags_deb_is_desired_and_resolved_only_while_on_disk(world, on_disk):
    # A divergent tag's .deb was re-cut upstream, so its URL serves other bytes. Once the file
    # is gone, a fetch can only fail download_corrupt, and Prefetch used to retry it for ever.
    w = world([release(T1, divergent=True)], [dev("d", last_served_tag=T1)], promoted=T1,
              on_disk=[T1] if on_disk else [])
    job = FetchPackage(sha256=deb_sha(T1))
    desired = run(w.catalog.desired_assets())
    assert (job in desired) is on_disk
    assert FetchOsImage(tag=T1) in desired  # the tag's OS image is unaffected
    assert run(w.catalog.resolve(PackageRequest(deb_sha(T1)))) == (
        Candidates((job,), pinned=True) if on_disk else Unknown("unknown_package"))


def test_a_frozen_deb_that_a_live_tag_also_ships_stays_desired(world):
    shared = release(T1).package
    w = world([release(T1, divergent=True), ReleaseRow(T2, False, shared, None)], promoted=T2)
    job = FetchPackage(sha256=deb_sha(T1))
    assert job in run(w.catalog.desired_assets())
    assert run(w.catalog.resolve(PackageRequest(deb_sha(T1)))) == Candidates((job,), pinned=True)


# -- B3 desired_assets --------------------------------------------------------------------------


def test_desired_assets_is_pins_known_goods_target_and_promoted_deb(world):
    tags = ["v0.1.0", "v0.2.0", "v0.3.0", "v0.4.0", "v0.5.0", "v0.6.0"]
    pin, kg, frontier, promoted, retired_pin, unrelated = tags
    served, last_good = "v0.0.8", "v0.0.9"
    w = world([*(release(t) for t in tags), release(served), release(last_good)],
              [dev("a", attached_tag=pin, known_good_tag=kg),
               dev("b", known_good_tag=frontier, last_served_tag=served),
               dev("r", attached_tag=retired_pin, retired=True)],
              promoted=promoted, last_good=last_good)
    desired = run(w.catalog.desired_assets())
    assert desired == frozenset(
        [*os_images(pin, kg, frontier, served),
         *(FetchPackage(sha256=deb_sha(t))
           for t in (pin, kg, frontier, served, promoted, last_good))])
    assert unrelated not in {getattr(job, "tag", None) for job in desired}


def test_desired_assets_bootstraps_when_the_frontier_is_empty(world):
    w = world([release(T1), release(T2, pre=True)])
    assert run(w.catalog.desired_assets()) == frozenset(
        [FetchOsImage(tag=T1), FetchPackage(sha256=deb_sha(T1))])


def test_desired_assets_skips_missing_locators(world):
    # (A device naming an unknown tag cannot exist: `devices` has foreign keys to app_releases.)
    w = world([release(T1, deb=False), release(T2, image=False)],
              [dev("a", attached_tag=T1, known_good_tag=T2)],
              promoted=T1)
    assert run(w.catalog.desired_assets()) == frozenset(
        [FetchOsImage(tag=T1), FetchPackage(sha256=deb_sha(T2))])


def test_desired_assets_of_an_empty_catalog_is_empty(world):
    assert run(world().catalog.desired_assets()) == frozenset()


# -- B4 device_package / promoted_package -------------------------------------------------------


def test_device_package_rides_the_served_tag(world):
    w = world([release(T1), release(T2)], [dev(DEVICE_ID, last_served_tag=T1)], promoted=T2)
    assert run(w.catalog.device_package(SERIAL)) == DevicePackage(T1, T1, deb_sha(T1), 10)


@pytest.mark.parametrize("serial,devices", [
    (None, ()),
    ("bad serial", ()),
    (SERIAL, ()),
    (SERIAL, (dev(DEVICE_ID),)),
])
def test_device_package_without_a_carried_tag_is_unresolved(serial, devices, world):
    w = world([release(T1)], devices, promoted=T1)
    assert run(w.catalog.device_package(serial)) == ManifestRefusal("app_manifest_unresolved")


def test_device_package_for_a_release_without_a_deb_is_undeployable(world):
    w = world([release(T1, deb=False)], [dev(DEVICE_ID, last_served_tag=T1)])
    assert run(w.catalog.device_package(SERIAL)) == ManifestRefusal("app_manifest_undeployable")


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
def test_promoted_package_falls_back_to_the_last_good_while_the_promoted_is_absent(on_disk, expected, world):
    w = world([release(T1), release(T2)], promoted=T2, last_good=T1,
              on_disk=on_disk)
    assert run(w.catalog.promoted_package()) == DevicePackage(
        expected, expected, deb_sha(expected), 10)


@pytest.mark.parametrize("outgoing_on_disk,last_good", [(True, T1), (False, "v0.0.0")])
def test_promote_keeps_the_outgoing_tag_as_last_good_only_when_its_deb_is_on_disk(outgoing_on_disk, last_good, world):
    w = world([release("v0.0.0"), release(T1), release(T2)], promoted=T1, last_good="v0.0.0",
              on_disk=[T1] if outgoing_on_disk else [])
    run(w.catalog.promote(T2))
    assert (w.reads.promoted(), w.reads.last_good()) == (T2, last_good)


# -- B5 operator actions ------------------------------------------------------------------------


def _refused(w: World, action, code: str, kind: str) -> None:
    with pytest.raises(CatalogError) as caught:
        run(action)
    assert (caught.value.code, caught.value.kind) == (code, kind)
    assert w.publisher.calls == []


def test_pin_unknown_tag_is_release_not_found_with_no_write(world):
    w = world([release(T1)], [dev(DEVICE_ID)])
    _refused(w, w.catalog.pin(DEVICE_ID, "v9.9.9"), "release_not_found", "not_found")
    assert w.reads.device(DEVICE_ID).attached_tag is None
    assert [tx.state for tx in w.transactions.begun] == ["rolled_back"]


def test_pin_malformed_tag_is_invalid(world):
    w = world([release(T1)], [dev(DEVICE_ID)])
    _refused(w, w.catalog.pin(DEVICE_ID, "1.0"), "invalid_tag", "invalid")


def test_pin_unknown_device_is_device_not_found_with_no_publish(world):
    w = world([release(T1)])
    _refused(w, w.catalog.pin("device-nope", T1), "device_not_found", "not_found")
    assert w.device_count() == 0


def test_pin_sets_the_pin_and_publishes_both_fetches_and_prefetch_in_its_transaction(world):
    w = world([release(T1)], [dev(DEVICE_ID)])
    run(w.catalog.pin(DEVICE_ID, T1))
    assert w.reads.device(DEVICE_ID).attached_tag == T1
    (tx,) = w.transactions.begun
    assert tx.state == "committed"
    assert w.publisher.calls == [
        PublishedCall(FetchOsImage(tag=T1), True, tx),
        PublishedCall(FetchPackage(sha256=deb_sha(T1)), True, tx),
        PublishedCall(Prefetch(), False, tx),
    ]


def test_pin_to_a_release_without_a_deb_fetches_only_the_os_image(world):
    w = world([release(T1, deb=False)], [dev(DEVICE_ID)])
    run(w.catalog.pin(DEVICE_ID, T1))
    assert [call.job for call in w.publisher.calls] == [FetchOsImage(tag=T1), Prefetch()]


def test_unpin(world):
    w = world([release(T1)], [dev(DEVICE_ID, attached_tag=T1)])
    run(w.catalog.unpin(DEVICE_ID))
    assert w.reads.device(DEVICE_ID).attached_tag is None
    assert [call.job for call in w.publisher.calls] == [Prefetch()]


def test_unpin_unknown_device_publishes_nothing(world):
    w = world()
    _refused(w, w.catalog.unpin("device-nope"), "device_not_found", "not_found")


def test_promote_refusals_write_nothing(world):
    w = world([release(T1, deb=False)], promoted=None)
    _refused(w, w.catalog.promote("v9.9.9"), "release_not_found", "not_found")
    _refused(w, w.catalog.promote(T1), "release_undeployable", "conflict")
    _refused(w, w.catalog.promote("nope"), "invalid_tag", "invalid")
    assert w.reads.promoted() is None


def test_promote_sets_the_pointer_and_fetches_its_deb(world):
    w = world([release(T1), release(T2)], promoted=T2)
    run(w.catalog.promote(T1))
    assert w.reads.promoted() == T1
    (tx,) = w.transactions.begun
    assert w.publisher.calls == [
        PublishedCall(FetchPackage(sha256=deb_sha(T1)), True, tx),
        PublishedCall(Prefetch(), False, tx),
    ]


def test_refresh_publishes_a_sync_now_retrying_a_terminal_outcome(world):
    w = world()
    run(w.catalog.refresh())
    assert w.publisher.calls == [
        PublishedCall(SyncReleases(), True, None),
        PublishedCall(Prefetch(), False, None),
    ]
    assert w.transactions.begun == []  # publish_now owns its own transaction


# -- views --------------------------------------------------------------------------------------


def test_releases_view_is_semver_desc_with_flags(world):
    w = world([release("v0.9.0"), release("v0.10.0", deb=False),
               release("v0.10.0-rc.1", pre=True, image=False)], promoted="v0.9.0")
    assert run(w.catalog.releases_view()) == (
        ReleaseView("v0.10.0", False, False, False, True),
        ReleaseView("v0.10.0-rc.1", True, True, False, False),
        ReleaseView("v0.9.0", False, True, True, True),
    )


def test_netboot_view_is_the_frontier_and_active_devices(world):
    w = world([release(T1), release(T2), release("v9.0.0")],
              [dev("a", known_good_tag=T1), dev("b", known_good_tag=T2),
               dev("r", known_good_tag="v9.0.0", retired=True)])
    assert run(w.catalog.netboot_view()) == NetbootView(
        T2, (device("a", known_good_tag=T1), device("b", known_good_tag=T2)))
