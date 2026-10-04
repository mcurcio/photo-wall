"""`PgAssetRecords` and the 021 backfill against real PostgreSQL.

The DB tests skip without `PHOTO_WALL_TEST_DATABASE_URL` (the `registry` fixture convention in
`tests/conftest.py`) and run in CI.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from fakes.transactions import FakeTransaction

from central.assets.library import THUMBNAIL_REFERENCE
from central.db import Database
from central.infra.asset_records import PgAssetRecords
from central.infra.transactions import PgTransactions
from central.kernel.assets import AssetKey, AssetKind, AssetReady, AssetReference, OriginLocator
from central.kernel.handling import ProducedFactsConflict
from contracts.time import ManualClock

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_T1 = "1" * 64
SHA_T2 = "2" * 64
SHA_S1 = "5" * 64
KEY = AssetKey(AssetKind.PLAYER_DEB, SHA_A)
MIGRATIONS = Path(__file__).resolve().parents[1] / "central" / "migrations"


def ref(owner: str, url: str = "https://example.test/a.deb", size: int = 10) -> AssetReference:
    return AssetReference(owner, OriginLocator(url, sha256=SHA_A, size=size),
                          expected_size=size, expected_sha256=SHA_A)


class Repo:
    def __init__(self, database: Database, clock: ManualClock) -> None:
        self.clock = clock
        self.records = PgAssetRecords(clock)
        self.transactions = PgTransactions(database)
        self.database = database

    def __getattr__(self, name):
        method = getattr(self.records, name)

        def call(*args):
            with self.transactions.begin() as tx:
                return method(tx, *args)

        return call

    def count(self, table: str) -> int:
        with self.database.transaction() as conn:
            return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


@pytest.fixture
def repo(registry):
    return Repo(registry.db, registry.clock)


def test_a_fake_transaction_is_refused_without_a_database():
    with pytest.raises(TypeError):
        PgAssetRecords(ManualClock(0.0)).get(FakeTransaction(), KEY)


def test_reference_is_idempotent_and_reports_changes(repo):
    assert repo.reference(KEY, ref("v1.0.0")) is True
    assert repo.reference(KEY, ref("v1.0.0")) is False
    assert repo.reference(KEY, ref("v1.0.0", url="https://mirror.test/a.deb")) is True
    asset = repo.get(KEY)
    assert asset.references == (ref("v1.0.0", url="https://mirror.test/a.deb"),)
    assert asset.produced is None and asset.last_served_at is None


def test_get_orders_references_newest_first_then_owner_desc(repo):
    repo.reference(KEY, ref("v1.0.0"))
    repo.clock.advance(1)
    repo.reference(KEY, ref("v0.9.0"))
    repo.reference(KEY, ref("v0.9.1"))  # same added_at as v0.9.0: owner DESC breaks the tie
    repo.clock.advance(1)
    repo.reference(KEY, ref("v1.0.0", url="https://mirror.test/a.deb"))  # a change keeps added_at
    assert [r.owner for r in repo.get(KEY).references] == ["v0.9.1", "v0.9.0", "v1.0.0"]


def test_retire_of_the_last_reference_keeps_the_row_and_its_facts(repo):
    facts = AssetReady(size=10, sha256=SHA_A)
    repo.reference(KEY, ref("v1.0.0"))
    repo.reference(KEY, ref("v1.1.0"))
    repo.record_produced(KEY, facts)
    repo.retire(KEY, "v1.0.0")
    assert [r.owner for r in repo.get(KEY).references] == ["v1.1.0"]
    repo.retire(KEY, "nobody")  # absent owner -> no-op
    repo.retire(KEY, "v1.1.0")
    assert repo.get(KEY) is None  # no reference: not an asset
    assert repo.count("assets") == 1 and repo.count("asset_references") == 0
    repo.retire(KEY, "v1.1.0")  # absent reference -> no-op
    repo.retire(AssetKey(AssetKind.PLAYER_DEB, SHA_B), "v1.1.0")  # absent asset -> no-op
    # A new reference revives the key with the facts it always had (never cleared).
    assert repo.reference(KEY, ref("v1.2.0")) is True
    assert repo.get(KEY).produced == facts


def test_record_produced_is_write_once(repo):
    facts = AssetReady(size=10, sha256=SHA_A)
    repo.record_produced(KEY, facts)  # absent -> no-op
    assert repo.count("assets") == 0
    repo.reference(KEY, ref("v1.0.0"))
    repo.record_produced(KEY, facts)
    repo.record_produced(KEY, facts)  # equal -> no-op
    with pytest.raises(ProducedFactsConflict):
        repo.record_produced(KEY, AssetReady(size=11, sha256=SHA_A))
    assert repo.get(KEY).produced == facts


def test_an_identity_keyed_thumbnail_takes_new_produced_facts(repo):
    """A library thumbnail is keyed by its original, so the library may regenerate its bytes:
    after a purge the re-produced facts replace the recorded ones instead of conflicting for
    good (B5-FC2-4). Mutation probe: drop `OR NOT keyed_by_content` from the UPDATE."""
    key = AssetKey(AssetKind.LIBRARY_THUMBNAIL, "asset-" + "c" * 64)
    assert not key.kind.keyed_by_content and KEY.kind.keyed_by_content
    repo.reference(key, THUMBNAIL_REFERENCE)
    repo.record_produced(key, AssetReady(size=10, sha256=SHA_A))
    repo.record_produced(key, AssetReady(size=11, sha256=SHA_B))
    assert repo.get(key).produced == AssetReady(size=11, sha256=SHA_B)


@pytest.mark.parametrize("kind", [k for k in AssetKind if k is not AssetKind.LIBRARY_THUMBNAIL])
@pytest.mark.parametrize("locator_sha", [None, SHA_B])
def test_a_reference_whose_locator_does_not_name_its_key_is_refused(repo, kind, locator_sha):
    # 028's CHECK: production may fetch from ANY reference of a key, and `download` checks the
    # bytes only against a set locator sha, so a reference to other (or unchecked) bytes would
    # install them under the key. The schema refuses it, for both kinds.
    key = AssetKey(kind, SHA_A)
    stray = AssetReference("v1.0.0", OriginLocator("https://example.test/a", sha256=locator_sha,
                                                   size=10), None, None)
    with pytest.raises(psycopg.errors.CheckViolation):
        repo.reference(key, stray)
    assert repo.count("assets") == 0 and repo.count("asset_references") == 0  # rolled back
    good = AssetReference("v1.0.0", OriginLocator("https://example.test/a", SHA_A, 10), None, None)
    assert repo.reference(key, good) is True


@pytest.mark.parametrize("stray", [
    AssetReference("library-preview", OriginLocator("https://library.example/a", None, None),
                   None, None),
    AssetReference("v1.0.0", OriginLocator("http://library.invalid/", None, None), None, None),
    AssetReference("library-preview", OriginLocator("http://library.invalid/", SHA_A, 10),
                   None, None),
])
def test_a_thumbnail_admits_only_the_reserved_reference(repo, stray):
    # 064: a thumbnail's locator never names an address or bytes (the media worker alone holds
    # the library's); `central.assets.library.THUMBNAIL_REFERENCE` is the one row allowed.
    from central.assets.library import THUMBNAIL_REFERENCE

    key = AssetKey(AssetKind.LIBRARY_THUMBNAIL, "asset-" + SHA_A)
    with pytest.raises(psycopg.errors.CheckViolation):
        repo.reference(key, stray)
    assert repo.reference(key, THUMBNAIL_REFERENCE) is True


def test_touch_served_stamps_the_database_clock_and_only_it_is_compared(repo):
    repo.touch_served(KEY)  # absent -> no-op
    assert repo.served_within(KEY, 3600.0) is False
    repo.reference(KEY, ref("v1.0.0"))
    assert repo.get(KEY).last_served_at is None and repo.served_within(KEY, 3600.0) is False
    repo.touch_served(KEY)
    with repo.database.transaction() as conn:
        now = conn.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS t").fetchone()["t"]
    served = repo.get(KEY).last_served_at
    assert 0 <= float(now) - served < 60  # the database's clock, not the caller's ManualClock
    assert repo.served_within(KEY, 3600.0) is True
    with repo.database.transaction() as conn:
        conn.execute("UPDATE assets SET last_served_at=last_served_at-7200")
    assert repo.served_within(KEY, 3600.0) is False


# -- the 021 backfill -----------------------------------------------------------------------------


@pytest.fixture
def pre_021(empty_database):
    """A fresh database migrated up to (not including) 021; yields its DSN."""
    with psycopg.connect(empty_database) as conn:
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < "021":
                conn.execute(path.read_text())
    return empty_database


def _release(conn, tag, *, asset=None, base=None):
    major, minor, patch = (int(part) for part in tag[1:].split("."))
    asset_sha, asset_size, asset_url = asset or (None, None, None)
    base_sha, base_size, base_url = base or (None, None, None)
    conn.execute(
        "INSERT INTO app_releases (tag, major, minor, patch, is_prerelease, asset_sha256, "
        "asset_size, asset_url, base_tarball_sha256, base_tarball_size, base_tarball_url, "
        "discovered_at, updated_at) VALUES (%s,%s,%s,%s,FALSE,%s,%s,%s,%s,%s,%s,1,1)",
        (tag, major, minor, patch, asset_sha, asset_size, asset_url, base_sha, base_size, base_url),
    )


def test_021_backfill_seeds_references_and_produced_facts(pre_021):
    with psycopg.connect(pre_021) as conn:
        _release(conn, "v1.0.0", asset=(SHA_A, 10, "https://gh.test/v1.0.0/a.deb"),
                 base=(SHA_T1, 100, "https://gh.test/v1.0.0/base.tar.gz"))
        _release(conn, "v1.1.0", asset=(SHA_A, 10, "https://gh.test/v1.1.0/a.deb"),
                 base=(SHA_T2, 200, "https://gh.test/v1.1.0/base.tar.gz"))
        _release(conn, "v2.0.0", asset=(SHA_B, 20, "https://gh.test/v2.0.0/b.deb"))
        _release(conn, "v3.0.0")  # undeployable: nothing to seed
        _release(conn, "v4.0.0", asset=(SHA_C, 30, "ftp://gh.test/c.deb"))  # not http(s)
        conn.execute("INSERT INTO app_packages (sha256, version, size, registered_at) "
                     "VALUES (%s,'v1.0.0',10,1), (%s,'v9.0.0',30,1)", (SHA_A, SHA_C))
        conn.execute(
            "INSERT INTO base_cache (tag, squashfs_sha256, size, state, updated_at) VALUES "
            "('v1.0.0', %s, 50, 'evicted', 1), ('v1.1.0', NULL, NULL, 'caching', 1), "
            "('v2.0.0', %s, 60, 'cached', 1)",
            (SHA_S1, SHA_S1),
        )
        conn.execute((MIGRATIONS / "021_assets.sql").read_text())

    database = Database(pre_021)
    try:
        repo = Repo(database, ManualClock(1000.0))
        os_old = repo.get(AssetKey(AssetKind.OS_IMAGE, "v1.0.0"))
        assert os_old.references == (AssetReference(
            "v1.0.0", OriginLocator("https://gh.test/v1.0.0/base.tar.gz", SHA_T1, 100),
            expected_size=None, expected_sha256=None),)
        assert os_old.produced == AssetReady(size=50, sha256=SHA_S1)
        assert repo.get(AssetKey(AssetKind.OS_IMAGE, "v1.1.0")).produced is None
        assert repo.get(AssetKey(AssetKind.OS_IMAGE, "v2.0.0")) is None  # no reference, no facts

        shared = repo.get(AssetKey(AssetKind.PLAYER_DEB, SHA_A))
        assert [r.owner for r in shared.references] == ["v1.1.0", "v1.0.0"]
        assert shared.references[1] == AssetReference(
            "v1.0.0", OriginLocator("https://gh.test/v1.0.0/a.deb", SHA_A, 10),
            expected_size=10, expected_sha256=SHA_A)
        assert shared.produced == AssetReady(size=10, sha256=SHA_A)
        assert repo.get(AssetKey(AssetKind.PLAYER_DEB, SHA_B)).produced is None
        assert repo.get(AssetKey(AssetKind.PLAYER_DEB, SHA_C)) is None
        assert repo.count("assets") == 4
        assert repo.count("asset_references") == 5
    finally:
        database.close()
