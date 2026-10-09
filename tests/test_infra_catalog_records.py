"""`PgReleaseRecords` round-trips every method against a migrated schema.

The DB tests use the `registry` fixture, which skips without PHOTO_WALL_TEST_DATABASE_URL (CI runs
them). The fake-transaction guard runs without a database.
"""

from __future__ import annotations

import dataclasses
import hashlib

import psycopg
import pytest
from fakes.transactions import FakeTransaction

from central.content_catalog.ports import Promotion, ReleaseRow, StoredEtag
from central.infra.catalog_records import PgReleaseRecords
from central.infra.transactions import PgTransactions
from central.kernel.assets import OriginLocator
from central.kernel.ports import PublishedRelease, UpstreamVersion

T1, T, T2 = "v0.0.1", "v0.0.2", "v0.0.3"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def deb(tag: str, cut: str = "") -> OriginLocator:
    return OriginLocator(f"https://example.test/{tag}{cut}.deb", sha("deb" + tag + cut), 10)


def image(tag: str, cut: str = "") -> OriginLocator:
    return OriginLocator(f"https://example.test/{tag}.tgz", sha("img" + tag + cut), 64)


def published(tag: str, *, pre: bool = False, has_image: bool = True, cut: str = "",
              at: float | None = 1) -> PublishedRelease:
    """`tag` observed at upstream version `at` (None: no manifest body was read)."""
    return PublishedRelease(tag, pre, image(tag, cut) if has_image else None,
                            None if at is None else UpstreamVersion(float(at), 1))


def row(release: PublishedRelease, package: OriginLocator | None = None) -> ReleaseRow:
    return ReleaseRow(release.tag, release.is_prerelease, package, release.os_image)


@pytest.fixture
def pg(registry):
    return PgTransactions(registry.db)


def _seed(pg, *releases: PublishedRelease) -> None:
    with pg.begin() as tx:
        for release in releases:
            assert PgReleaseRecords().claim(tx, release, now=1000.0) is None


def _seed_deb(registry, tag: str, package: OriginLocator) -> None:
    """A release's `.deb`, written as the netboot tracer seeds it: no record method writes one."""
    with registry.db.transaction() as conn:
        conn.execute("UPDATE app_releases SET asset_url=%s,asset_sha256=%s,asset_size=%s "
                     "WHERE tag=%s", (package.url, package.sha256, package.size, tag))


def _raw(registry, sql, params=()):
    with registry.db.transaction() as conn:
        return conn.execute(sql, params).fetchone()


# -- no database: a fake transaction handed to a real repository --------------------------------


FAKE_TRANSACTION_CALLS = {
    "PgReleaseRecords.get": lambda tx: PgReleaseRecords().get(tx, T1),
    "PgReleaseRecords.all": lambda tx: PgReleaseRecords().all(tx),
    "PgReleaseRecords.fleet_desired_assets": lambda tx: PgReleaseRecords().fleet_desired_assets(tx),
    "PgReleaseRecords.claim": lambda tx: PgReleaseRecords().claim(tx, published(T1), now=1.0),
    "PgReleaseRecords.apply": lambda tx: PgReleaseRecords().apply(tx, published(T1), now=1.0),
    "PgReleaseRecords.promoted_tag": lambda tx: PgReleaseRecords().promoted_tag(tx),
    "PgReleaseRecords.set_promoted": lambda tx: PgReleaseRecords().set_promoted(tx, T1),
    "PgReleaseRecords.promotion": lambda tx: PgReleaseRecords().promotion(tx),
    "PgReleaseRecords.load_etag": lambda tx: PgReleaseRecords().load_etag(tx),
    "PgReleaseRecords.store_etag": lambda tx: PgReleaseRecords().store_etag(tx, "e", now=1.0),
    "PgReleaseRecords.shipping": lambda tx: PgReleaseRecords().shipping(tx, sha("x")),
    "PgReleaseRecords.last_good_tag": lambda tx: PgReleaseRecords().last_good_tag(tx),
    "PgReleaseRecords.set_last_good": lambda tx: PgReleaseRecords().set_last_good(tx, T1),
}


def test_a_fake_transaction_is_a_type_error():
    # Every call is tried, so one run names every offender: one that accepts the fake, and one
    # that refuses it with anything but a TypeError.
    offenders = []
    for name, call in FAKE_TRANSACTION_CALLS.items():
        try:
            call(FakeTransaction())
        except TypeError:
            continue
        except Exception as error:
            offenders.append(f"{name}: {type(error).__name__}")
        else:
            offenders.append(f"{name}: accepted")
    assert offenders == []


# -- releases -----------------------------------------------------------------------------------


def test_claim_inserts_then_returns_the_previous_row_and_apply_writes_over_it(registry, pg):
    releases = PgReleaseRecords()
    first = published(T1)
    with pg.begin() as tx:
        assert releases.claim(tx, first, now=1000.0) is None  # inserted: applied
        assert releases.get(tx, T1) == row(first)
    recut = published(T1, has_image=False, at=2)
    with pg.begin() as tx:
        assert releases.claim(tx, recut, now=2000.0) == row(first)  # claimed: nothing written
        assert releases.get(tx, T1) == row(first)
        assert releases.apply(tx, recut, now=2000.0) is True
        assert releases.get(tx, T1) == row(recut)  # base_* columns cleared with the image
    stored = _raw(registry, "SELECT major, minor, patch, prerelease, discovered_at, "
                            "updated_at, base_tarball_url, upstream_changed_at, upstream_asset_id "
                            "FROM app_releases WHERE tag=%s", (T1,))
    assert (stored["major"], stored["minor"], stored["patch"], stored["prerelease"]) == (
        0, 0, 1, "")
    assert (stored["discovered_at"], stored["updated_at"]) == (1000.0, 2000.0)
    assert stored["base_tarball_url"] is None
    assert (stored["upstream_changed_at"], stored["upstream_asset_id"]) == (2.0, 1)


