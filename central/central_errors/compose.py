"""Central's errors composed from what room lights keep and what a Player said. Pure: every input is
an argument; nothing here judges a doubt (room lights does) or whether it blacks a Frame (the
Player does)."""
from __future__ import annotations

from collections.abc import Mapping

from central.central_errors.ports import Reference, SourceAdvice
from central.central_errors.views import (
    CentralErrorsView,
    FrameClear,
    FrameImpeded,
    FrameImpediment,
    FrameNotReported,
    SourceAttentionView,
)
from central.room_light.model import (
    ColourSensor,
    ColourSensorId,
    ColourSensorRef,
    Doubt,
    Facts,
    MeasuredLight,
    ReportedLight,
    RoomLight,
    RoomLightId,
    SensedColour,
    Sensor,
    SensorId,
    SensorRef,
    Source,
)
from central.room_light.views import DoubtView
from contracts.node_impeded import ImpededFrames


def in_doubt(lights: Mapping[RoomLightId, RoomLight], sensors: Mapping[SensorId, Sensor],
             colour_sensors: Mapping[ColourSensorId, ColourSensor], facts: Facts,
             naming: Mapping[RoomLightId, tuple[str, ...]],
             advice: Mapping[Source, SourceAdvice]) -> CentralErrorsView:
    """Every reference a room light, Sensor or colour sensor names that is in doubt now, in House ›
    Lights order (room lights' references first, then Sensors and colour sensors by id): the room
    lights it reports (its brightness or its colour), the Frames whose lighting (`naming`) names
    those lights, and what to do, from its source's `advice`. A reference nothing names is no one's
    attention. Pure."""
    reports: dict[Reference, list[RoomLightId]] = {}
    for light_id, light in lights.items():
        for reference in _references(light, sensors, colour_sensors):
            reports.setdefault(reference, []).append(light_id)
    for _, sensor in sorted(sensors.items()):
        reports.setdefault(sensor.at, [])
    for _, colour_sensor in sorted(colour_sensors.items()):
        reports.setdefault(colour_sensor.at, [])
    rows = []
    for reference, light_ids in reports.items():
        doubt = _doubt(reference, facts)
        if doubt is None:
            continue
        frames = tuple(dict.fromkeys(frame for light_id in light_ids for frame in naming.get(light_id, ())))
        rows.append(SourceAttentionView(
            source=reference.source, address=reference.address,
            doubt=DoubtView(reason=doubt.reason, since=doubt.since), lights=tuple(dict.fromkeys(light_ids)),
            frames=frames, what_to_do=advice[reference.source].what_to_do(reference, doubt.reason)))
    return CentralErrorsView(in_doubt=tuple(rows))


def _references(light: RoomLight, sensors: Mapping[SensorId, Sensor],
                colour_sensors: Mapping[ColourSensorId, ColourSensor]) -> tuple[Reference, ...]:
    """The references `light` reads: its reporter, or its Sensor's; and the colour sensor it follows."""
    found: list[Reference] = []
    if isinstance(light, ReportedLight):
        found.append(light.reporter)
    elif isinstance(light, MeasuredLight) and light.sensor in sensors:
        found.append(sensors[light.sensor].at)
    if isinstance(light.colour, SensedColour) and light.colour.sensor in colour_sensors:
        found.append(colour_sensors[light.colour.sensor].at)
    return tuple(found)


def _doubt(reference: Reference, facts: Facts) -> Doubt | None:
    kept = (facts.readings.get(reference) if isinstance(reference, SensorRef)
            else facts.colours.get(reference) if isinstance(reference, ColourSensorRef)
            else facts.lights.get(reference))
    return None if kept is None else kept.doubt


def impediment(frame_id: str, output_id: str, report: ImpededFrames | None) -> FrameImpediment:
    """What the Player of `frame_id`'s current binding (`output_id` on its Pi) last said: no report
    on its line, not reported; an entry for this Frame on this Output, impeded by it; else clear
    (an absent Frame is not impeded). Pure."""
    if report is None:
        return FrameNotReported()
    entry = next((each for each in report.frames if each.frame_id == frame_id and each.output_id == output_id), None)
    return FrameClear() if entry is None else FrameImpeded(by=entry)


__all__ = ["impediment", "in_doubt"]
