"""Display tuning's test patterns in the Player's scalar reference (decision 0024 as decision 0028
amends it): a look pattern (White, Grey ramp) goes through the Display's white balance, contrast and
gamma only, never the room's light, the paper or the mat, so White is the TV's own white for the
paper match; the Black level is the raw signal; outside every opening stays black for all of them.
Pure: no GL (the pattern pass's shader, player/pattern_pass.py, is held to this reference by
tests/native_fit_pass_harness.py, on Linux CI)."""
from __future__ import annotations

import math

import pytest

from contracts.calibration_target import TargetCode
from contracts.frame_lighting import AT_FULL, FarLight, FrameLighting, Paper, WashLight
from contracts.lut import LUT_BYTES
from contracts.models import CoverFit, FrameProfile
from contracts.node_frame import PictureOperations
from contracts.node_picture import DisplayLight
from contracts.room_light import (
    FixedColour,
    FixedEntry,
    RoomLightsDocument,
)
from contracts.tuning import TestPattern
from player.calibration_target import target_modules
from player.geometry import (
    BLACK_LEVEL_STEPS,
    GREY_RAMP_STEPS,
    SIGNAL_LEVELS,
    board_draw,
    encode_srgb,
    final_pixel,
    frame_lights,
    homography,
    inside_opening,
    inverse,
    pattern_signal,
    transform,
)
from player.pattern_pass import PATTERN_CODES, _fragment

PROFILE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=27)
SMALL_PROFILE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)
GLOWING_TABLE = bytes([200]) * LUT_BYTES        # every colour to a bright grey: a LUT nobody misses
ROOM = RoomLightsDocument(change=1, lights={"room": FixedEntry(colour=FixedColour(kelvin=3000))})
# A dim warm room with a deep mat and a dark paper: what a photo gets, and a pattern never does.
DIM_ROOM = FrameLighting(mat_depth_mm=8, paper=Paper(whiteness=.6, ink_density=1.2), lights=(
    WashLight(light="room", lux=40, reference=AT_FULL),
    FarLight(light="room", lux=200, reference=AT_FULL, from_degrees=0, steep_degrees=30,
             softness_degrees=20)))
DISPLAY = DisplayLight(1000.0, 0.0003)


def lights(fit=CoverFit()):
    return frame_lights(DIM_ROOM, ROOM, fit, PROFILE, DISPLAY)


def operations(slope=1.0, *, rgb=(1.0, 1.0, 1.0), pattern=None) -> PictureOperations:
    return PictureOperations("HDMI-A-1", 1, rgb, slope, 1.0, DISPLAY, test_pattern=pattern)


def tilted_opening(degrees: float) -> tuple[tuple[float, float], ...]:
    """A 0.5 x 0.3 rectangle about the output's centre turned `degrees` clockwise (y down)."""
    turn = math.radians(degrees)
    cos, sin = math.cos(turn), math.sin(turn)
    return tuple((.5 + dx * cos - dy * sin, .5 + dx * sin + dy * cos)
                 for dx, dy in ((-.25, -.15), (.25, -.15), (.25, .15), (-.25, .15)))


def test_white_is_the_tvs_own_white_through_the_displays_steps_only():
    """The room, the paper, the mat and the paper profile's LUT never reach a look pattern; white
    balance does (the paper match compares the TV's white as the Display draws it)."""
    neutral = operations(pattern=TestPattern.WHITE)
    lit = lights()
    assert final_pixel((0.0, 0.0, 0.0), neutral, inside=True, lights=lit, x=.5, y=.01, lut=GLOWING_TABLE,
                       opening_uv=(.3, .7)) == pytest.approx((1.0, 1.0, 1.0, 1.0), abs=1e-12)
    assert final_pixel((0.0, 0.0, 0.0), neutral, inside=False, lights=lit, x=.5, y=.5,
                       opening_uv=(.3, .7)) == (0.0, 0.0, 0.0, 1.0)
    cool = operations(rgb=(.9, .95, 1.0), pattern=TestPattern.WHITE)
    assert final_pixel((0.0, 0.0, 0.0), cool, inside=True, lights=lit, x=.5, y=.5, opening_uv=(.5, .5))[:3] == (
        pytest.approx(tuple(encode_srgb(m) for m in (.9, .95, 1.0)), abs=1e-12))
    photo_white = final_pixel((1.0, 1.0, 1.0), operations(), inside=True, lights=lit, x=.5, y=.5)
    assert photo_white[0] < .75         # the dim room really dims a photo: the comparison is not vacuous


