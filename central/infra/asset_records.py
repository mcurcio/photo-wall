"""The PostgreSQL `AssetRecords` (tables `assets` and `asset_references`, migration 021).

Every method runs inside the caller's transaction (`pg_connection(tx)`), so it blocks: call it
from a worker thread.

Concurrency: `retire` locks the asset row `FOR UPDATE` before deleting, and `reference` takes a
row lock through its upsert, so a reference added concurrently with the retirement of the last
one either sees the row deleted and re-creates it, or keeps it alive; never an orphan reference.
"""

from __future__ import annotations

from typing import Any

from central.infra.transactions import pg_connection
from central.kernel.assets import Asset, AssetKey, AssetReady, AssetReference, OriginLocator
from central.kernel.handling import ProducedFactsConflict
from central.kernel.transactions import Transaction
from contracts.time import Clock

_REFERENCE_COLUMNS = ("locator_url", "locator_sha256", "locator_size",
                      "expected_size", "expected_sha256")


def _reference(row: dict[str, Any]) -> AssetReference:
    return AssetReference(
        owner=row["owner"],
        locator=OriginLocator(row["locator_url"], sha256=row["locator_sha256"],
                              size=row["locator_size"]),
        expected_size=row["expected_size"],
        expected_sha256=row["expected_sha256"],
    )


def _produced(row: dict[str, Any]) -> AssetReady | None:
    if row["produced_sha256"] is None:
        return None
    return AssetReady(size=row["produced_size"], sha256=row["produced_sha256"])


class PgAssetRecords:
    """Implements `central.kernel.ports.AssetRecords`."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def get(self, tx: Transaction, key: AssetKey) -> Asset | None:
        conn = pg_connection(tx)
        row = conn.execute(
            "SELECT produced_size, produced_sha256, last_served_at FROM assets "
            "WHERE kind=%s AND identity=%s",
            (key.kind.value, key.identity),
        ).fetchone()
        if row is None:
            return None
        refs = conn.execute(
            "SELECT owner, locator_url, locator_sha256, locator_size, expected_size, "
            "expected_sha256 FROM asset_references WHERE kind=%s AND identity=%s "
            "ORDER BY added_at DESC, owner DESC",
            (key.kind.value, key.identity),
        ).fetchall()
        if not refs:  # the row goes with its last reference; a bare row is not an asset
            return None
        return Asset(key=key, references=tuple(_reference(ref) for ref in refs),
                     produced=_produced(row), last_served_at=row["last_served_at"])

    def reference(self, tx: Transaction, key: AssetKey, ref: AssetReference) -> bool:
        conn = pg_connection(tx)
        now = self._clock.utc()
        # A no-op upsert rather than DO NOTHING: it row-locks the asset against a concurrent retire.
        conn.execute(
            "INSERT INTO assets (kind, identity, created_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (kind, identity) DO UPDATE SET created_at = assets.created_at",
            (key.kind.value, key.identity, now),
        )
        changed = ", ".join(f"{column} = EXCLUDED.{column}" for column in _REFERENCE_COLUMNS)
        differs = " OR ".join(f"asset_references.{column} IS DISTINCT FROM EXCLUDED.{column}"
                              for column in _REFERENCE_COLUMNS)
        cursor = conn.execute(
            "INSERT INTO asset_references (kind, identity, owner, locator_url, locator_sha256, "
            "locator_size, expected_size, expected_sha256, added_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (kind, identity, owner) DO UPDATE SET {changed} WHERE {differs}",
            (key.kind.value, key.identity, ref.owner, ref.locator.url, ref.locator.sha256,
             ref.locator.size, ref.expected_size, ref.expected_sha256, now),
        )
        return cursor.rowcount == 1

    def retire(self, tx: Transaction, key: AssetKey, owner: str) -> None:
        conn = pg_connection(tx)
        locked = conn.execute(
            "SELECT 1 FROM assets WHERE kind=%s AND identity=%s FOR UPDATE",
            (key.kind.value, key.identity),
        ).fetchone()
        if locked is None:
            return
        conn.execute(
            "DELETE FROM asset_references WHERE kind=%s AND identity=%s AND owner=%s",
            (key.kind.value, key.identity, owner),
        )
        conn.execute(
            "DELETE FROM assets a WHERE a.kind=%s AND a.identity=%s AND NOT EXISTS ("
            "SELECT 1 FROM asset_references r WHERE r.kind=a.kind AND r.identity=a.identity)",
            (key.kind.value, key.identity),
        )

    def record_produced(self, tx: Transaction, key: AssetKey, facts: AssetReady) -> None:
        conn = pg_connection(tx)
        written = conn.execute(
            "UPDATE assets SET produced_size=%s, produced_sha256=%s "
            "WHERE kind=%s AND identity=%s AND produced_sha256 IS NULL",
            (facts.size, facts.sha256, key.kind.value, key.identity),
        ).rowcount
        if written:
            return
        row = conn.execute(
            "SELECT produced_size, produced_sha256 FROM assets WHERE kind=%s AND identity=%s",
            (key.kind.value, key.identity),
        ).fetchone()
        if row is None:
            return  # absent -> no-op
        recorded = _produced(row)
        if recorded != facts:
            raise ProducedFactsConflict(f"{key}: recorded {recorded}, given {facts}")

    def forget_produced(self, tx: Transaction, key: AssetKey) -> None:
        pg_connection(tx).execute(
            "UPDATE assets SET produced_size=NULL, produced_sha256=NULL "
            "WHERE kind=%s AND identity=%s",
            (key.kind.value, key.identity),
        )

    def touch_served(self, tx: Transaction, key: AssetKey, at: float) -> None:
        pg_connection(tx).execute(
            "UPDATE assets SET last_served_at=%s WHERE kind=%s AND identity=%s",
            (at, key.kind.value, key.identity),
        )
