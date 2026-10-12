"""Waking the worker's Output document source (display_store's `DocumentSource` listens): the
channel and the one way to wake every Node, from app code in the caller's transaction (no
trigger). Its own module so the stores display_store reads (power, house) wake Nodes without an
import cycle.
"""
from __future__ import annotations

from typing import Any, Final

OUTPUT_DOCUMENTS_CHANNEL: Final = "photo_wall_output_documents"   # payload: the Node's device id


def notify_nodes_in(conn: Any, device_ids_sql: str, params: tuple[Any, ...] = ()) -> None:
    """Wake the document source of each Node `device_ids_sql` selects (column device_id),
    delivered when the caller's transaction commits."""
    conn.execute(f"SELECT pg_notify(%s, n.device_id) FROM ({device_ids_sql}) n",  # noqa: S608 (module SQL)
                 (OUTPUT_DOCUMENTS_CHANNEL, *params))


def wake_every_node_in(conn: Any) -> None:
    """Wake every unretired Node's document source (a change every timetable follows: Night off,
    a hold, the house's zone or place)."""
    notify_nodes_in(conn, "SELECT device_id FROM players WHERE retired_at IS NULL")


__all__ = ["OUTPUT_DOCUMENTS_CHANNEL", "notify_nodes_in", "wake_every_node_in"]
