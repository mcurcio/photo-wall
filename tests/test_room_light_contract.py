"""The room-light wire facts (decision 0028): the room-lights document, the light sensor reading, a
Frame's lighting and the Display's light, each strict and round-tripping."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from contracts.frame_lighting import (
    FRAME_LIGHTS_MAX,
    CloseLight,
    FarLight,
    FrameLighting,
    Paper,
    WashLight,
)
from contracts.lut import LutRef
from contracts.node_calibration import MAX_LIGHTING_BYTES
from contracts.node_picture import UPGRADE_DISPLAY_LIGHT, DisplayLight
from contracts.room_light import (
    FULL_ON,
    ROOM_LIGHTS_BYTES,
    ROOM_LIGHTS_MAX,
    Dimmer,
    FixedColour,
    LightSensorReading,
    Measured,
    NotReported,
    ReportedColour,
    RoomLightEntry,
    RoomLightsDocument,
    Unavailable,
    decode_light_sensor,
    decode_room_lights,
    encode_light_sensor,
    encode_room_lights,
)

DOCUMENT = RoomLightsDocument(change=7, lights={
    "room": RoomLightEntry(FixedColour(6500), FULL_ON),
    "bookshelf-spot": RoomLightEntry(ReportedColour((0.3, 0.4, 0.3)), Dimmer(True, 0.5, (1.0, 0.5, 0.25))),
    "daylight": RoomLightEntry(FixedColour(5500), Measured(320.5)),
    "overheads": RoomLightEntry(FixedColour(2700), Unavailable()),
    "lamp": RoomLightEntry(FixedColour(2700), NotReported()),
})


def test_the_room_lights_document_round_trips_every_state_and_colour():
    raw = encode_room_lights(DOCUMENT)
    assert decode_room_lights(raw) == DOCUMENT
    assert json.loads(raw)["lights"]["bookshelf-spot"] == {
        "colour": {"channel_shares": [0.3, 0.4, 0.3], "kind": "reported"},
        "state": {"brightness": 0.5, "kind": "dimmer", "on": True, "rgb": [1.0, 0.5, 0.25]}}


def test_the_largest_document_fits_its_cap():
    """Mutation probe: ROOM_LIGHTS_BYTES 8192 -> the largest valid document is refused."""
    entry = RoomLightEntry(ReportedColour((1 / 3, 1 / 3, 1 / 3)), Dimmer(True, 1 / 3, (1 / 3, 1 / 3, 1 / 3)))
    largest = RoomLightsDocument(change=2**63 - 1, lights={f"{'x' * 29}{i:03d}": entry for i in range(ROOM_LIGHTS_MAX)})
    assert len(encode_room_lights(largest)) <= ROOM_LIGHTS_BYTES


@pytest.mark.parametrize("change", [
    lambda value: value["lights"]["room"]["state"].update(kind="dimmed"),
    lambda value: value["lights"]["room"]["state"].update(brightness=1.5),
    lambda value: value["lights"]["room"]["colour"].update(kelvin=6550),
    lambda value: value["lights"]["daylight"]["state"].update(lux=-1),
    lambda value: value["lights"]["bookshelf-spot"]["state"].update(rgb=[1, 1]),
    lambda value: value["lights"].update({"Bad Id": value["lights"]["room"]}),
    lambda value: value.update(change=0),
    lambda value: value["lights"]["room"]["state"].pop("rgb"),
    lambda value: value["lights"]["room"].update(extra=1),
])
def test_the_document_is_strict(change):
    value = json.loads(encode_room_lights(DOCUMENT))
    change(value)
    with pytest.raises(ValueError, match="^room_lights_document$"):
        decode_room_lights(json.dumps(value).encode())


def test_the_light_sensor_reading_says_lux_or_unavailable():
    for reading in (LightSensorReading(12.5), LightSensorReading(None)):
        assert decode_light_sensor(encode_light_sensor(reading)) == reading
    with pytest.raises(ValueError, match="^light_sensor_reading$"):
        decode_light_sensor(b'{"lux":-1}')


def _lighting(*lights) -> FrameLighting:
    return FrameLighting(mat_depth_mm=4.5, lights=lights, paper=Paper(whiteness=0.9, ink_density=2.3))


def test_a_frame_lighting_holds_the_three_kinds_and_its_paper():
    lighting = FrameLighting(
        mat_depth_mm=4.5,
        lights=(WashLight(light="room", lux=150, reference="dimmer-at-full"),
                FarLight(light="daylight", lux=300, reference=1200, from_degrees=270, steep_degrees=30,
                         softness_degrees=20),
                CloseLight(light="bookshelf-spot", lux=400, reference="dimmer-at-full", across_mm=0, up_mm=350,
                           out_mm=250, inner_beam_degrees=20, outer_beam_degrees=40)),
        paper=Paper(whiteness=0.92, ink_density=2.3, profile=LutRef("ab" * 32, "lut33-perceptual-relcol-bpc-1")))
    assert FrameLighting.model_validate_json(lighting.model_dump_json()) == lighting


@pytest.mark.parametrize("bad", [
    lambda: CloseLight(light="s", lux=1, reference="dimmer-at-full", across_mm=0, up_mm=0, out_mm=1,
                       inner_beam_degrees=40, outer_beam_degrees=40),
    lambda: WashLight(light="s", lux=0, reference="dimmer-at-full"),
    lambda: WashLight(light="s", lux=1, reference="at-full"),
    lambda: Paper(whiteness=0.9, ink_density=0.1),
    lambda: _lighting(*(WashLight(light="s", lux=1, reference=1) for _ in range(FRAME_LIGHTS_MAX + 1))),
])
def test_a_frame_lighting_refuses_what_has_no_physical_meaning(bad):
    with pytest.raises(ValidationError):
        bad()


def test_the_largest_frame_lighting_fits_the_trial():
    """Mutation probe: MAX_LIGHTING_BYTES 1024 -> four close lights with a paper profile do not fit."""
    close = CloseLight(light="a" * 32, lux=199999.25, reference=123456.75, across_mm=-4999.25, up_mm=-4999.25,
                       out_mm=4999.25, inner_beam_degrees=169.875, outer_beam_degrees=170.0)
    lighting = FrameLighting(mat_depth_mm=19.5, lights=(close,) * FRAME_LIGHTS_MAX,
                             paper=Paper(whiteness=0.912345, ink_density=2.345678,
                                         profile=LutRef("f" * 64, "z" * 64)))
    raw = json.dumps(lighting.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    assert len(raw.encode()) <= MAX_LIGHTING_BYTES


def test_the_display_light_has_physical_ranges():
    assert UPGRADE_DISPLAY_LIGHT == DisplayLight(matched_lux=1000.0, black=0.0003)
    for matched, black in ((5, 0.0003), (1000, 0), (1000, 0.5), (True, 0.001)):
        with pytest.raises(ValueError, match="^display_light$"):
            DisplayLight(matched_lux=matched, black=black)
