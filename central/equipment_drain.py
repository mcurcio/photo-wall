"""Read side of the Runtime admission fence for a withdrawn Player.

Readers fail closed on an active `equipment_drains` row. The V1 command path that
wrote drains was removed; no Central code writes one now, so these reads admit
everything until a future withdrawal owner writes the table again.
"""

from __future__ import annotations

from central.registry import RegistryError


def control_fence_in(conn, player_id: str) -> dict | None:
    """Salt the v2 challenge with the drain cut even when visible state is unchanged."""
    row = conn.execute(
        "SELECT attempt_id,phase,prepared_at FROM active_equipment_drains "
        "WHERE player_id=%s", (player_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def fenced_players_in(conn) -> frozenset[str]:
    """Read the durable fence inside a Coordination-locked transaction."""
    return frozenset(row["player_id"] for row in conn.execute(
        "SELECT player_id FROM active_equipment_drains"
    ).fetchall())


def require_unfenced_player_in(conn, player_id: str) -> None:
    if conn.execute(
        "SELECT 1 FROM active_equipment_drains WHERE player_id=%s", (player_id,),
    ).fetchone():
        raise RegistryError("equipment_draining")


def require_unfenced_frame_in(conn, frame_id: str) -> None:
    if conn.execute(
        "SELECT 1 FROM bindings b JOIN active_equipment_drains d ON d.player_id=b.player_id "
        "WHERE b.frame_id=%s", (frame_id,),
    ).fetchone():
        raise RegistryError("equipment_draining")
