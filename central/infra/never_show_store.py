"""The Never show list over PostgreSQL (decision 0036, slice 3): the one repository of migration
134's `never_show` table. A photo is keyed as `library_assets` keys its Immich asset, so the
list names a photo's current revision (`asset_id`) whatever revision was listed. Every method runs
in the caller's transaction (Coordination's for a change, which the next planning pass reads).
"""
from __future__ import annotations

from typing import Any

from central.everyday.model import EverydayRefusal
from central.everyday.views import NeverShowEntry
from central.registry import RegistryError


def not_listed(asset_column: str) -> str:
    """The clause every read of a Scene's line, a pool's count and a fit count leaves a listed
    photo out with: no Never show row names the Immich asset whose current revision is
    `asset_column` (an SQL expression; a NULL one is kept)."""
    return ("NOT EXISTS (SELECT 1 FROM never_show n JOIN library_assets x "
            f"USING (connection_ref, upstream_id) WHERE x.asset_id={asset_column})")


class PgNeverShow:
    """Stateless; every method runs in the caller's transaction."""

    @staticmethod
    def listed_in(conn: Any) -> frozenset[str]:
        """The listed photos' current revisions."""
        return frozenset(row["asset_id"] for row in conn.execute(
            "SELECT l.asset_id FROM never_show n JOIN library_assets l USING (connection_ref, upstream_id) "
            "WHERE l.asset_id IS NOT NULL").fetchall())

    @staticmethod
    def requested_in(conn: Any, request_id: str) -> str | None:
        """The photo a request listed (its current revision), None when no such request."""
        row = conn.execute(
            "SELECT l.asset_id FROM never_show n JOIN library_assets l USING (connection_ref, upstream_id) "
            "WHERE n.request_id=%s", (request_id,)).fetchone()
        return None if row is None else row["asset_id"]

    @staticmethod
    def add_in(conn: Any, request_id: str, asset_id: str, *, now: float, by: str) -> bool:
        """List the photo `asset_id` names; False when it was listed already (nothing written).
        RegistryError("unknown_photo", 404) for a photo the library does not hold."""
        photo = conn.execute("SELECT connection_ref, upstream_id FROM library_assets WHERE asset_id=%s",
                             (asset_id,)).fetchone()
        if photo is None:
            raise RegistryError(EverydayRefusal.UNKNOWN_PHOTO.value, 404)
        return conn.execute(
            "INSERT INTO never_show(connection_ref,upstream_id,request_id,listed_at,created_at,created_by,"
            "changed_at,changed_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (connection_ref, upstream_id) DO NOTHING RETURNING request_id",
            (photo["connection_ref"], photo["upstream_id"], request_id, now, now, by, now, by),
        ).fetchone() is not None

    @staticmethod
    def remove_in(conn: Any, asset_id: str) -> bool:
        """Show again: take the photo off the list; False when it was not on it."""
        return conn.execute(
            "DELETE FROM never_show n USING library_assets l WHERE l.asset_id=%s "
            "AND (n.connection_ref, n.upstream_id)=(l.connection_ref, l.upstream_id) RETURNING n.request_id",
            (asset_id,)).fetchone() is not None

    @staticmethod
    def entries_in(conn: Any) -> tuple[NeverShowEntry, ...]:
        """Scenes › Never show list: every listed photo, newest first."""
        return tuple(NeverShowEntry(asset_id=row["asset_id"], kind=row["kind"], listed_at=row["listed_at"],
                                    by=row["created_by"]) for row in conn.execute(
            "SELECT l.asset_id, l.kind, n.listed_at, n.created_by FROM never_show n "
            "JOIN library_assets l USING (connection_ref, upstream_id) WHERE l.asset_id IS NOT NULL "
            "ORDER BY n.listed_at DESC, l.asset_id").fetchall())


__all__ = ["PgNeverShow", "not_listed"]
