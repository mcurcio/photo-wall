"""Real-PostgreSQL test support for the content catalog, the Asset records and the job runtime.

The domain code under test runs against the migrated schema through the real repositories, like
the rest of the suite (the `registry` fixture skips without PHOTO_WALL_TEST_DATABASE_URL; CI runs
it). Rows go in through the repositories, or as SQL for tables no repository writes (`devices`).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from runtime_fakes import apply_procrastinate_schema
from test_registry import enroll, frame

from central.assets.store import CacheStore
from central.content_catalog.ports import DeviceRow, Promoter, Promotion, ReleaseRow, StoredEtag
from central.db import Database
from central.infra.asset_records import PgAssetRecords
from central.infra.catalog_records import PgDeviceRecords, PgReleaseRecords
from central.infra.transactions import PgTransaction, PgTransactions, pg_connection
from central.kernel.assets import AssetKey, AssetKind, AssetReady, AssetReference, OriginLocator
from central.kernel.ports import PublishedRelease
from contracts.time import ManualClock

MIGRATIONS = Path(__file__).resolve().parents[1] / "central" / "migrations"


@contextmanager
def schema_before(first_unapplied: str) -> Iterator[Database]:
    """A fresh schema migrated through the migration before `first_unapplied` (a file-name
    prefix, e.g. "028"), with procrastinate installed; `Database.migrate()` then applies the
    rest. Skips without PHOTO_WALL_TEST_DATABASE_URL."""
    dsn = os.environ.get("PHOTO_WALL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set PHOTO_WALL_TEST_DATABASE_URL for real PostgreSQL integration")
    schema = "pw_test_" + uuid.uuid4().hex
    conninfo = make_conninfo(dsn, options=f"-c search_path={schema}")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
    db = None
    try:
        with psycopg.connect(conninfo) as conn:
            conn.execute("""CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name >= first_unapplied:
                    break
                sql = path.read_text()
                conn.execute(sql)
                conn.execute("INSERT INTO schema_migrations(name,sha256) VALUES(%s,%s)",
                             (path.name, hashlib.sha256(sql.encode()).hexdigest()))
        apply_procrastinate_schema(conninfo)
        db = Database(conninfo)
        yield db
    finally:
        if db is not None:
            db.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(
                psycopg.sql.Identifier(schema)))


class RecordingTransactions(PgTransactions):
    """`PgTransactions` that keeps every transaction it began, so a test can assert boundaries."""

    def __init__(self, database) -> None:
        super().__init__(database)
        self.begun: list[PgTransaction] = []

    @contextmanager
    def begin(self) -> Iterator[PgTransaction]:
        with super().begin() as tx:
            self.begun.append(tx)
            yield tx


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def published(row: ReleaseRow) -> PublishedRelease:
    """The listing entry that stores `row` (the inverse of `PgReleaseRecords.get`), with no
    upstream version: any later observation applies over it."""
    return PublishedRelease(row.tag, row.is_prerelease, row.package,
                            None if row.package else "no_player_asset", row.os_image, None)


def seed_releases(transactions: PgTransactions, rows, *, promoted: str | None = None,
                  promoted_by: Promoter = "operator", last_good: str | None = None,
                  etag: str | None = None, now: float = 1000.0,
                  etag_stored_at: float | None = None) -> None:
    """Claim and apply every release (a `ReleaseRow` or `PublishedRelease`; a divergent row is
    applied frozen), then the policy and the ETag (stored at `etag_stored_at`, default `now`)."""
    releases = PgReleaseRecords()
    with transactions.begin() as tx:
        for row in rows:
            release = published(row) if isinstance(row, ReleaseRow) else row
            divergent = isinstance(row, ReleaseRow) and row.divergent
            if releases.claim(tx, release, now=now) is not None or divergent:
                releases.apply(tx, release, divergent=divergent, now=now)
        if promoted is not None:
            releases.set_promoted(tx, promoted, by=promoted_by)
        if last_good is not None:
            releases.set_last_good(tx, last_good)
        if etag is not None:
            releases.store_etag(tx, etag, now=now if etag_stored_at is None else etag_stored_at)


