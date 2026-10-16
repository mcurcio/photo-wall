"""Room lights' rules, each with one home (decision 0028). Pure: no I/O, no clock (callers pass
Central's time where a rule records it).

What a Home Assistant message says (`topic_entity`, `parse_*`, `light_state`, `illuminance_state`),
who reports what (`light_reported_by_entity`, `sensor_at_entity`, `sensor_on_player`,
`light_measured_by`), which House > Lights changes are refused (`check_*`), whether a Frame's
lighting fits the room lights (`check_frame_lighting`), the document every Pi draws with
(`room_lights_document`) and a Frame's lighting on upgrade (`upgrade_lighting`).
"""
from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping
from typing import Final, Literal

from central.room_light.model import (
    HOME_ASSISTANT_ENTITY_PATTERN,
    HOME_ASSISTANT_MESSAGE_BYTES,
    ILLUMINANCE_TOPIC_PREFIX,
    LIGHT_TOPIC_PREFIX,
    SENSORS_MAX,
    FixedReporter,
    FrameLightingRefusal,
    HomeAssistantLight,
    HomeAssistantSensor,
    IlluminanceMessage,
    LightStateMessage,
    OnPlayer,
    Reporter,
    RoomLight,
    Sensor,
    SensorReporter,
    SetupRefusal,
)
from contracts.frame_lighting import (
    DIMMER_AT_FULL,
    SHEET_WHITENESS,
    FrameLighting,
    Paper,
    WashLight,
)
from contracts.node_picture import DisplayLight
from contracts.room_light import (
    FULL_ON,
    LUX_MAX,
    ROOM_LIGHTS_MAX,
    Dimmer,
    Measured,
    NotReported,
    ReportedColour,
    RoomLightEntry,
    RoomLightsDocument,
    RoomLightState,
    Unavailable,
)
from contracts.strict_json import loads_object

UPGRADE_ROOM_LIGHT_ID = "room"          # the one fixed room light every Frame's upgrade wash views
UPGRADE_ROOM_LIGHT_NAME = "Room"

_ENTITY: Final = re.compile(HOME_ASSISTANT_ENTITY_PATTERN)
_OUT_OF_RANGE: Final = "out-of-range"


def topic_entity(topic: str) -> tuple[Literal["light", "illuminance"], str] | None:
    """("light", entity) for LIGHT_TOPIC_PREFIX + entity, ("illuminance", entity) for
    ILLUMINANCE_TOPIC_PREFIX + entity, where entity matches HOME_ASSISTANT_ENTITY_PATTERN whole;
    None for any other topic."""
    for kind, prefix in (("light", LIGHT_TOPIC_PREFIX), ("illuminance", ILLUMINANCE_TOPIC_PREFIX)):
        if topic.startswith(prefix):
            entity = topic[len(prefix):]
            return (kind, entity) if len(entity) <= 255 and _ENTITY.fullmatch(entity) else None
    return None


def _message(raw: bytes) -> dict[str, object]:
    value = loads_object(raw, max_bytes=HOME_ASSISTANT_MESSAGE_BYTES)
    if value is None:
        raise ValueError(_OUT_OF_RANGE)
    return value


def _byte(value: object) -> bool:
    return type(value) is int and 0 <= value <= 255


def parse_light_message(raw: bytes) -> LightStateMessage:
    """The message `raw` holds: strict JSON (contracts.strict_json) of at most
    HOME_ASSISTANT_MESSAGE_BYTES, an object with "state" ("on", "off", "unavailable" or "unknown";
    unknown becomes "unavailable") and optional "brightness" (0..255 int) and "rgb" ([r, g, b], each
    0..255 int); other keys are ignored (Home Assistant may add attributes). ValueError("out-of-range")
    for anything else. A null brightness or rgb is none sent (Home Assistant's off light)."""
    value = _message(raw)
    state = value.get("state")
    if state not in ("on", "off", "unavailable", "unknown"):
        raise ValueError(_OUT_OF_RANGE)
    brightness = value.get("brightness")
    if brightness is not None and not _byte(brightness):
        raise ValueError(_OUT_OF_RANGE)
    rgb = value.get("rgb")
    if rgb is not None and not (isinstance(rgb, list) and len(rgb) == 3 and all(_byte(item) for item in rgb)):
        raise ValueError(_OUT_OF_RANGE)
    return LightStateMessage("unavailable" if state == "unknown" else state, brightness,
                             None if rgb is None else (rgb[0], rgb[1], rgb[2]))


