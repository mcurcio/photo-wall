"""The physics' geometry in the scalar reference (decision 0028; it replaced the print look's sliders and
mat shadow, whose rules live on in it): a far light's mat shadow (its band, its softness, the wall's up
on a tilted or turned Frame, an even band in a skewed opening), a close light's cone placed from the
wall's up and its point shadow, each Frame's own lights on an Output, the paper profile's LUT before
the paper, and the fixed highlight rolloff. Pure: no GL (the shader is held to this reference by
tests/native_renderer_pixel_harness.py)."""
from __future__ import annotations

import itertools
import math

import pytest

from contracts.frame_lighting import (
    DIMMER_AT_FULL,
    CloseLight,
    FarLight,
    FrameLighting,
    Paper,
    WashLight,
)
from contracts.lut import LUT_BYTES, LUT_GRID, lut_offset
from contracts.models import CornersFit, CoverFit, FrameProfile
from contracts.node_frame import PictureOperations
from contracts.node_picture import DisplayLight
from contracts.room_light import FULL_ON, FixedColour, RoomLightEntry, RoomLightsDocument
from player.geometry import (
    HIGHLIGHT_KNEE,
    LUMA,
    encode_srgb,
    final_pixel,
    frame_lights,
    homography,
    irradiance,
    light_colour,
    lut_applied,
    lut_sampled,
    output_pixel,
    rolled_off,
    transform,
)

SCREEN = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=27)     # 27" 16:9
WIDTH_MM = 27 * 25.4 * (16 / 9) / math.hypot(16 / 9, 1)
HEIGHT_MM = 27 * 25.4 / math.hypot(16 / 9, 1)
DISPLAY = DisplayLight(1000.0, 0.0003)
ROOM = RoomLightsDocument(change=1, lights={"room": RoomLightEntry(FixedColour(6500), FULL_ON)})
PAPER = Paper(whiteness=.9, ink_density=-math.log10(DISPLAY.black))
WASH = 100.0                       # the room's even light, lux
FAR = 300.0                        # the far light's lux


def far(from_degrees=0, steep=45, softness=0) -> FarLight:
    return FarLight(light="room", lux=FAR, reference=DIMMER_AT_FULL, from_degrees=from_degrees,
                    steep_degrees=steep, softness_degrees=softness)


def lit(*lights, depth=2.0, fit=CoverFit(), paper=PAPER):
    lighting = FrameLighting(mat_depth_mm=depth, paper=paper,
                             lights=(WashLight(light="room", lux=WASH, reference=DIMMER_AT_FULL), *lights))
    return frame_lights(lighting, ROOM, fit, SCREEN, DISPLAY)


def reached(lights, x, y) -> float:
    """The far light's share at (x, y): (E - wash) / its lux (white light, so any channel)."""
    return (irradiance(lights, x, y)[0] - WASH) / FAR


def operations() -> PictureOperations:
    return PictureOperations("HDMI-A-1", 1, (1.0, 1.0, 1.0), 1.0, 1.0, DISPLAY.matched_lux, DISPLAY.black)


# -- A far light's mat shadow ------------------------------------------------------------------------

def test_no_depth_casts_no_shadow():
    flat = lit(far(), depth=0)
    assert reached(flat, .5, 1e-6) == pytest.approx(1.0)


def test_one_light_from_the_top_shades_a_band_its_depth_times_cot_steep_wide():
    """Depth 2 mm, steep 45, no softness, Cover at 0 on a 27" 16:9 screen: 1 mm below the top edge
    the far light is gone, 3 mm it is there, the bottom and side edges are lit. At steep 60 the
    band is 2 cot 60 = 1.15 mm. Mutation probe: use the depth as the width (no cot) -> the 1.5 mm
    point at steep 60 is shaded."""
    lights = lit(far())
    assert lights.size_mm == pytest.approx((WIDTH_MM, HEIGHT_MM))
    assert reached(lights, .5, 1 / HEIGHT_MM) == 0.0
    assert reached(lights, .5, 3 / HEIGHT_MM) == pytest.approx(1.0)
    assert reached(lights, .5, 1 - 1 / HEIGHT_MM) == pytest.approx(1.0)
    assert reached(lights, 1 / WIDTH_MM, .5) == pytest.approx(1.0)
    steep = lit(far(steep=60))
    assert reached(steep, .5, 1 / HEIGHT_MM) == 0.0
    assert reached(steep, .5, 1.5 / HEIGHT_MM) == pytest.approx(1.0)
    overhead = lit(far(steep=90), depth=20)
    assert reached(overhead, .5, 1e-9) == pytest.approx(1.0)                # cot 90 is exactly 0


def test_a_soft_light_eases_its_edge_between_its_steepest_and_shallowest_rays():
    """Softness 20 at steep 45: fully dark nearer than 2 cot 55, fully lit from 2 cot 35, and a
    smoothstep between."""
    lights = lit(far(softness=20))
    near, full = (2 / math.tan(math.radians(a)) for a in (55, 35))
    assert lights.lights[1].band_mm == pytest.approx((near, full))
    assert reached(lights, .5, (near + full) / 2 / HEIGHT_MM) == pytest.approx(.5, abs=1e-9)
    assert reached(lights, .5, near * .99 / HEIGHT_MM) == 0.0
    assert reached(lights, .5, full * 1.01 / HEIGHT_MM) == pytest.approx(1.0)


