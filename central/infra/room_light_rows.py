"""The house's room-light setup read in a caller's transaction (decision 0034, migration 120): the one
layout of the `singletons` document 'room_light_setup' (every room light with its House › Lights
position, every Sensor and colour sensor, and the room-lights document's change number), shared by
the room-light store and the Frame-lighting check, which each import it and never each other. The
setup is one document because "a colour sensor a light follows stays" is a rule across lights and
sensors; its row is also the house's lock (central/infra/frame_lighting_store.py `PgHouseLock`)."""
from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, TypeAdapter

from central.infra.singletons import Singleton
from central.room_light.model import (
    ColourSensor,
    ColourSensorId,
    RoomLight,
    RoomLightId,
    Sensor,
    SensorId,
)

ROOM_LIGHT: Final = TypeAdapter(RoomLight)


class _PlacedLight(BaseModel):
    """A room light and its place in House › Lights' order."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    light: RoomLight
    position: int


class _RoomLightSetup(BaseModel):
    """The house's room-light setup as stored: lights by id with their position, Sensors and colour
    sensors by id, and the room-lights document's change number."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    lights: dict[RoomLightId, _PlacedLight]
    sensors: dict[SensorId, Sensor]
    colour_sensors: dict[ColourSensorId, ColourSensor]
    change: int


ROOM_LIGHT_SETUP: Final[Singleton[_RoomLightSetup]] = Singleton("room_light_setup", _RoomLightSetup)
_EMPTY: Final = _RoomLightSetup(lights={}, sensors={}, colour_sensors={}, change=0)


def setup_in(conn: Any) -> _RoomLightSetup:
    """The stored setup (migration 120 creates its row; none reads as an empty house at change 0)."""
    stored = ROOM_LIGHT_SETUP.read_in(conn)
    return _EMPTY if stored is None else stored.value


def lights_of(setup: _RoomLightSetup) -> dict[RoomLightId, RoomLight]:
    """Every room light of `setup` by id, in the house's order."""
    return {light_id: placed.light for light_id, placed in
            sorted(setup.lights.items(), key=lambda pair: (pair[1].position, pair[0]))}


def lights_in(conn: Any) -> dict[RoomLightId, RoomLight]:
    """Every room light by id, in the house's order."""
    return lights_of(setup_in(conn))


__all__ = ["ROOM_LIGHT", "ROOM_LIGHT_SETUP", "_PlacedLight", "_RoomLightSetup", "lights_in", "lights_of",
           "setup_in"]
