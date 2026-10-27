"""A room light's stated colour as the Pi draws it (2026-10-25, R6: bounding a colour temperature is
the node's): the document carries the colour as its source stated it (real kelvin, 1000..40000, or a
bulb's rgb); the Player alone bounds a kelvin to the table (contracts/kelvin.py, 2000..10000) and
rounds it to the table's step, a half up, and draws a bulb's rgb as is. Proven through what the
final pass draws with (player/geometry.py `picture_lights`) and what the Player reports
(`light_lux_on_frame`): a light's lux is never changed by its colour.

Mutation probe: `drawn_kelvin` without the bound (bounded = kelvin) -> the 1000 K and 40000 K
drawings raise ValueError("kelvin") from contracts/kelvin.py `kelvin_white`."""
from __future__ import annotations

import math

import pytest

from contracts.frame_lighting import AT_FULL, FrameLighting, Paper, WashLight
from contracts.kelvin import (
    KELVIN_STATED_MAX,
    KELVIN_STATED_MIN,
    KELVIN_TABLE_MAX,
    KELVIN_TABLE_MIN,
)
from contracts.node_picture import DisplayLight
from contracts.room_light import (
    Dimmer,
    FixedEntry,
    KelvinColour,
    Measured,
    MeasuredEntry,
    ReportedEntry,
    RgbColour,
    RoomLightEntry,
    RoomLightsDocument,
)
from player.geometry import LUMA, drawn_kelvin, light_colour, light_lux_on_frame, picture_lights

DISPLAY = DisplayLight(1000.0, 0.0003)
SIZE = (950.0, 540.0)
LUX = 200.0
LIGHTING = FrameLighting(mat_depth_mm=0, paper=Paper(whiteness=0.9, ink_density=2),
                         lights=(WashLight(light="room", lux=LUX, reference=AT_FULL),))


def drawn(entry: RoomLightEntry) -> tuple[float, float, float]:
    """The rgb lux the final pass draws the Frame's one wash with (none: (0, 0, 0))."""
    lit = picture_lights(LIGHTING, RoomLightsDocument(change=1, lights={"room": entry}), SIZE, DISPLAY)
    return lit.lights[0].rgb if lit.lights else (0.0, 0.0, 0.0)


def reported_lux(entry: RoomLightEntry) -> float:
    """What the Player reports as the Frame's light, lux."""
    lux = light_lux_on_frame(LIGHTING, RoomLightsDocument(change=1, lights={"room": entry}))
    assert lux is not None
    return lux


def fixed(kelvin: float) -> FixedEntry:
    return FixedEntry(colour=KelvinColour(kelvin=kelvin))


def luminance(rgb) -> float:
    return sum(weight * channel for weight, channel in zip(LUMA, rgb, strict=True))


@pytest.mark.parametrize(("stated", "row"), [
    (3450.0, 3500), (3449.9, 3400), (3456.7, 3500), (3350.0, 3400), (3550.0, 3600), (2050.0, 2100),
    (9950.0, 10000), (6500.0, 6500), (2000.0, 2000), (10000.0, 10000),
    (1000.0, 2000), (1999.0, 2000), (1999.9, 2000), (10001.0, 10000), (10049.0, 10000), (40000.0, 10000),
])
def test_a_stated_kelvin_is_bounded_to_the_table_and_rounded_a_half_up(stated, row):
    """3,450 K draws at 3,500 K (a half up, not Python's round to even), 3,449.9 K at 3,400 K;
    below 2,000 K at 2,000 K, above 10,000 K at 10,000 K."""
    assert drawn_kelvin(stated) == row
    assert type(drawn_kelvin(stated)) is int


@pytest.mark.parametrize(("stated", "row"), [
    (1000.0, 2000), (1999.0, 2000), (40000.0, 10000), (10001.0, 10000), (3456.7, 3500), (3450.0, 3500),
    (2700.0, 2700),
])
def test_the_frame_draws_a_stated_kelvin_at_its_rows_colour(stated, row):
    """Through the final pass's lights: the drawn colour is the drawn row's light_colour, at the
    light's lux."""
    assert drawn(fixed(stated)) == pytest.approx(tuple(LUX * channel for channel in light_colour(row)))