def parse_illuminance_message(raw: bytes) -> IlluminanceMessage:
    """{"lux": <number 0..LUX_MAX>} or {"state": "unavailable" | "unknown"} (lux None); strict JSON
    of at most HOME_ASSISTANT_MESSAGE_BYTES. ValueError("out-of-range") for anything else."""
    value = _message(raw)
    if "lux" in value:
        lux = value["lux"]
        if type(lux) not in (int, float) or not math.isfinite(lux) or not 0 <= lux <= LUX_MAX:
            raise ValueError(_OUT_OF_RANGE)
        return IlluminanceMessage(float(lux))
    if value.get("state") in ("unavailable", "unknown"):
        return IlluminanceMessage(None)
    raise ValueError(_OUT_OF_RANGE)


def light_state(message: LightStateMessage) -> Dimmer | Unavailable:
    """Home Assistant's light as a room light state: unavailable -> Unavailable(); off ->
    Dimmer(on=False, brightness=0.0 when none was sent, else brightness/255); on -> Dimmer(on=True,
    brightness = brightness/255, or 1.0 when none was sent; rgb = each/255, or None)."""
    if message.state == "unavailable":
        return Unavailable()
    rgb = None if message.rgb is None else (message.rgb[0] / 255, message.rgb[1] / 255, message.rgb[2] / 255)
    if message.state == "off":
        return Dimmer(on=False, brightness=0.0 if message.brightness is None else message.brightness / 255, rgb=rgb)
    return Dimmer(on=True, brightness=1.0 if message.brightness is None else message.brightness / 255, rgb=rgb)


def illuminance_state(message: IlluminanceMessage) -> Measured | Unavailable:
    """Measured(lux) or Unavailable() for lux None."""
    return Unavailable() if message.lux is None else Measured(lux=message.lux)


def light_reported_by_entity(lights: Collection[RoomLight], entity: str) -> RoomLight | None:
    """The room light whose reporter is HomeAssistantLight(entity), or None."""
    return next((light for light in lights if isinstance(light.reporter, HomeAssistantLight)
                 and light.reporter.entity == entity), None)


def sensor_at_entity(sensors: Collection[Sensor], entity: str) -> Sensor | None:
    """The Sensor at HomeAssistantSensor(entity), or None."""
    return next((sensor for sensor in sensors if isinstance(sensor.at, HomeAssistantSensor)
                 and sensor.at.entity == entity), None)


def sensor_on_player(sensors: Collection[Sensor], player_id: str) -> Sensor | None:
    """The Sensor at OnPlayer(player_id), or None (at most one per Pi)."""
    return next((sensor for sensor in sensors if isinstance(sensor.at, OnPlayer)
                 and sensor.at.player_id == player_id), None)


def light_measured_by(lights: Collection[RoomLight], sensor_id: str) -> RoomLight | None:
    """The room light whose reporter is SensorReporter(sensor_id), or None (a Sensor reports at most
    one room light)."""
    return next((light for light in lights if isinstance(light.reporter, SensorReporter)
                 and light.reporter.sensor == sensor_id), None)


def measured_by_a_sensor(reporter: Reporter) -> bool:
    """Whether a room light reported by `reporter` is a reading in lux (a SensorReporter), so a Frame
    light of it takes a lux reference; else it is a dimmer and takes DIMMER_AT_FULL. The one home of
    which reference fits which reporter."""
    return isinstance(reporter, SensorReporter)


