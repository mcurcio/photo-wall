"""`PgReleaseRecords` / `PgDeviceRecords` round-trip every method against a migrated schema.

The DB tests use the `registry` fixture, which skips without PHOTO_WALL_TEST_DATABASE_URL (CI runs
them). The SQL assertions are ported from `test_netboot_base_pin.py` and the sweep cases of
`test_netboot_base_recovery.py`. The fake-transaction guard runs without a database.
"""

from __future__ import annotations

import hashlib

import psycopg
import pytest
from fakes.transactions import FakeTransaction
from test_registry import enroll, frame

from central.content_catalog.ports import (
    DeviceRow,
    DeviceUpdate,
    NamedTags,
    Promotion,
    PromotionWrite,
    ReleaseRow,
)
from central.infra.catalog_records import PgDeviceRecords, PgReleaseRecords
from central.infra.transactions import PgTransactions
from central.kernel.assets import OriginLocator
from central.kernel.ports import PublishedRelease
from central.netboot_base import record_base_health
from contracts.models import BaseHealth

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
    lambda tx: PgReleaseRecords().set_promoted(tx, T1, by="operator"),
    lambda tx: PgReleaseRecords().promotion(tx),
    lambda tx: PgReleaseRecords().load_etag(tx),
    lambda tx: PgReleaseRecords().store_etag(tx, "e"),
    lambda tx: PgReleaseRecords().bound_player_count(tx),
    lambda tx: PgReleaseRecords().shipping(tx, sha("x")),
    lambda tx: PgReleaseRecords().mark_divergent(tx, T1),
    lambda tx: PgReleaseRecords().last_good_tag(tx),
    lambda tx: PgReleaseRecords().set_last_good(tx, T1),
    lambda tx: PgDeviceRecords().known_good_tags(tx),
    lambda tx: PgDeviceRecords().named_tags(tx, served_since=1.0),
    lambda tx: PgDeviceRecords().names_any(tx, {T1}, served_since=1.0),
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


def test_promoted_tag_round_trip_records_who_promoted(pg):
    _seed(pg, published(T1), published(T), published(T2))
    releases = PgReleaseRecords()
    with pg.begin() as tx:
        assert releases.promoted_tag(tx) is None and releases.promotion(tx) is None
        assert releases.set_promoted(tx, T1, by="auto") == PromotionWrite(True, None)
        assert releases.promotion(tx) == Promotion(T1, "auto")
        assert releases.set_promoted(tx, T, by="auto") == PromotionWrite(
            True, Promotion(T1, "auto"))
        assert releases.set_promoted(tx, T2, by="operator") == PromotionWrite(
            True, Promotion(T, "auto"))
    with pg.begin() as tx:
        assert releases.promoted_tag(tx) == T2
        assert releases.promotion(tx) == Promotion(T2, "operator")


@pytest.mark.parametrize("first", ["seeded", "same_transaction"])
def test_an_auto_write_never_moves_an_operator_promotion_and_an_operator_write_always_does(
        pg, first):
    # Issue #23: the write itself refuses, whatever the caller read before it.
    _seed(pg, published(T1), published(T), published(T2))
    releases = PgReleaseRecords()
    if first == "seeded":
        with pg.begin() as tx:
            releases.set_promoted(tx, T1, by="operator")
    with pg.begin() as tx:
        if first == "same_transaction":
            releases.set_promoted(tx, T1, by="operator")
        assert releases.set_promoted(tx, T, by="auto") == PromotionWrite(
            False, Promotion(T1, "operator"))
        assert releases.promotion(tx) == Promotion(T1, "operator")
        assert releases.set_promoted(tx, T2, by="operator") == PromotionWrite(
            True, Promotion(T1, "operator"))
        assert releases.promotion(tx) == Promotion(T2, "operator")


@pytest.mark.parametrize("columns,values", [
    ("singleton, promoted_tag", "TRUE, 'v0.0.1'"),  # a writer that does not say who it is
    ("singleton, promoted_tag, promoted_by", "TRUE, 'v0.0.1', 'sync'"),
])
def test_the_policy_row_refuses_a_promotion_without_a_known_promoter(registry, pg, columns,
                                                                    values):
    _seed(pg, published(T1))
    with pytest.raises(psycopg.errors.IntegrityError), registry.db.transaction() as conn:
        # Test-controlled literals only.
        conn.execute(f"INSERT INTO app_release_policy({columns}) VALUES({values})")


def test_last_good_round_trip_rides_the_policy_row(pg):
    _seed(pg, published(T1), published(T2))
    releases = PgReleaseRecords()
    with pg.begin() as tx:
        assert releases.last_good_tag(tx) is None
        releases.set_promoted(tx, T2, by="operator")
        releases.set_last_good(tx, T1)
    with pg.begin() as tx:
        assert (releases.promoted_tag(tx), releases.last_good_tag(tx)) == (T2, T1)
        # moving the promoted pointer leaves last-good alone
        releases.set_promoted(tx, T1, by="operator")
        assert releases.last_good_tag(tx) == T1


def test_shipping_finds_every_release_of_a_sha(pg):
    shared = deb(T1)
    _seed(pg, published(T1), published(T, package=shared), published(T2))
    with pg.begin() as tx:
        assert [r.tag for r in PgReleaseRecords().shipping(tx, shared.sha256)] == [T1, T]
        assert PgReleaseRecords().shipping(tx, sha("unknown")) == ()


