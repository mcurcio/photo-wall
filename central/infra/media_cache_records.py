"""The PostgreSQL `MediaVariantUse` (`central.assets.maintenance`): which media variant files a
live pin holds (`media_references`, by variant name since 087) and when each was last served
(`assets.last_served_at`), both by the produced digest that names the file."""

from __future__ import annotations

from central.assets.maintenance import VariantFileUse
from central.db import MEDIA_LOCK, TransactionClock
from central.infra.transactions import pg_connection
from central.kernel.assets import AssetKind
from central.kernel.transactions import Transaction

_KIND = AssetKind.MEDIA_VARIANT.value


class PgMediaVariantUse:
    """Implements `central.assets.maintenance.MediaVariantUse`.

    A pin's `expires_at` is written on the media times (`MediaRepository.times`), so it is
    compared with the same clock: `times` is `DatabaseTransactionClock` in every process."""

    def __init__(self, *, times: TransactionClock) -> None:
        self._times = times

    def uses(self, tx: Transaction) -> dict[str, VariantFileUse]:
        conn = pg_connection(tx)
        now = self._times.now_in(conn)
        rows = conn.execute(
            "SELECT a.produced_sha256 AS digest, max(a.last_served_at) AS last_served_at, "
            "bool_or(EXISTS(SELECT 1 FROM media_references r WHERE r.variant_key=a.identity "
            "AND r.expires_at>%s)) AS pinned "
            "FROM assets a WHERE a.kind=%s AND a.produced_sha256 IS NOT NULL "
            "GROUP BY a.produced_sha256", (now, _KIND)).fetchall()
        return {row["digest"]: VariantFileUse(pinned=row["pinned"],
                                              last_served_at=row["last_served_at"])
                for row in rows}

    def lock(self, tx: Transaction) -> None:
        pg_connection(tx).execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))

    def pinned(self, tx: Transaction, digest: str) -> bool:
        conn = pg_connection(tx)
        return conn.execute(
            "SELECT 1 FROM assets a JOIN media_references r ON r.variant_key=a.identity "
            "WHERE a.kind=%s AND a.produced_sha256=%s AND r.expires_at>%s LIMIT 1",
            (_KIND, digest, self._times.now_in(conn))).fetchone() is not None