def check_room_light(light: RoomLight, lights: Mapping[str, RoomLight], sensors: Mapping[str, Sensor],
                     named_by_frames: Collection[str]) -> SetupRefusal | None:
    """Whether saving `light` (new, or replacing lights[light.id]) is refused: a ReportedColour on
    anything but a HomeAssistantLight; a SensorReporter naming no Sensor, or a Sensor another room
    light already reports; a HomeAssistantLight entity another room light or a HomeAssistantSensor
    already names; a new light past ROOM_LIGHTS_MAX; a reporter that changes which reference a Frame
    light of it takes (`measured_by_a_sensor`) while any Frame names it (`named_by_frames`: the Frames
    whose saved lighting or open trial names it), NAMED_BY_A_FRAME, as removing it would be: their
    references would no longer fit and their lights would add nothing. None when it may be saved."""
    others = [other for key, other in lights.items() if key != light.id]
    reporter = light.reporter
    previous = lights.get(light.id)
    if (previous is not None and named_by_frames
            and measured_by_a_sensor(previous.reporter) != measured_by_a_sensor(reporter)):
        return SetupRefusal.NAMED_BY_A_FRAME
    if isinstance(light.colour, ReportedColour) and not isinstance(reporter, HomeAssistantLight):
        return SetupRefusal.REPORTED_COLOUR_NEEDS_HOME_ASSISTANT
    if isinstance(reporter, SensorReporter):
        if reporter.sensor not in sensors:
            return SetupRefusal.UNKNOWN_SENSOR
        if light_measured_by(others, reporter.sensor) is not None:
            return SetupRefusal.SENSOR_REPORTS_A_LIGHT
    if isinstance(reporter, HomeAssistantLight) and (
            light_reported_by_entity(others, reporter.entity) is not None
            or sensor_at_entity(sensors.values(), reporter.entity) is not None):
        return SetupRefusal.ENTITY_REPORTS_ANOTHER
    if light.id not in lights and len(lights) >= ROOM_LIGHTS_MAX:
        return SetupRefusal.TOO_MANY
    return None


def check_room_light_removal(light_id: str, lights: Mapping[str, RoomLight],
                             named_by_frames: Collection[str]) -> SetupRefusal | None:
    """UNKNOWN_ROOM_LIGHT when lights has no light_id; NAMED_BY_A_FRAME when any Frame's lighting
    names it (`named_by_frames`: the Frames whose saved lighting or open trial names it); else None."""
    if light_id not in lights:
        return SetupRefusal.UNKNOWN_ROOM_LIGHT
    if named_by_frames:
        return SetupRefusal.NAMED_BY_A_FRAME
    return None


def check_sensor(sensor: Sensor, sensors: Mapping[str, Sensor],
                 lights: Mapping[str, RoomLight]) -> SetupRefusal | None:
    """Whether saving `sensor` is refused: its HomeAssistantSensor entity already names another
    Sensor or a room light; an OnPlayer Pi that already has another Sensor; a new Sensor past
    SENSORS_MAX. None when it may be saved."""
    others = [other for key, other in sensors.items() if key != sensor.id]
    if isinstance(sensor.at, HomeAssistantSensor) and (
            sensor_at_entity(others, sensor.at.entity) is not None
            or light_reported_by_entity(lights.values(), sensor.at.entity) is not None):
        return SetupRefusal.ENTITY_REPORTS_ANOTHER
    if isinstance(sensor.at, OnPlayer) and sensor_on_player(others, sensor.at.player_id) is not None:
        return SetupRefusal.ENTITY_REPORTS_ANOTHER
    if sensor.id not in sensors and len(sensors) >= SENSORS_MAX:
        return SetupRefusal.TOO_MANY
    return None


def check_sensor_removal(sensor_id: str, sensors: Mapping[str, Sensor],
                         lights: Mapping[str, RoomLight]) -> SetupRefusal | None:
    """UNKNOWN_SENSOR when sensors has no sensor_id; SENSOR_REPORTS_A_LIGHT while a room light's
    reporter names it; else None."""
    if sensor_id not in sensors:
        return SetupRefusal.UNKNOWN_SENSOR
    if light_measured_by(lights.values(), sensor_id) is not None:
        return SetupRefusal.SENSOR_REPORTS_A_LIGHT
    return None


