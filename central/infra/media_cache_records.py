"""The PostgreSQL `MediaVariantUse` (`central.assets.maintenance`): which media variant files an
offer or a secured lock holds (decision 0034: the pin is derived, never stored) and when each was
last served (`assets.last_served_at`), both by the produced digest that names the file."""

from __future__ import annotations

from typing import Final

from central.assets.maintenance import SERVED_SPARE_SECONDS, VariantFileUse
from central.db import MEDIA_LOCK
from central.infra.transactions import pg_connection
from central.kernel.assets import AssetKind
from central.kernel.transactions import Transaction

_KIND = AssetKind.MEDIA_VARIANT.value
# Every variant an offer or a secured lock names, as `key` (`Variant.key`, its name
# `<asset_id>.<recipe_id>`): the derived pins.
_HELD: Final = (
    "SELECT v->>'asset_id' || '.' || (v->>'recipe_id') AS key FROM plan_offers, "
    "jsonb_path_query(manifest, '$.layers[*].variant') AS v UNION "
    "SELECT layer->'variant'->>'asset_id' || '.' || (layer->'variant'->>'recipe_id') AS key "
    "FROM assignment_locks")
# A variant served this recently, by the database's clock alone (it wrote `last_served_at`): a
# transfer may still be reading it.
_SERVED: Final = ("a.last_served_at >= EXTRACT(EPOCH FROM clock_timestamp()) - "
                  f"{SERVED_SPARE_SECONDS}")


class PgMediaVariantUse:
    """Implements `central.assets.maintenance.MediaVariantUse`.

    A file is held while any offer or secured lock row names a variant it is the file of,
    whatever the row's `valid_until`: those times are Central's process clock, so only Central
    expires them (the Coordinator's cleanup deletes expired offers and locks). The worker that
    runs this never compares such a time with a clock of its own (never compare clocks across
    systems; the old media store's GC held the same rule). A file served within
    `SERVED_SPARE_SECONDS` is held too: the grant stamps `last_served_at` (the database's clock,
    compared only with the database's), so a transfer is never cut short."""

    def uses(self, tx: Transaction) -> dict[str, VariantFileUse]:
        rows = pg_connection(tx).execute(
            "SELECT a.produced_sha256 AS digest, max(a.last_served_at) AS last_served_at, "
            f"bool_or(h.key IS NOT NULL OR {_SERVED}) AS pinned "  # noqa: S608
            f"FROM assets a LEFT JOIN ({_HELD}) AS h ON h.key=a.identity "
            "WHERE a.kind=%s AND a.produced_sha256 IS NOT NULL "
            "GROUP BY a.produced_sha256", (_KIND,)).fetchall()
        return {row["digest"]: VariantFileUse(pinned=row["pinned"],
                                              last_served_at=row["last_served_at"])
                for row in rows}

    def lock(self, tx: Transaction) -> None:
        pg_connection(tx).execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))

    def pinned(self, tx: Transaction, digest: str) -> bool:
        return pg_connection(tx).execute(
            f"SELECT 1 FROM assets a WHERE a.kind=%s AND a.produced_sha256=%s AND ({_SERVED} OR "  # noqa: S608
            f"a.identity IN ({_HELD})) LIMIT 1", (_KIND, digest)).fetchone() is not None