def test_every_stated_kelvin_draws_without_error_and_keeps_its_lux():
    """Across the whole stated range (every 50 K and both ends) nothing raises, the colour is a row's,
    and the light's lux is the same whatever its colour."""
    kelvins = [float(k) for k in range(KELVIN_STATED_MIN, KELVIN_STATED_MAX + 1, 50)] + [KELVIN_STATED_MAX - 0.1]
    for kelvin in kelvins:
        rgb = drawn(fixed(kelvin))
        assert luminance(rgb) == pytest.approx(LUX)
        assert reported_lux(fixed(kelvin)) == pytest.approx(LUX)
        assert KELVIN_TABLE_MIN <= drawn_kelvin(kelvin) <= KELVIN_TABLE_MAX


def test_a_warmer_stated_kelvin_draws_warmer_and_out_of_range_draws_the_end_rows():
    cold, warm = drawn(fixed(KELVIN_STATED_MAX)), drawn(fixed(KELVIN_STATED_MIN))
    assert cold == drawn(fixed(KELVIN_TABLE_MAX)) and warm == drawn(fixed(KELVIN_TABLE_MIN))
    assert warm[0] / warm[2] > cold[0] / cold[2]


@pytest.mark.parametrize("entry", [
    ReportedEntry(colour=KelvinColour(kelvin=1200.0), state=Dimmer(on=True, brightness=0.5), doubt=None),
    MeasuredEntry(colour=KelvinColour(kelvin=38000.0), state=Measured(lux=100.0), doubt=None),
])
def test_every_entry_kind_draws_an_out_of_range_kelvin(entry):
    """A reported and a measured light draw a stated kelvin past the table too, at the end row."""
    lighting = FrameLighting(mat_depth_mm=0, paper=Paper(whiteness=0.9, ink_density=2), lights=(
        WashLight(light="room", lux=LUX, reference=AT_FULL if isinstance(entry, ReportedEntry)
                  else {"kind": "at-reading", "lux": 200.0}),))
    lit = picture_lights(lighting, RoomLightsDocument(change=1, lights={"room": entry}), SIZE, DISPLAY)
    (light,) = lit.lights
    row = drawn_kelvin(entry.colour.kelvin)
    assert luminance(light.rgb) == pytest.approx(LUX * 0.5)
    assert light.rgb == pytest.approx(tuple(LUX * 0.5 * channel for channel in light_colour(row)))


def test_a_colour_bulb_draws_its_rgb_warm_at_lux_times_brightness():
    """A bulb in a colour mode (Home Assistant's hs, rgb (1, 0.5, 0)): drawn warm, red over green
    and no blue, its luminance lux x brightness; the same bulb off adds nothing."""
    on = ReportedEntry(colour=RgbColour(rgb=(1.0, 0.5, 0.0)), state=Dimmer(on=True, brightness=0.4), doubt=None)
    rgb = drawn(on)
    assert rgb[0] > rgb[1] > rgb[2] == 0.0
    assert luminance(rgb) == pytest.approx(LUX * 0.4)
    assert reported_lux(on) == pytest.approx(LUX * 0.4)
    assert rgb[1] / rgb[0] == pytest.approx(((0.5 + 0.055) / 1.055) ** 2.4)    # the share taken to linear light
    off = ReportedEntry(colour=RgbColour(rgb=(1.0, 0.5, 0.0)), state=Dimmer(on=False, brightness=0.4), doubt=None)
    assert drawn(off) == (0.0, 0.0, 0.0)
    assert reported_lux(off) == 0.0


def test_a_white_bulb_and_a_grey_rgb_draw_the_same_luminance():
    """Colour never changes how bright a light is: a grey rgb share and a kelvin at the same
    brightness both put lux x brightness on the Frame."""
    grey = drawn(ReportedEntry(colour=RgbColour(rgb=(0.2, 0.2, 0.2)), state=Dimmer(on=True, brightness=1.0), doubt=None))
    assert grey == pytest.approx((LUX, LUX, LUX))
    assert not any(math.isnan(channel) for channel in grey)
