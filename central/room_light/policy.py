"""Room lights' rules, each with one home (decision 0028). Pure: no I/O, no clock (callers pass
Central's time where a rule records it).

Implemented by the central lane; signatures frozen by the milestone's interface commit.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Literal

from central.room_light.model import (
    FrameLightingRefusal,
    IlluminanceMessage,
    LightStateMessage,
    RoomLight,
    Sensor,
    SetupRefusal,
)
from contracts.frame_lighting import FrameLighting
from contracts.node_picture import DisplayLight
from contracts.room_light import Dimmer, Measured, RoomLightsDocument, RoomLightState, Unavailable

UPGRADE_ROOM_LIGHT_ID = "room"          # the one fixed room light every Frame's upgrade wash views
UPGRADE_ROOM_LIGHT_NAME = "Room"


def topic_entity(topic: str) -> tuple[Literal["light", "illuminance"], str] | None:
    """("light", entity) for LIGHT_TOPIC_PREFIX + entity, ("illuminance", entity) for
    ILLUMINANCE_TOPIC_PREFIX + entity, where entity matches HOME_ASSISTANT_ENTITY_PATTERN whole;
    None for any other topic."""
    raise NotImplementedError


def parse_light_message(raw: bytes) -> LightStateMessage:
    """The message `raw` holds: strict JSON (contracts.strict_json) of at most
    HOME_ASSISTANT_MESSAGE_BYTES, an object with "state" ("on", "off", "unavailable" or "unknown";
    unknown becomes "unavailable") and optional "brightness" (0..255 int) and "rgb" ([r, g, b], each
    0..255 int); other keys are ignored (Home Assistant may add attributes). ValueError("out-of-range")
    for anything else."""
    raise NotImplementedError


def parse_illuminance_message(raw: bytes) -> IlluminanceMessage:
    """{"lux": <number 0..LUX_MAX>} or {"state": "unavailable" | "unknown"} (lux None); strict JSON
    of at most HOME_ASSISTANT_MESSAGE_BYTES. ValueError("out-of-range") for anything else."""
    raise NotImplementedError


def light_state(message: LightStateMessage) -> Dimmer | Unavailable:
    """Home Assistant's light as a room light state: unavailable -> Unavailable(); off ->
    Dimmer(on=False, brightness=0.0 when none was sent, else brightness/255); on -> Dimmer(on=True,
    brightness = brightness/255, or 1.0 when none was sent; rgb = each/255, or None)."""
    raise NotImplementedError


def illuminance_state(message: IlluminanceMessage) -> Measured | Unavailable:
    """Measured(lux) or Unavailable() for lux None."""
    raise NotImplementedError


def light_reported_by_entity(lights: Collection[RoomLight], entity: str) -> RoomLight | None:
    """The room light whose reporter is HomeAssistantLight(entity), or None."""
    raise NotImplementedError


def sensor_at_entity(sensors: Collection[Sensor], entity: str) -> Sensor | None:
    """The Sensor at HomeAssistantSensor(entity), or None."""
    raise NotImplementedError


def sensor_on_player(sensors: Collection[Sensor], player_id: str) -> Sensor | None:
    """The Sensor at OnPlayer(player_id), or None (at most one per Pi)."""
    raise NotImplementedError


def light_measured_by(lights: Collection[RoomLight], sensor_id: str) -> RoomLight | None:
    """The room light whose reporter is SensorReporter(sensor_id), or None (a Sensor reports at most
    one room light)."""
    raise NotImplementedError


def check_room_light(light: RoomLight, lights: Mapping[str, RoomLight],
                     sensors: Mapping[str, Sensor]) -> SetupRefusal | None:
    """Whether saving `light` (new, or replacing lights[light.id]) is refused: a ReportedColour on
    anything but a HomeAssistantLight; a SensorReporter naming no Sensor, or a Sensor another room
    light already reports; a HomeAssistantLight entity another room light or a HomeAssistantSensor
    already names; a new light past ROOM_LIGHTS_MAX. None when it may be saved."""
    raise NotImplementedError


def check_room_light_removal(light_id: str, lights: Mapping[str, RoomLight],
                             named_by_frames: Collection[str]) -> SetupRefusal | None:
    """UNKNOWN_ROOM_LIGHT when lights has no light_id; NAMED_BY_A_FRAME when any Frame's lighting
    names it (`named_by_frames`: the Frames whose saved lighting or open trial names it); else None."""
    raise NotImplementedError


def check_sensor(sensor: Sensor, sensors: Mapping[str, Sensor],
                 lights: Mapping[str, RoomLight]) -> SetupRefusal | None:
    """Whether saving `sensor` is refused: its HomeAssistantSensor entity already names another
    Sensor or a room light; an OnPlayer Pi that already has another Sensor; a new Sensor past
    SENSORS_MAX. None when it may be saved."""
    raise NotImplementedError


def check_sensor_removal(sensor_id: str, sensors: Mapping[str, Sensor],
                         lights: Mapping[str, RoomLight]) -> SetupRefusal | None:
    """UNKNOWN_SENSOR when sensors has no sensor_id; SENSOR_REPORTS_A_LIGHT while a room light's
    reporter names it; else None."""
    raise NotImplementedError


def check_frame_lighting(lighting: FrameLighting, lights: Mapping[str, RoomLight]) -> FrameLightingRefusal | None:
    """Whether a Frame's lighting fits the room lights: every Frame light names a room light
    (UNKNOWN_ROOM_LIGHT), and its reference matches its room light's reporter (REFERENCE_MISMATCH):
    DIMMER_AT_FULL for a HomeAssistantLight or FixedReporter, a lux for a SensorReporter."""
    raise NotImplementedError


def room_lights_document(lights: Mapping[str, RoomLight], states: Mapping[str, RoomLightState],
                         change: int) -> RoomLightsDocument:
    """The document every Pi draws with: every room light with its colour and state. A
    FixedReporter's state is FULL_ON whatever `states` holds; a light `states` lacks is
    NotReported(). Pure; the same for every Pi."""
    raise NotImplementedError


def upgrade_lighting(display: DisplayLight, mat_depth_mm: float) -> FrameLighting:
    """A Frame's lighting on upgrade, so today's picture is unchanged (decision 0028; with the
    sliders gone, "unchanged" is today's picture at neutral sliders): one WashLight of
    UPGRADE_ROOM_LIGHT_ID at display.matched_lux with reference DIMMER_AT_FULL, the mat's depth
    kept, and Paper(whiteness=SHEET_WHITENESS, ink_density=-log10(display.black), profile None), so
    the paper's black lands exactly on the panel's black and its white on the TV's white."""
    raise NotImplementedError


__all__ = [
    "UPGRADE_ROOM_LIGHT_ID", "UPGRADE_ROOM_LIGHT_NAME", "check_frame_lighting", "check_room_light",
    "check_room_light_removal", "check_sensor", "check_sensor_removal", "illuminance_state", "light_measured_by",
    "light_reported_by_entity", "light_state", "parse_illuminance_message", "parse_light_message",
    "room_lights_document", "sensor_at_entity", "sensor_on_player", "topic_entity", "upgrade_lighting",
]