def test_grey_ramp_steps_are_signal_levels_and_its_bottom_half_is_smooth():
    ramp = operations(pattern=TestPattern.GREY_RAMP)
    lit = lights()
    for step in range(GREY_RAMP_STEPS):
        u = (step + .5) / GREY_RAMP_STEPS
        drawn = final_pixel((0.0, 0.0, 0.0), ramp, inside=True, lights=lit, x=.5, y=.5, opening_uv=(u, .25))
        assert drawn == pytest.approx((step / 10, step / 10, step / 10, 1.0), abs=1e-9), step
    for u in (0.0, .13, .5, .77, 1.0):
        drawn = final_pixel((0.0, 0.0, 0.0), ramp, inside=True, lights=lit, x=.5, y=.5, opening_uv=(u, .75))
        assert drawn == pytest.approx((u, u, u, 1.0), abs=1e-9), u
    assert pattern_signal(TestPattern.GREY_RAMP, 1.4, .25) == (1.0, 1.0, 1.0)     # u clamped


def test_black_level_is_raw_whatever_the_settings():
    lifted = operations(.5, rgb=(.8, .9, 1.0), pattern=TestPattern.BLACK_LEVEL)
    lit = lights()

    def drawn(u, v, inside=True):
        return final_pixel((.5, .5, .5), lifted, inside=inside, lights=lit, x=.5, y=.01, lut=GLOWING_TABLE,
                           opening_uv=(u, v), dither_at=(3, 1))
    for k in range(1, BLACK_LEVEL_STEPS + 1):
        u = (k - .5) / BLACK_LEVEL_STEPS
        assert drawn(u, .5) == (k / SIGNAL_LEVELS,) * 3 + (1.0,), k
        assert drawn(u, .5, inside=False) == (0.0, 0.0, 0.0, 1.0)
        assert drawn(u - .45 / BLACK_LEVEL_STEPS, .5) == (0.0, 0.0, 0.0, 1.0)    # the cell's margin
    for u, v in ((.5, .1), (.5, .9), (.02, .34), (.99, .66)):
        assert drawn(u, v) == (0.0, 0.0, 0.0, 1.0), (u, v)
    # The same settings do lift a photo's black: the raw path really skips them.
    assert final_pixel((0.0, 0.0, 0.0), operations(.5), inside=True, lights=lit, x=.5, y=.5)[0] > .1


@pytest.mark.parametrize("pattern", list(TestPattern))
def test_no_pattern_paints_outside_the_opening(pattern):
    corners = tilted_opening(30)
    fit = CoverFit(corners=corners, rotation=30)
    lit = lights(fit)
    # A calibration target's board, drawn across the whole opening: none of it past the opening.
    board = board_draw(fit, SMALL_PROFILE, target_modules(TargetCode("p-" + "0" * 32, "HDMI-A-1", 1)))
    to_opening = inverse(homography(corners))
    lifted = operations(.5, pattern=pattern)
    inside_lit = outside = 0
    for i in range(80):
        for j in range(80):
            x, y = (i + .5) / 80, (j + .5) / 80
            inside = inside_opening(corners, x, y)
            drawn = final_pixel((0.0, 0.0, 0.0), lifted, inside=inside, lights=lit, x=x, y=y,
                                opening_uv=transform(to_opening, x, y), board=board, dither_at=(i, j))
            if inside:
                inside_lit += drawn[:3] != (0.0, 0.0, 0.0)
            else:
                outside += 1
                assert drawn == (0.0, 0.0, 0.0, 1.0), (pattern, x, y)
    assert outside > 2000 and inside_lit > 40, (outside, inside_lit)    # Black level, pre-software target: their cells alone


def test_a_pattern_needs_the_openings_uv():
    with pytest.raises(ValueError, match="opening_uv"):
        final_pixel((0.0, 0.0, 0.0), operations(pattern=TestPattern.WHITE), inside=True, lights=lights(), x=.5, y=.5)


def test_the_shader_names_every_pattern():
    assert set(PATTERN_CODES) == {None, *TestPattern} and PATTERN_CODES[None] == 0
    assert len(set(PATTERN_CODES.values())) == len(PATTERN_CODES)
    for lut_on in (False, True):
        source = _fragment(lut_on)
        assert f"#define PATTERN_COMMISSIONING_GRID {PATTERN_CODES[None]}\n" in source
        for pattern in TestPattern:
            assert f"#define PATTERN_{pattern.name} {PATTERN_CODES[pattern]}\n" in source
