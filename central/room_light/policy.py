"""Room lights' rules, each with one home. Pure: no I/O, no clock.

What each room light reads as now (`entry_of`, `room_lights_document`), which House › Lights
changes are refused (`check_*`), and how a Player's drawn report reads against the document now
(`drawn_state`).
"""
from __future__ import annotations

from collections.abc import Mapping

from central.room_light.model import (
    SENSORS_MAX,
    FixedLight,
    MeasuredLight,
    ReportedLight,
    RoomLight,
    RoomLightId,
    RoomLightRefusal,
    RoomLightRefused,
    Sensor,
    SensorId,
    Words,
)
from central.room_light.views import DrawnState
from contracts.room_light import (
    ROOM_LIGHTS_DRAWN_LEASE_SECONDS,
    ROOM_LIGHTS_MAX,
    ColourDimmer,
    ColourEntry,
    Dimmer,
    FixedColour,
    FixedEntry,
    MeasuredEntry,
    NotReported,
    RoomLightEntry,
    RoomLightsDocument,
    RoomLightsDrawn,
    WhiteEntry,
)


def entry_of(light: RoomLight, words: Words, sensors: Mapping[SensorId, Sensor]) -> RoomLightEntry:
    """`light` as the document's entry of its kind, with its reference's last word in `words`: a
    FixedLight is always on, at full; a ReportedLight of a configured colour takes a ColourDimmer's on
    and brightness (its colour wins); a ReportedLight of a reported colour takes a Dimmer as driven
    white (1, 1, 1); a MeasuredLight its Sensor's reading. No word (or no such Sensor): NotReported."""
    if isinstance(light, FixedLight):
        return FixedEntry(colour=light.colour)
    if isinstance(light, MeasuredLight):
        sensor = sensors.get(light.sensor)
        reading = None if sensor is None else words.readings.get(sensor.at)
        return MeasuredEntry(colour=light.colour, state=NotReported() if reading is None else reading)
    word = words.lights.get(light.reporter)
    if isinstance(light.colour, FixedColour):
        if isinstance(word, ColourDimmer):
            word = Dimmer(on=word.on, brightness=word.brightness)
        return WhiteEntry(colour=light.colour, state=NotReported() if word is None else word)
    if isinstance(word, Dimmer):
        word = ColourDimmer(on=word.on, brightness=word.brightness, rgb=(1.0, 1.0, 1.0))
    return ColourEntry(colour=light.colour, state=NotReported() if word is None else word)


def room_lights_document(lights: Mapping[RoomLightId, RoomLight], sensors: Mapping[SensorId, Sensor], words: Words,
                         change: int) -> RoomLightsDocument:
    """The document every Pi draws with: every room light's entry (`entry_of`). The same for every Pi."""
    return RoomLightsDocument(change=change, lights={
        light_id: entry_of(light, words, sensors) for light_id, light in sorted(lights.items())})


def measured_by(lights: Mapping[RoomLightId, RoomLight], sensor_id: SensorId) -> RoomLightId | None:
    """The room light that measures Sensor `sensor_id`, or None (a Sensor measures at most one)."""
    return next((light_id for light_id, light in lights.items()
                 if isinstance(light, MeasuredLight) and light.sensor == sensor_id), None)


def _references(lights: Mapping[RoomLightId, RoomLight], sensors: Mapping[SensorId, Sensor]) -> set[tuple[str, str]]:
    """Every (source, address) a room light's reporter or a Sensor names."""
    taken = {(light.reporter.source, light.reporter.address) for light in lights.values()
             if isinstance(light, ReportedLight)}
    return taken | {(sensor.at.source, sensor.at.address) for sensor in sensors.values()}


