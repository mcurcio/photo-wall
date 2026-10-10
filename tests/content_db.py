"""Real-PostgreSQL test support for the content catalog, the Asset records and the job runtime.

The domain code under test runs against the migrated schema through the real repositories, like
the rest of the suite (the `registry` fixture skips without PHOTO_WALL_TEST_DATABASE_URL; CI runs
it). Rows go in through the repositories, or as SQL for what no repository writes (`devices`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import psycopg
from runtime_fakes import apply_procrastinate_schema
from test_registry import enroll, frame

from central.assets.store import CacheStore
from central.content_catalog.ports import StoredEtag
from central.db import Database
from central.infra.asset_records import PgAssetRecords
from central.infra.catalog_records import PgReleaseRecords
from central.infra.transactions import PgTransaction, PgTransactions, pg_connection
from central.kernel.assets import AssetKey, AssetReady, AssetReference, OriginLocator
from contracts.time import ManualClock

MIGRATIONS = Path(__file__).resolve().parents[1] / "central" / "migrations"


@contextmanager
def schema_before(empty_database: str, first_unapplied: str, *,
                  procrastinate: bool = True) -> Iterator[Database]:
    """`empty_database` (the fixture's conninfo) migrated through the migration before
    `first_unapplied` (a file-name prefix, e.g. "028"), recorded in the migration ledger, with
    procrastinate installed unless `procrastinate=False`; `Database.migrate()` then applies the
    rest."""
    with psycopg.connect(empty_database) as conn:
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
    if procrastinate:
        apply_procrastinate_schema(empty_database)
    db = Database(empty_database)
    try:
        yield db
    finally:
        db.close()


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


def store_etag(transactions: PgTransactions, etag: str, *, stored_at: float) -> None:
    """The release listing's ETag, stored at `stored_at`."""
    with transactions.begin() as tx:
        PgReleaseRecords().store_etag(tx, etag, now=stored_at)


def insert_device(db, device_id: str, *, serial: str | None = None, retired: bool = False,
                  seen: float = 1000.0) -> None:
    """A `devices` row."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO devices(device_id, serial, first_seen, last_seen, retired_at) "
            "VALUES(%s,%s,%s,%s,%s)",
            (device_id, serial, seen, seen, seen if retired else None))


def bind_a_player(registry) -> None:
    """One enrolled player bound to a frame: `bound_player_count` becomes 1."""
    identity, _, _ = enroll(registry)
    frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)


class Reads:
    """Read-back helpers over one `PgTransactions`."""

    def __init__(self, transactions: PgTransactions) -> None:
        self.transactions = transactions

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


def facts_of(data: bytes) -> AssetReady:
    return AssetReady(size=len(data), sha256=hashlib.sha256(data).hexdigest())