def insert_device(db, device_id: str, *, serial: str | None = None,
                  attached_tag: str | None = None, known_good_tag: str | None = None,
                  last_served_tag: str | None = None, boot_outcome: str | None = None,
                  failed_tag: str | None = None, last_served_at: float | None = None,
                  retired: bool = False, seen: float = 1000.0) -> None:
    """A `devices` row (the tags must be releases: the table's foreign keys)."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO devices(device_id, serial, attached_tag, known_good_tag, "
            "last_served_tag, boot_outcome, failed_tag, last_served_at, first_seen, last_seen, "
            "retired_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (device_id, serial, attached_tag, known_good_tag, last_served_tag, boot_outcome,
             failed_tag, last_served_at, seen, seen, seen if retired else None))


def bind_a_player(registry) -> None:
    """One enrolled player bound to a frame: `bound_player_count` becomes 1."""
    identity, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)


class Reads:
    """Read-back helpers over one `PgTransactions`."""

    def __init__(self, transactions: PgTransactions) -> None:
        self.transactions = transactions

    def device(self, device_id: str) -> DeviceRow | None:
        with self.transactions.begin() as tx:
            return PgDeviceRecords().get(tx, device_id)

    def release(self, tag: str) -> ReleaseRow | None:
        with self.transactions.begin() as tx:
            return PgReleaseRecords().get(tx, tag)

    def releases(self) -> dict[str, ReleaseRow]:
        with self.transactions.begin() as tx:
            return {row.tag: row for row in PgReleaseRecords().all(tx)}

    def promoted(self) -> str | None:
        with self.transactions.begin() as tx:
            return PgReleaseRecords().promoted_tag(tx)

    def promotion(self) -> Promotion | None:
        with self.transactions.begin() as tx:
            return PgReleaseRecords().promotion(tx)

    def last_good(self) -> str | None:
        with self.transactions.begin() as tx:
            return PgReleaseRecords().last_good_tag(tx)

    def etag(self) -> str | None:
        stored = self.stored_etag()
        return None if stored is None else stored.etag

    def stored_etag(self) -> StoredEtag | None:
        with self.transactions.begin() as tx:
            return PgReleaseRecords().load_etag(tx)

    def asset(self, key: AssetKey):
        with self.transactions.begin() as tx:
            return PgAssetRecords(ManualClock(0.0)).get(tx, key)

    def owners(self, key: AssetKey) -> list[str] | None:
        asset = self.asset(key)
        return None if asset is None else [ref.owner for ref in asset.references]

    def facts(self, key: AssetKey) -> AssetReady | None | Literal["no_row"]:
        """The asset row's produced facts, even with no reference left (`get` then answers
        None); "no_row" when the row itself is gone."""
        with self.transactions.begin() as tx:
            row = pg_connection(tx).execute(
                "SELECT produced_size, produced_sha256 FROM assets WHERE kind=%s AND identity=%s",
                (key.kind.value, key.identity)).fetchone()
        if row is None:
            return "no_row"
        if row["produced_sha256"] is None:
            return None
        return AssetReady(size=row["produced_size"], sha256=row["produced_sha256"])


def reference(transactions: PgTransactions, assets: PgAssetRecords, key: AssetKey,
              owner: str, locator: OriginLocator, *,
              expected: AssetReady | None = None) -> None:
    with transactions.begin() as tx:
        assets.reference(tx, key, AssetReference(
            owner, locator, expected.size if expected else None,
            expected.sha256 if expected else None))


def record_produced(transactions: PgTransactions, assets: PgAssetRecords, key: AssetKey,
                    facts: AssetReady) -> None:
    with transactions.begin() as tx:
        assets.record_produced(tx, key, facts)


def put_file(store: CacheStore, key: AssetKey, data: bytes) -> Path:
    path = store.layout.path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def store_deb(transactions: PgTransactions, assets: PgAssetRecords, store: CacheStore,
              sha256: str, size: int) -> None:
    """A produced `.deb` on disk, as `DiskStoredAssets.present` sees it (record + file size)."""
    key = AssetKey(AssetKind.PLAYER_DEB, sha256)
    locator = OriginLocator(f"https://example.test/{sha256}.deb", sha256, size)
    reference(transactions, assets, key, "seed", locator,
              expected=AssetReady(size=size, sha256=sha256))
    record_produced(transactions, assets, key, AssetReady(size=size, sha256=sha256))
    put_file(store, key, b"d" * size)


def facts_of(data: bytes) -> AssetReady:
    return AssetReady(size=len(data), sha256=hashlib.sha256(data).hexdigest())