@pytest.mark.parametrize("tilt", [7, 10, -10])
def test_a_light_from_above_shades_only_the_top_of_an_opening_level_on_the_wall(tilt):
    """A Cover Frame turned `tilt` degrees on the panel, its opening drawn level on the wall: a far
    light from straight above shades the opening's top edge and neither side. Mutation probe: turn
    the light by the whole rotation -> one side edge is shaded too."""
    turn = math.radians(tilt)
    half_w, half_h, cx, cy = .3, .3, .5, .5
    corners = tuple((cx + x * math.cos(turn) - y * math.sin(turn) * 9 / 16,
                     cy + (x * math.sin(turn) * 16 / 9 + y * math.cos(turn)))
                    for x, y in ((-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h)))
    fit = CoverFit(corners=corners, rotation=tilt % 360)
    lights = lit(far(steep=20), depth=8, fit=fit)
    to_output = homography(fit.corners)
    edge = .002
    for u in (.2, .5, .8):
        assert reached(lights, *transform(to_output, u, edge)) < 1                # the top edge: shaded
    for v in (.3, .5, .7):
        for u in (edge, 1 - edge):                                               # the sides: lit
            assert reached(lights, *transform(to_output, u, v)) == pytest.approx(1.0), (u, v)


def test_a_light_from_wall_up_shades_the_edge_that_is_up_on_a_turned_frame():
    """Cover at 90: the wall's up is the output's right edge. Mutation probe: ignore fit.rotation ->
    the top edge is shaded instead."""
    lights = lit(far(), fit=CoverFit(rotation=90))
    across, down = lights.size_mm
    assert (across, down) == pytest.approx((HEIGHT_MM, WIDTH_MM))
    assert reached(lights, 1 - 1 / across, .5) == 0.0
    assert reached(lights, .5, 1 / down) == pytest.approx(1.0)
    assert reached(lights, 1 / across, .5) == pytest.approx(1.0)


def test_a_skewed_corners_opening_gets_an_even_band():
    """The edge distance is measured in mat space (the opening's own rectangle at its physical size),
    so the band under a skewed top edge is as wide at both ends. Mutation probe: measure in output
    fractions -> the two ends differ by more than 5 %."""
    skewed = CornersFit(corners=((.1, .1), (.9, .25), (.8, .9), (.15, .8)))
    lights = lit(far(steep=30, softness=20), depth=3, fit=skewed)
    to_output = homography(skewed.corners)

    def band(u: float) -> float:
        """How far below the top edge (mm, mat space) the far light at column u crosses halfway."""
        low, high = 0.0, .5
        for _ in range(60):
            v = (low + high) / 2
            low, high = (v, high) if reached(lights, *transform(to_output, u, v)) < .5 else (low, v)
        return low * lights.size_mm[1]

    left, right = band(.05), band(.95)
    assert left == pytest.approx(right, rel=.01), (left, right)
    assert 3 / math.tan(math.radians(40)) < left < 3 / math.tan(math.radians(20))


@pytest.mark.parametrize("kelvin", [2000, 2700, 4000, 6500, 10000])
def test_a_lights_lux_is_its_brightness_and_its_kelvin_only_its_colour(kelvin):
    """Each light's colour has luminance 1, so its lux alone says how bright it is. Mutation probe:
    use kelvin_white (largest channel 1) -> a 2700 K light is a third as bright as a 6500 K one."""
    colour = light_colour(kelvin)
    assert sum(weight * channel for weight, channel in zip(LUMA, colour, strict=True)) == pytest.approx(1.0)


# -- A close light: its place from the wall's up, its cone, its point shadow -------------------------

def spot(**place) -> CloseLight:
    return CloseLight(**{"light": "room", "lux": 400, "reference": DIMMER_AT_FULL, "across_mm": 0, "up_mm": 350,
                         "out_mm": 250, "inner_beam_degrees": 20, "outer_beam_degrees": 40, **place})


def test_a_close_light_is_its_lux_at_the_centre_and_follows_the_wall_up_on_a_turned_frame():
    """`lux` is at the Frame's centre; the light above the Frame (up_mm) is toward the top edge at
    quarter 0 and toward the output's right edge at 90 (the wall's up there)."""
    level = lit(spot(), depth=0)
    assert irradiance(level, .5, .5)[0] - WASH == pytest.approx(400.0)
    assert irradiance(level, .5, .3)[0] > irradiance(level, .5, .7)[0]
    turned = lit(spot(), depth=0, fit=CoverFit(rotation=90))
    assert irradiance(turned, .7, .5)[0] > irradiance(turned, .3, .5)[0]


