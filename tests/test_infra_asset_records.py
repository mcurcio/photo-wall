"""`PgAssetRecords` against real PostgreSQL. The 021 backfill's history test is retired with
every test that ran today's code on a schema older than production's (decision 0034).

The DB tests skip without `PHOTO_WALL_TEST_DATABASE_URL` (the `registry` fixture convention in
`tests/conftest.py`) and run in CI.
"""

from __future__ import annotations

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
KEY = AssetKey(AssetKind.SEALED_ENVIRONMENT, SHA_A)


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
    repo.reference(KEY, ref("v0.9.1"))  # same created_at as v0.9.0: owner DESC breaks the tie
    repo.clock.advance(1)
    repo.reference(KEY, ref("v1.0.0", url="https://mirror.test/a.deb"))  # a change keeps created_at
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
    repo.retire(AssetKey(AssetKind.SEALED_ENVIRONMENT, SHA_B), "v1.1.0")  # absent asset -> no-op
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


@pytest.mark.parametrize("kind", [k for k in AssetKind if k.keyed_by_content])
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