def check_room_light(light_id: RoomLightId, light: RoomLight, lights: Mapping[RoomLightId, RoomLight],
                     sensors: Mapping[SensorId, Sensor], named_by: tuple[str, ...]) -> RoomLightRefused | None:
    """Whether saving `light` as `light_id` (new, or replacing lights[light_id]) is refused, in this
    order: a change between MeasuredLight and any other kind while a Frame names it (`named_by`: its
    Frame lights' references would no longer fit), NAMED_BY_A_FRAME; a MeasuredLight naming no Sensor
    (UNKNOWN_SENSOR) or a Sensor another light measures (SENSOR_REPORTS_A_LIGHT); a reporter
    another light or a Sensor already names (REFERENCE_TAKEN); a new light past ROOM_LIGHTS_MAX
    (TOO_MANY). None when it may be saved."""
    previous = lights.get(light_id)
    others = {key: other for key, other in lights.items() if key != light_id}
    if (previous is not None and named_by
            and isinstance(previous, MeasuredLight) != isinstance(light, MeasuredLight)):
        return RoomLightRefused(RoomLightRefusal.NAMED_BY_A_FRAME)
    if isinstance(light, MeasuredLight):
        if light.sensor not in sensors:
            return RoomLightRefused(RoomLightRefusal.UNKNOWN_SENSOR)
        if measured_by(others, light.sensor) is not None:
            return RoomLightRefused(RoomLightRefusal.SENSOR_REPORTS_A_LIGHT)
    if isinstance(light, ReportedLight) and (
            (light.reporter.source, light.reporter.address) in _references(others, sensors)):
        return RoomLightRefused(RoomLightRefusal.REFERENCE_TAKEN)
    if previous is None and len(lights) >= ROOM_LIGHTS_MAX:
        return RoomLightRefused(RoomLightRefusal.TOO_MANY)
    return None


def check_room_light_removal(light_id: RoomLightId, lights: Mapping[RoomLightId, RoomLight],
                             named_by: tuple[str, ...]) -> RoomLightRefused | None:
    """UNKNOWN_ROOM_LIGHT when there is no such light; NAMED_BY_A_FRAME while a Frame's lighting
    names it; else None."""
    if light_id not in lights:
        return RoomLightRefused(RoomLightRefusal.UNKNOWN_ROOM_LIGHT)
    if named_by:
        return RoomLightRefused(RoomLightRefusal.NAMED_BY_A_FRAME)
    return None


def check_sensor(sensor_id: SensorId, sensor: Sensor, sensors: Mapping[SensorId, Sensor],
                 lights: Mapping[RoomLightId, RoomLight]) -> RoomLightRefused | None:
    """Whether saving `sensor` as `sensor_id` is refused: its reference already names another Sensor
    or a room light's reporter (REFERENCE_TAKEN: a second Sensor on one Pi is this); a new Sensor past
    SENSORS_MAX (TOO_MANY). None when it may be saved."""
    others = {key: other for key, other in sensors.items() if key != sensor_id}
    if (sensor.at.source, sensor.at.address) in _references(lights, others):
        return RoomLightRefused(RoomLightRefusal.REFERENCE_TAKEN)
    if sensor_id not in sensors and len(sensors) >= SENSORS_MAX:
        return RoomLightRefused(RoomLightRefusal.TOO_MANY)
    return None


def check_sensor_removal(sensor_id: SensorId, sensors: Mapping[SensorId, Sensor],
                         lights: Mapping[RoomLightId, RoomLight]) -> RoomLightRefused | None:
    """UNKNOWN_SENSOR when there is no such Sensor; SENSOR_REPORTS_A_LIGHT while a room light
    is measured by it; else None."""
    if sensor_id not in sensors:
        return RoomLightRefused(RoomLightRefusal.UNKNOWN_SENSOR)
    if measured_by(lights, sensor_id) is not None:
        return RoomLightRefused(RoomLightRefusal.SENSOR_REPORTS_A_LIGHT)
    return None


def drawn_state(report: RoomLightsDrawn | None, age_seconds: float | None, digest_now: str) -> DrawnState:
    """A Player's drawn report against the document now (`digest_now`): "not-reported" with no report
    or one older than ROOM_LIGHTS_DRAWN_LEASE_SECONDS on Central's clock; "not-following" when its
    reader is not following WALL; "drawn" when it drew the document now; else "behind"."""
    if report is None or age_seconds is None or age_seconds > ROOM_LIGHTS_DRAWN_LEASE_SECONDS:
        return "not-reported"
    if not report.following:
        return "not-following"
    return "drawn" if report.drawn == digest_now else "behind"


__all__ = ["check_room_light", "check_room_light_removal", "check_sensor", "check_sensor_removal", "drawn_state",
           "entry_of", "measured_by", "room_lights_document"]
