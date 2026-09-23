"""`PgReleaseRecords` / `PgDeviceRecords` round-trip every method against a migrated schema (B7).

The DB tests use the `registry` fixture, which skips without PHOTO_WALL_TEST_DATABASE_URL (CI runs
them). The SQL assertions are ported from `test_netboot_base_pin.py` and the sweep cases of
`test_netboot_base_recovery.py`. The fake-transaction guard runs without a database.
"""

from __future__ import annotations

import hashlib

import pytest
from fakes.transactions import FakeTransaction
from test_registry import enroll, frame

from central.content_catalog.ports import DeviceRow, DeviceUpdate, ReleaseRow
from central.infra.catalog_records import PgDeviceRecords, PgReleaseRecords
from central.infra.transactions import PgTransactions
from central.kernel.assets import OriginLocator
from central.kernel.ports import PublishedRelease

T1, T, T2 = "v0.0.1", "v0.0.2", "v0.0.3"
PENDING_HEALTH_TIMEOUT = 15 * 60


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def deb(tag: str, cut: str = "") -> OriginLocator:
    return OriginLocator(f"https://example.test/{tag}{cut}.deb", sha("deb" + tag + cut), 10)


def image(tag: str) -> OriginLocator:
    return OriginLocator(f"https://example.test/{tag}.tgz", sha("img" + tag), 64)


def published(tag: str, *, pre: bool = False, package: OriginLocator | None = None,
              has_deb: bool = True, has_image: bool = True) -> PublishedRelease:
    locator = package or (deb(tag) if has_deb else None)
    return PublishedRelease(tag, pre, locator, None if locator else "no_player_asset",
                            image(tag) if has_image else None)


def row(release: PublishedRelease) -> ReleaseRow:
    return ReleaseRow(release.tag, release.is_prerelease, release.package, release.os_image)


@pytest.fixture
def pg(registry):
    return PgTransactions(registry.db)


def _seed(pg, *releases: PublishedRelease) -> None:
    with pg.begin() as tx:
        for release in releases:
            PgReleaseRecords().upsert(tx, release, now=1000.0)


def _insert_device(registry, device_id, **cols):
    cols.setdefault("first_seen", 1000.0)
    cols.setdefault("last_seen", 1000.0)
    names = ["device_id", *cols]
    placeholders = ",".join(["%s"] * len(names))
    with registry.db.transaction() as conn:
        # Column names are test-controlled literals, never request input.
        conn.execute(f"INSERT INTO devices({','.join(names)}) VALUES({placeholders})",
                     (device_id, *cols.values()))


def _raw(registry, sql, params=()):
    with registry.db.transaction() as conn:
        return conn.execute(sql, params).fetchone()


# -- no database: a fake transaction handed to a real repository --------------------------------


@pytest.mark.parametrize("call", [
    lambda tx: PgReleaseRecords().get(tx, T1),
    lambda tx: PgReleaseRecords().all(tx),
    lambda tx: PgReleaseRecords().upsert(tx, published(T1), now=1.0),
    lambda tx: PgReleaseRecords().promoted_tag(tx),
    lambda tx: PgReleaseRecords().set_promoted(tx, T1),
    lambda tx: PgReleaseRecords().load_etag(tx),
    lambda tx: PgReleaseRecords().store_etag(tx, "e"),
    lambda tx: PgReleaseRecords().bound_player_count(tx),
    lambda tx: PgDeviceRecords().lock(tx, "d", "s", now=1.0),
    lambda tx: PgDeviceRecords().active(tx),
    lambda tx: PgDeviceRecords().get(tx, "d"),
    lambda tx: PgDeviceRecords().apply(tx, "d", DeviceUpdate(None, False)),
    lambda tx: PgDeviceRecords().record_served(tx, "d", T1, now=1.0),
    lambda tx: PgDeviceRecords().set_pin(tx, "d", T1),
    lambda tx: PgDeviceRecords().sweep_failed_boots(tx, served_before=1.0),
])
def test_a_fake_transaction_is_a_type_error(call):
    with pytest.raises(TypeError):
        call(FakeTransaction())


# -- releases -----------------------------------------------------------------------------------


