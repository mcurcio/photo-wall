"""The room lights' rows read in a caller's transaction: the one layout of `room_lights` (migration
099), shared by the room-light store and the Frame-lighting check, which each import it and never
each other."""
from __future__ import annotations

from typing import Any, Final

from pydantic import TypeAdapter

from central.room_light.model import RoomLight, RoomLightId

ROOM_LIGHT: Final = TypeAdapter(RoomLight)


def lights_in(conn: Any) -> dict[RoomLightId, RoomLight]:
    """Every room light by id, in the house's order."""
    rows = conn.execute("SELECT id, light FROM room_lights ORDER BY position, id").fetchall()
    return {row["id"]: ROOM_LIGHT.validate_python(row["light"]) for row in rows}


__all__ = ["ROOM_LIGHT", "lights_in"]
