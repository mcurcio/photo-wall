"""The PostgreSQL `MediaVariantUse` (`central.assets.maintenance`): which media variant files a
pin holds (`media_references`, by variant name since 087) and when each was last served
(`assets.last_served_at`), both by the produced digest that names the file."""

from __future__ import annotations

from central.assets.maintenance import VariantFileUse
from central.db import MEDIA_LOCK
from central.infra.transactions import pg_connection
from central.kernel.assets import AssetKind
from central.kernel.transactions import Transaction

_KIND = AssetKind.MEDIA_VARIANT.value


class PgMediaVariantUse:
    """Implements `central.assets.maintenance.MediaVariantUse`.

    Any `media_references` row holds its file, whatever its `expires_at`: pins are written on
    Central's process clock (an offer's `valid_until`, a lock's `end`, a transfer's grant time),
    so only Central expires them (the Coordinator's `expire_pins_in`, a transfer's release). The
    worker that runs this never compares a pin's time with a clock of its own (never compare
    clocks across systems; the old media store's GC held the same rule)."""

    def uses(self, tx: Transaction) -> dict[str, VariantFileUse]:
        rows = pg_connection(tx).execute(
            "SELECT a.produced_sha256 AS digest, max(a.last_served_at) AS last_served_at, "
            "bool_or(EXISTS(SELECT 1 FROM media_references r WHERE r.variant_key=a.identity)) "
            "AS pinned FROM assets a WHERE a.kind=%s AND a.produced_sha256 IS NOT NULL "
            "GROUP BY a.produced_sha256", (_KIND,)).fetchall()
        return {row["digest"]: VariantFileUse(pinned=row["pinned"],
                                              last_served_at=row["last_served_at"])
                for row in rows}

    def lock(self, tx: Transaction) -> None:
        pg_connection(tx).execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))

    def pinned(self, tx: Transaction, digest: str) -> bool:
        return pg_connection(tx).execute(
            "SELECT 1 FROM assets a JOIN media_references r ON r.variant_key=a.identity "
            "WHERE a.kind=%s AND a.produced_sha256=%s LIMIT 1",
            (_KIND, digest)).fetchone() is not None