def test_a_close_light_casts_a_point_shadow_under_the_edge_toward_it():
    """A light straight above the Frame's top edge, close in: under the top edge the mat hides it
    for depth x h / out mm (h along the wall to the light's foot), the bottom edge is lit."""
    lights = lit(spot(up_mm=HEIGHT_MM / 2 + 100, out_mm=200, inner_beam_degrees=150, outer_beam_degrees=170), depth=6)
    at_top = irradiance(lights, .5, .5 / HEIGHT_MM)[0] - WASH          # half a mm below the edge
    deeper = irradiance(lights, .5, 10 / HEIGHT_MM)[0] - WASH          # 10 mm: past depth * h / out = 3 mm
    assert at_top == 0.0 and deeper > 0


# -- Each Frame its own lights; the paper profile's LUT; the rolloff ---------------------------------

def test_each_frame_on_an_output_carries_its_own_lights():
    a = CoverFit(corners=((.04, .1), (.46, .1), (.46, .9), (.04, .9)))
    b = CoverFit(corners=((.54, .1), (.96, .1), (.96, .9), (.54, .9)))
    shaded, plain = lit(far(steep=5), depth=20, fit=a), lit(fit=b)
    frames = ((a, 16 / 9, (.5, .5, .5), shaded), (b, 16 / 9, (.5, .5, .5), plain))
    assert output_pixel(.75, .12, frames, operations(), (1920, 1080)) == final_pixel(
        (.5, .5, .5), operations(), inside=True, lights=plain, x=.75, y=.12)
    assert output_pixel(.25, .12, frames, operations(), (1920, 1080)) == final_pixel(
        (.5, .5, .5), operations(), inside=True, lights=shaded, x=.25, y=.12)


def lut_table(colour) -> bytes:
    """A LUT body (contracts/lut.py layout) whose grid point (r, g, b), each 0..LUT_GRID-1, holds
    `colour(r, g, b)` as three bytes."""
    body = bytearray(LUT_BYTES)
    for r, g, b in itertools.product(range(LUT_GRID), repeat=3):
        offset = lut_offset(r, g, b)
        body[offset:offset + 3] = bytes(colour(r, g, b))
    return bytes(body)


IDENTITY = lut_table(lambda *point: (round(index * 255 / (LUT_GRID - 1)) for index in point))
INVERTING = lut_table(lambda *point: (255 - round(index * 255 / (LUT_GRID - 1)) for index in point))


def test_an_identity_lut_samples_back_its_input_and_an_inverting_one_inverts():
    for encoded in itertools.product((0.0, .03, .2, .5, .51, .77, .999, 1.0), repeat=3):
        assert lut_sampled(IDENTITY, encoded) == pytest.approx(encoded, abs=1 / 255)
    for linear in ((0.0, 0.0, 0.0), (.2, .5, .9), (1.0, 1.0, 1.0)):
        inverted = [encode_srgb(value) for value in lut_applied(linear, INVERTING)]
        assert inverted == pytest.approx([1 - encode_srgb(value) for value in linear], abs=1 / 255)
    halfway = (.5 / (LUT_GRID - 1), 0.0, 0.0)
    assert lut_sampled(INVERTING, halfway)[0] == pytest.approx((255 + INVERTING[3]) / 2 / 255)


def test_the_paper_profile_comes_before_the_paper_and_a_lut_not_held_draws_without_it():
    """The LUT acts on the photo as authored, then the paper takes it: black photo through the
    inverting table is the paper's white. A LUT not held (None) draws exactly the photo's paper."""
    upgrade = lit(depth=0)
    upgrade_wash_only = frame_lights(FrameLighting(mat_depth_mm=0, paper=PAPER, lights=(
        WashLight(light="room", lux=DISPLAY.matched_lux, reference=DIMMER_AT_FULL),)), ROOM, CoverFit(), SCREEN, DISPLAY)
    white = final_pixel((0.0, 0.0, 0.0), operations(), inside=True, lights=upgrade_wash_only, x=.5, y=.5,
                        lut=INVERTING)
    assert white[:3] == pytest.approx((1.0, 1.0, 1.0), abs=2 / 255)
    assert final_pixel((.2, .5, .9), operations(), inside=True, lights=upgrade, x=.5, y=.5) != final_pixel(
        (.2, .5, .9), operations(), inside=True, lights=upgrade, x=.5, y=.5, lut=INVERTING)


@pytest.mark.parametrize("top", [1.0 + 1e-6, 1.5, 3.0])
def test_the_rolloff_brings_the_peak_to_exactly_white_and_leaves_the_knee_smooth(top):
    assert rolled_off(top, HIGHLIGHT_KNEE, top) == 1.0
    h = 1e-7
    assert (rolled_off(HIGHLIGHT_KNEE + h, HIGHLIGHT_KNEE, top) - HIGHLIGHT_KNEE) / h == pytest.approx(1.0, abs=1e-5)
    assert rolled_off(HIGHLIGHT_KNEE * .999, HIGHLIGHT_KNEE, top) == HIGHLIGHT_KNEE * .999
    previous = 0.0
    for step in range(1, 101):
        out = rolled_off(HIGHLIGHT_KNEE + (top - HIGHLIGHT_KNEE) * step / 100, HIGHLIGHT_KNEE, top)
        assert out >= previous
        previous = out
