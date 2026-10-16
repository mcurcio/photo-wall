"""The room-light wire facts: the room-lights document, the light sensor reading, what the Player
drew with, a Frame's lighting and the Display's light, each strict and round-tripping."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from contracts.frame_lighting import (
    AT_FULL,
    FRAME_LIGHTS_MAX,
    AtReading,
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
    ROOM_LIGHTS_BYTES,
    ROOM_LIGHTS_MAX,
    ColourDimmer,
    ColourEntry,
    Dimmer,
    FixedColour,
    FixedEntry,
    LightSensorReading,
    Measured,
    MeasuredEntry,
    NotReported,
    ReportedColour,
    RoomLightsDocument,
    RoomLightsDrawn,
    Unavailable,
    WhiteEntry,
    decode_light_sensor,
    decode_room_lights,
    encode_light_sensor,
    encode_room_lights,
    room_lights_digest,
)

DOCUMENT = RoomLightsDocument(change=7, lights={
    "room": FixedEntry(colour=FixedColour(kelvin=6500)),
    "bookshelf-spot": ColourEntry(colour=ReportedColour(channel_shares=(0.3, 0.4, 0.3)),
                                  state=ColourDimmer(on=True, brightness=0.5, rgb=(1.0, 0.5, 0.25))),
    "daylight": MeasuredEntry(colour=FixedColour(kelvin=5500), state=Measured(lux=320.5)),
    "overheads": WhiteEntry(colour=FixedColour(kelvin=2700), state=Unavailable()),
    "hall": WhiteEntry(colour=FixedColour(kelvin=3000), state=Dimmer(on=False, brightness=0.0)),
    "lamp": WhiteEntry(colour=FixedColour(kelvin=2700), state=NotReported()),
})


def test_the_room_lights_document_round_trips_every_state_and_colour():
    raw = encode_room_lights(DOCUMENT)
    assert decode_room_lights(raw) == DOCUMENT
    assert json.loads(raw)["lights"]["bookshelf-spot"] == {
        "colour": {"channel_shares": [0.3, 0.4, 0.3], "kind": "reported"},
        "kind": "colour",
        "state": {"brightness": 0.5, "kind": "colour-dimmer", "on": True, "rgb": [1.0, 0.5, 0.25]}}
    assert room_lights_digest(decode_room_lights(raw)) == room_lights_digest(DOCUMENT)


def test_the_largest_document_fits_its_cap():
    """Mutation probe: ROOM_LIGHTS_BYTES 8192 -> the largest valid document is refused."""
    entry = ColourEntry(colour=ReportedColour(channel_shares=(1 / 3, 1 / 3, 1 / 3)),
                        state=ColourDimmer(on=True, brightness=1 / 3, rgb=(1 / 3, 1 / 3, 1 / 3)))
    largest = RoomLightsDocument(change=2**63 - 1, lights={f"{'x' * 29}{i:03d}": entry for i in range(ROOM_LIGHTS_MAX)})
    assert len(encode_room_lights(largest)) <= ROOM_LIGHTS_BYTES


@pytest.mark.parametrize("change", [
    lambda value: value["lights"]["hall"]["state"].update(kind="dimmed"),
    lambda value: value["lights"]["hall"]["state"].update(brightness=1.5),
    lambda value: value["lights"]["hall"]["state"].update(on=1),
    lambda value: value["lights"]["room"]["colour"].update(kelvin=6550),
    lambda value: value["lights"]["daylight"]["state"].update(lux=-1),
    lambda value: value["lights"]["daylight"]["state"].update(lux="320"),
    lambda value: value["lights"]["bookshelf-spot"]["state"].update(rgb=[1, 1]),
    lambda value: value["lights"].update({"Bad Id": value["lights"]["room"]}),
    lambda value: value.update(change=0),
    lambda value: value["lights"]["room"].update(extra=1),
    # A colour and a state that do not belong together cannot be built.
    lambda value: value["lights"]["hall"]["state"].update(rgb=[1, 1, 1]),
    lambda value: value["lights"]["overheads"].update(colour=value["lights"]["bookshelf-spot"]["colour"]),
    lambda value: value["lights"]["daylight"].update(state=value["lights"]["hall"]["state"]),
    lambda value: value["lights"]["room"].update(state=value["lights"]["hall"]["state"]),
], ids=["state-kind", "brightness", "bool-as-int", "kelvin-step", "lux-negative", "lux-string", "rgb-two",
        "bad-id", "change-0", "extra-field", "white-with-rgb", "white-with-reported-colour",
        "measured-with-dimmer", "fixed-with-state"])
def test_the_document_is_strict(change):
    value = json.loads(encode_room_lights(DOCUMENT))
    change(value)
    with pytest.raises(ValueError, match="^room_lights_document$"):
        decode_room_lights(json.dumps(value).encode())


def test_the_light_sensor_reading_says_lux_or_unavailable():
    for reading in (LightSensorReading(reading=Measured(lux=12.5)), LightSensorReading(reading=Unavailable())):
        assert decode_light_sensor(encode_light_sensor(reading)) == reading
    for raw in (b'{"reading":{"kind":"measured","lux":-1}}', b'{"lux":12}', b'{"reading":{"kind":"not-reported"}}'):
        with pytest.raises(ValueError, match="^light_sensor_reading$"):
            decode_light_sensor(raw)


def test_what_the_player_drew_with_names_a_digest_or_none():
    assert RoomLightsDrawn(drawn=room_lights_digest(DOCUMENT), following=True).drawn == room_lights_digest(DOCUMENT)
    assert RoomLightsDrawn(drawn=None, following=False).drawn is None
    with pytest.raises(ValidationError):
        RoomLightsDrawn(drawn="not-a-digest", following=True)


def _lighting(*lights) -> FrameLighting:
    return FrameLighting(mat_depth_mm=4.5, lights=lights, paper=Paper(whiteness=0.9, ink_density=2.3))


def test_a_frame_lighting_holds_the_three_kinds_and_its_paper():
    lighting = FrameLighting(
        mat_depth_mm=4.5,
        lights=(WashLight(light="room", lux=150, reference=AT_FULL),
                FarLight(light="daylight", lux=300, reference=AtReading(lux=1200), from_degrees=270, steep_degrees=30,
                         softness_degrees=20),
                CloseLight(light="bookshelf-spot", lux=400, reference=AT_FULL, across_mm=0, up_mm=350,
                           out_mm=250, inner_beam_degrees=20, outer_beam_degrees=40)),
        paper=Paper(whiteness=0.92, ink_density=2.3, profile=LutRef("ab" * 32, "lut33-perceptual-relcol-bpc-1")))
    assert FrameLighting.model_validate_json(lighting.model_dump_json()) == lighting


@pytest.mark.parametrize("bad", [
    lambda: CloseLight(light="s", lux=1, reference=AT_FULL, across_mm=0, up_mm=0, out_mm=1,
                       inner_beam_degrees=40, outer_beam_degrees=40),
    lambda: WashLight(light="s", lux=0, reference=AT_FULL),
    lambda: WashLight(light="s", lux=1, reference="dimmer-at-full"),
    lambda: WashLight(light="s", lux=1, reference=400),
    lambda: WashLight(light="s", lux=1, reference={"kind": "at-reading"}),
    lambda: Paper(whiteness=0.9, ink_density=0.1),
    lambda: _lighting(*(WashLight(light="s", lux=1, reference=AtReading(lux=1)) for _ in range(FRAME_LIGHTS_MAX + 1))),
])
def test_a_frame_lighting_refuses_what_has_no_physical_meaning(bad):
    with pytest.raises(ValidationError):
        bad()


def test_the_largest_frame_lighting_fits_the_trial():
    """Mutation probe: MAX_LIGHTING_BYTES 1024 -> four close lights with a paper profile do not fit."""
    close = CloseLight(light="a" * 32, lux=199999.25, reference=AtReading(lux=123456.75), across_mm=-4999.25,
                       up_mm=-4999.25,
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