def test_upsert_inserts_then_returns_the_previous_row(registry, pg):
    releases = PgReleaseRecords()
    first = published(T1)
    with pg.begin() as tx:
        assert releases.upsert(tx, first, now=1000.0) is None
        assert releases.get(tx, T1) == row(first)
    recut = published(T1, package=deb(T1, "-recut"), has_image=False)
    with pg.begin() as tx:
        assert releases.upsert(tx, recut, now=2000.0) == row(first)
        assert releases.get(tx, T1) == row(recut)  # base_* columns cleared with the image
    stored = _raw(registry, "SELECT major, minor, patch, prerelease, mirror_state, discovered_at, "
                            "updated_at, base_tarball_url FROM app_releases WHERE tag=%s", (T1,))
    assert (stored["major"], stored["minor"], stored["patch"], stored["prerelease"]) == (
        0, 0, 1, "")
    assert stored["mirror_state"] == "discovered"
    assert (stored["discovered_at"], stored["updated_at"]) == (1000.0, 2000.0)
    assert stored["base_tarball_url"] is None


def test_upsert_of_a_prerelease_without_a_deb_is_undeployable_legacy_state(registry, pg):
    rc = published("v1.0.0-rc.1", pre=True, has_deb=False)
    _seed(pg, rc)
    with pg.begin() as tx:
        assert PgReleaseRecords().get(tx, rc.tag) == row(rc)
    stored = _raw(registry, "SELECT prerelease, is_prerelease, mirror_state FROM app_releases "
                            "WHERE tag=%s", (rc.tag,))
    assert dict(stored) == {"prerelease": "rc.1", "is_prerelease": True,
                            "mirror_state": "undeployable"}


def test_all_and_get_of_unknown(pg):
    _seed(pg, published(T2), published(T1, has_image=False))
    with pg.begin() as tx:
        assert PgReleaseRecords().all(tx) == (row(published(T1, has_image=False)),
                                              row(published(T2)))
        assert PgReleaseRecords().get(tx, "v9.9.9") is None


def test_promoted_tag_round_trip(pg):
    _seed(pg, published(T1), published(T2))
    releases = PgReleaseRecords()
    with pg.begin() as tx:
        assert releases.promoted_tag(tx) is None
        releases.set_promoted(tx, T1)
        assert releases.promoted_tag(tx) == T1
        releases.set_promoted(tx, T2)
    with pg.begin() as tx:
        assert releases.promoted_tag(tx) == T2


def test_etag_round_trip(pg):
    releases = PgReleaseRecords()
    with pg.begin() as tx:
        assert releases.load_etag(tx) is None
        releases.store_etag(tx, 'W/"abc"')
    with pg.begin() as tx:
        assert releases.load_etag(tx) == 'W/"abc"'
        releases.store_etag(tx, None)
        assert releases.load_etag(tx) is None


def test_bound_player_count_counts_only_bound_players(registry, pg):
    with pg.begin() as tx:
        assert PgReleaseRecords().bound_player_count(tx) == 0
    identity, _, _ = enroll(registry)
    frame(registry)
    with pg.begin() as tx:
        assert PgReleaseRecords().bound_player_count(tx) == 0  # enrolled but unbound
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    with pg.begin() as tx:
        assert PgReleaseRecords().bound_player_count(tx) == 1


# -- devices ------------------------------------------------------------------------------------


def test_lock_upserts_the_row_and_refreshes_serial_and_last_seen(registry, pg):
    devices = PgDeviceRecords()
    with pg.begin() as tx:
        assert devices.lock(tx, "device-a", "serial-1", now=1000.0) == DeviceRow(
            "device-a", "serial-1", None, None, None, None, None, None, False)
    with pg.begin() as tx:
        assert devices.lock(tx, "device-a", "serial-2", now=2000.0).serial == "serial-2"
    stored = _raw(registry, "SELECT first_seen, last_seen FROM devices WHERE device_id=%s",
                  ("device-a",))
    assert (stored["first_seen"], stored["last_seen"]) == (1000.0, 2000.0)