def check_frame_lighting(lighting: FrameLighting, lights: Mapping[str, RoomLight]) -> FrameLightingRefusal | None:
    """Whether a Frame's lighting fits the room lights: every Frame light names a room light
    (UNKNOWN_ROOM_LIGHT), and its reference matches its room light's reporter (REFERENCE_MISMATCH):
    DIMMER_AT_FULL for a HomeAssistantLight or FixedReporter, a lux for a SensorReporter."""
    for frame_light in lighting.lights:
        room_light = lights.get(frame_light.light)
        if room_light is None:
            return FrameLightingRefusal.UNKNOWN_ROOM_LIGHT
        if measured_by_a_sensor(room_light.reporter) == (frame_light.reference == DIMMER_AT_FULL):
            return FrameLightingRefusal.REFERENCE_MISMATCH
    return None


def room_lights_document(lights: Mapping[str, RoomLight], states: Mapping[str, RoomLightState],
                         change: int) -> RoomLightsDocument:
    """The document every Pi draws with: every room light with its colour and state. A
    FixedReporter's state is FULL_ON whatever `states` holds; a light `states` lacks is
    NotReported(). Pure; the same for every Pi."""
    return RoomLightsDocument(change=change, lights={
        light_id: RoomLightEntry(
            colour=light.colour,
            state=FULL_ON if isinstance(light.reporter, FixedReporter) else states.get(light_id, NotReported()))
        for light_id, light in sorted(lights.items())})


def upgrade_lighting(display: DisplayLight, mat_depth_mm: float) -> FrameLighting:
    """A Frame's lighting on upgrade, so today's picture is unchanged (decision 0028; with the
    sliders gone, "unchanged" is today's picture at neutral sliders): one WashLight of
    UPGRADE_ROOM_LIGHT_ID at display.matched_lux with reference DIMMER_AT_FULL, the mat's depth
    kept, and Paper(whiteness=SHEET_WHITENESS, ink_density=-log10(display.black), profile None), so
    the paper's black lands exactly on the panel's black and its white on the TV's white."""
    return FrameLighting(
        mat_depth_mm=mat_depth_mm,
        lights=(WashLight(light=UPGRADE_ROOM_LIGHT_ID, lux=display.matched_lux, reference=DIMMER_AT_FULL),),
        paper=Paper(whiteness=SHEET_WHITENESS, ink_density=-math.log10(display.black), profile=None))


def new_frame_lighting(display: DisplayLight, lights: Mapping[str, RoomLight]) -> FrameLighting:
    """A new Frame's lighting, so it shows what an upgraded Frame shows and passes
    `check_frame_lighting`: `upgrade_lighting(display, 0)` with its wash viewing the first fixed
    (always-on) room light in `lights`' order (the seeded "room" while it is there), never a light that
    may be gone; with no fixed room light, no light (the Frame shows the panel's black until its
    lighting is set on its Position tab). Pure."""
    upgraded = upgrade_lighting(display, 0.0)
    fixed = next((light_id for light_id, light in lights.items() if isinstance(light.reporter, FixedReporter)), None)
    if fixed is None:
        return upgraded.model_copy(update={"lights": ()})
    return upgraded.model_copy(update={"lights": tuple(
        light.model_copy(update={"light": fixed}) for light in upgraded.lights)})


__all__ = [
    "UPGRADE_ROOM_LIGHT_ID", "UPGRADE_ROOM_LIGHT_NAME", "check_frame_lighting", "check_room_light",
    "check_room_light_removal", "check_sensor", "check_sensor_removal", "illuminance_state", "light_measured_by",
    "measured_by_a_sensor", "new_frame_lighting",
    "light_reported_by_entity", "light_state", "parse_illuminance_message", "parse_light_message",
    "room_lights_document", "sensor_at_entity", "sensor_on_player", "topic_entity", "upgrade_lighting",
]