def test_mark_divergent_survives_later_upserts(pg):
    _seed(pg, published(T1))
    releases = PgReleaseRecords()
    with pg.begin() as tx:
        assert releases.get(tx, T1).divergent is False
        releases.mark_divergent(tx, T1)
        releases.upsert(tx, published(T1), now=2000.0)
        assert releases.get(tx, T1).divergent is True


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


def test_distinct_tag_reads_cover_active_devices_only(registry, pg):
    _seed(pg, published(T1), published(T), published(T2))
    _insert_device(registry, "device-a", attached_tag=T1, known_good_tag=T, last_served_tag=T,
                   last_served_at=1000.0)
    _insert_device(registry, "device-b", known_good_tag=T, last_served_tag=T1,
                   last_served_at=1000.0)
    _insert_device(registry, "device-r", attached_tag=T2, known_good_tag=T2,
                   last_served_tag=T2, last_served_at=1000.0, retired_at=1500.0)
    for n in range(3):
        _insert_device(registry, f"device-fake-{n}")  # a fake serial names nothing
    devices = PgDeviceRecords()
    with pg.begin() as tx:
        assert devices.known_good_tags(tx) == frozenset({T})
        assert devices.named_tags(tx, served_since=1000.0) == NamedTags(
            frozenset({T1}), frozenset({T}), frozenset({T, T1}))
        assert devices.names_any(tx, {T1, "v9.9.9"}, served_since=1000.0) is True
        # only the retired device names it
        assert devices.names_any(tx, {T2}, served_since=1000.0) is False
        assert devices.names_any(tx, set(), served_since=1000.0) is False


@pytest.mark.parametrize("served_at,named", [(1000.0, True), (999.0, False)])
def test_the_served_role_counts_a_serve_at_or_after_served_since_only(registry, pg, served_at,
                                                                     named):
    # Issue #25: both reads take the one cutoff.
    _seed(pg, published(T1), published(T), published(T2))
    _insert_device(registry, "device-s", last_served_tag=T1, last_served_at=served_at)
    _insert_device(registry, "device-p", attached_tag=T, last_served_tag=T, last_served_at=1.0)
    _insert_device(registry, "device-k", known_good_tag=T2, last_served_tag=T2,
                   last_served_at=1.0)
    devices = PgDeviceRecords()
    with pg.begin() as tx:
        assert devices.named_tags(tx, served_since=1000.0) == NamedTags(
            frozenset({T}), frozenset({T2}), frozenset({T1}) if named else frozenset())
        assert devices.names_any(tx, {T1}, served_since=1000.0) is named
        assert devices.names_any(tx, {T}, served_since=1000.0) is True  # a pin is not aged
        assert devices.names_any(tx, {T2}, served_since=1000.0) is True  # nor a known-good


@pytest.mark.parametrize("write", ["insert", "update"])
def test_the_database_refuses_a_served_tag_without_a_serve_time(registry, pg, write):
    # 026: the served window reads last_served_at, so a served tag with no serve time would
    # silently name nothing; the schema refuses the pair instead.
    _seed(pg, published(T1))
    _insert_device(registry, "device-a", last_served_tag=T1, last_served_at=1000.0)
    sql = {"insert": "INSERT INTO devices(device_id, last_served_tag, first_seen, last_seen) "
                     "VALUES('device-b', 'v0.0.1', 1.0, 1.0)",
           "update": "UPDATE devices SET last_served_at = NULL WHERE device_id = 'device-a'"}
    with pytest.raises(psycopg.errors.CheckViolation), registry.db.transaction() as conn:
        conn.execute(sql[write])


def test_apply_record_served_and_pin(registry, pg):
    _seed(pg, published(T1), published(T))
    _insert_device(registry, "device-a", last_served_tag=T, boot_outcome="pending",
                   last_served_at=1000.0)
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


# -- base-health (record_base_health still ships in the pruned netboot_base) ---------------------


def test_base_health_for_a_tag_no_longer_last_served_never_advances_known_good(registry, pg):
    # Ported from main's test_netboot_base_tracer lost-update probe (E1): a recovery serve moved
    # last_served_tag to T1 while a stale report still claims T2. Drop the conditional
    # `WHERE last_served_tag = running_tag` guard and the stale report wins.
    _seed(pg, published(T1), published(T2))
    _insert_device(registry, "device-a", last_served_tag=T1, boot_outcome="pending",
                   last_served_at=1000.0)
    stale = BaseHealth(authority_epoch=1, sequence=1, running_tag=T2, healthy=True)
    with registry.db.transaction() as conn:
        assert record_base_health(conn, "device-a", stale, clock=registry.clock) is False
    got = _raw(registry, "SELECT known_good_tag, boot_outcome FROM devices WHERE device_id=%s",
               ("device-a",))
    assert (got["known_good_tag"], got["boot_outcome"]) == (None, "pending")
    fresh = BaseHealth(authority_epoch=1, sequence=2, running_tag=T1, healthy=True)
    with registry.db.transaction() as conn:
        assert record_base_health(conn, "device-a", fresh, clock=registry.clock) is True
    assert _raw(registry, "SELECT known_good_tag FROM devices WHERE device_id=%s",
                ("device-a",))["known_good_tag"] == T1