def test_active_get_and_retired(registry, pg):
    _seed(pg, published(T1))
    _insert_device(registry, "device-b", known_good_tag=T1)
    _insert_device(registry, "device-a", retired_at=1500.0)
    devices = PgDeviceRecords()
    with pg.begin() as tx:
        assert [d.device_id for d in devices.active(tx)] == ["device-b"]
        assert devices.get(tx, "device-a").retired is True
        assert devices.get(tx, "device-b").known_good_tag == T1
        assert devices.get(tx, "device-nope") is None


def test_apply_record_served_and_pin(registry, pg):
    _seed(pg, published(T1), published(T))
    _insert_device(registry, "device-a", last_served_tag=T, boot_outcome="pending")
    devices = PgDeviceRecords()
    with pg.begin() as tx:
        devices.apply(tx, "device-a", DeviceUpdate(T, mark_boot_failed=True))
        assert (devices.get(tx, "device-a").failed_tag,
                devices.get(tx, "device-a").boot_outcome) == (T, "failed")
        devices.apply(tx, "device-a", DeviceUpdate(None, mark_boot_failed=False))
        got = devices.get(tx, "device-a")
        assert (got.failed_tag, got.boot_outcome) == (None, "failed")  # outcome untouched
        devices.record_served(tx, "device-a", T1, now=1234.0)
        got = devices.get(tx, "device-a")
        assert (got.last_served_tag, got.boot_outcome, got.last_served_at) == (
            T1, "pending", 1234.0)
    # Ported from test_netboot_base_pin: set, clear, and an unknown device matches nothing.
    with pg.begin() as tx:
        assert devices.set_pin(tx, "device-a", T1) is True
    assert _raw(registry, "SELECT attached_tag FROM devices WHERE device_id=%s",
                ("device-a",))["attached_tag"] == T1
    with pg.begin() as tx:
        assert devices.set_pin(tx, "device-a", None) is True
        assert devices.set_pin(tx, "device-does-not-exist", T1) is False
    assert _raw(registry, "SELECT attached_tag FROM devices WHERE device_id=%s",
                ("device-a",))["attached_tag"] is None


def test_sweep_fails_stale_pending_and_leaves_a_live_stick_untouched(registry, pg):
    # Ported from test_netboot_base_recovery (f): E2 NULL-guard, E2b served-tag fence, no bogus
    # fence for a device that served nothing, and recent / retired devices left alone.
    _seed(pg, published(T), published(T1), published(T2))
    _insert_device(registry, "device-frontier", known_good_tag=T2, known_good_at=1000.0)
    now = 10_000.0
    threshold = now - PENDING_HEALTH_TIMEOUT
    old, recent = threshold - 1, threshold + 1
    _insert_device(registry, "device-stick", boot_outcome="pending", failed_tag=T,
                   last_served_tag=T1, last_served_at=old)
    _insert_device(registry, "device-served-lower", boot_outcome="pending",
                   last_served_tag=T, last_served_at=old)
    _insert_device(registry, "device-pinned", boot_outcome="pending", attached_tag=T1,
                   last_served_tag=T, last_served_at=old)
    _insert_device(registry, "device-never", boot_outcome="pending", last_served_tag=None,
                   last_served_at=old)
    _insert_device(registry, "device-recent", boot_outcome="pending", last_served_tag=T2,
                   last_served_at=recent)
    _insert_device(registry, "device-retired", boot_outcome="pending", last_served_tag=T2,
                   last_served_at=old, retired_at=1000.0)

    devices = PgDeviceRecords()
    with pg.begin() as tx:
        assert devices.sweep_failed_boots(tx, served_before=threshold) == 4
        rows = {d.device_id: d for d in devices.active(tx)}
        retired = devices.get(tx, "device-retired")
    assert (rows["device-stick"].failed_tag, rows["device-stick"].boot_outcome) == (T, "failed")
    assert rows["device-served-lower"].failed_tag == T
    assert rows["device-pinned"].failed_tag == T1
    assert (rows["device-never"].failed_tag, rows["device-never"].boot_outcome) == (
        None, "failed")
    assert rows["device-recent"].boot_outcome == "pending"
    assert retired.boot_outcome == "pending"