def test_a_release_observation_never_writes_the_package_columns(registry, pg):
    _seed(pg, published(T1))
    _seed_deb(registry, T1, deb(T1))
    releases = PgReleaseRecords()
    with pg.begin() as tx:
        assert releases.claim(tx, published(T1, cut="-recut", at=2), now=2000.0) == row(
            published(T1), deb(T1))
        assert releases.apply(tx, published(T1, cut="-recut", at=2), now=2000.0)
        assert releases.get(tx, T1) == row(published(T1, cut="-recut"), deb(T1))


def test_claim_of_a_prerelease_records_its_flags(registry, pg):
    rc = published("v1.0.0-rc.1", pre=True)
    _seed(pg, rc)
    with pg.begin() as tx:
        assert PgReleaseRecords().get(tx, rc.tag) == row(rc)
    stored = _raw(registry, "SELECT prerelease, is_prerelease FROM app_releases WHERE tag=%s",
                  (rc.tag,))
    assert dict(stored) == {"prerelease": "rc.1", "is_prerelease": True}


def test_all_and_get_of_unknown(pg):
    _seed(pg, published(T2), published(T1, has_image=False))
    with pg.begin() as tx:
        assert PgReleaseRecords().all(tx) == (row(published(T1, has_image=False)),
                                              row(published(T2)))
        assert PgReleaseRecords().get(tx, "v9.9.9") is None


def test_set_promoted_is_the_operators_and_returns_the_outgoing_promotion(registry, pg):
    _seed(pg, published(T1), published(T), published(T2))
    releases = PgReleaseRecords()
    with pg.begin() as tx:
        assert releases.promoted_tag(tx) is None and releases.promotion(tx) is None
        assert releases.set_promoted(tx, T1) is None
        assert releases.promotion(tx) == Promotion(T1, "operator")
    with registry.db.transaction() as conn:  # a promotion the retired auto-promote recorded
        conn.execute("UPDATE app_release_policy SET promoted_tag=%s, promoted_by='auto'", (T,))
    with pg.begin() as tx:
        assert releases.set_promoted(tx, T2) == Promotion(T, "auto")
        assert releases.set_promoted(tx, T2) == Promotion(T2, "operator")
    with pg.begin() as tx:
        assert releases.promoted_tag(tx) == T2
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
        releases.set_promoted(tx, T2)
        releases.set_last_good(tx, T1)
    with pg.begin() as tx:
        assert (releases.promoted_tag(tx), releases.last_good_tag(tx)) == (T2, T1)
        # moving the promoted pointer leaves last-good alone
        releases.set_promoted(tx, T1)
        assert releases.last_good_tag(tx) == T1


def test_shipping_finds_every_release_of_a_sha(registry, pg):
    shared = deb(T1)
    _seed(pg, published(T1), published(T), published(T2))
    _seed_deb(registry, T1, shared)
    _seed_deb(registry, T, shared)
    with pg.begin() as tx:
        assert [r.tag for r in PgReleaseRecords().shipping(tx, shared.sha256)] == [T1, T]
        assert PgReleaseRecords().shipping(tx, sha("unknown")) == ()


@pytest.mark.parametrize("stored,offered,applied", [
    (None, None, True),  # a row never stamped takes any observation ...
    (None, (1.0, 5), True),
    ((2.0, 5), (2.0, 5), True),  # ... a stamped one an equal version ...
    ((2.0, 5), (2.0, 6), True),  # ... or a newer one: the id breaks a same-second tie
    ((2.0, 5), (3.0, 1), True),
    ((2.0, 5), (2.0, 4), False),  # an older one is refused
    ((2.0, 5), (1.0, 9), False),
    ((2.0, 5), None, False),  # and so is one whose manifest body was not read
])
def test_apply_refuses_an_older_observation_and_writes_nothing(registry, pg, stored, offered,
                                                               applied):
    def version(pair):
        return None if pair is None else UpstreamVersion(*pair)

    first = dataclasses.replace(published(T1), upstream_version=version(stored))
    _seed(pg, first)
    offer = dataclasses.replace(published(T1, cut="-recut", pre=True),
                                upstream_version=version(offered))
    releases = PgReleaseRecords()
    with pg.begin() as tx:
        assert releases.claim(tx, offer, now=2000.0) == row(first)
        assert releases.apply(tx, offer, now=2000.0) is applied
        assert releases.get(tx, T1) == row(offer if applied else first)
    got = _raw(registry, "SELECT upstream_changed_at, upstream_asset_id, updated_at "
                         "FROM app_releases WHERE tag=%s", (T1,))
    expected = offered if applied else stored
    assert (got["upstream_changed_at"], got["upstream_asset_id"]) == (
        (None, None) if expected is None else expected)
    assert got["updated_at"] == (2000.0 if applied else 1000.0)


def test_etag_round_trip_records_when_it_was_stored(registry, pg):
    releases = PgReleaseRecords()
    with pg.begin() as tx:
        assert releases.load_etag(tx) is None
        releases.store_etag(tx, 'W/"abc"', now=1000.0)
    with pg.begin() as tx:
        assert releases.load_etag(tx) == StoredEtag('W/"abc"', 1000.0)
        releases.store_etag(tx, None, now=2000.0)
        assert releases.load_etag(tx) == StoredEtag(None, 2000.0)
    with registry.db.transaction() as conn:  # an ETag no code of this version stored
        conn.execute("UPDATE app_release_poll SET etag='W/\"old\"', etag_stored_at=NULL")
    with pg.begin() as tx:
        assert releases.load_etag(tx) is None
