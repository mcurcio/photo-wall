"""Display tuning's test patterns in the Player's scalar reference (decision 0024, design rule 2):
a look pattern (White, Grey ramp) passes through every Photo Wall step and the mat, so it shows
what photos get; the Black level is the raw signal; outside every opening stays black for all
three. Pure: no GL (the shader is held to this reference by tests/native_renderer_pixel_harness.py,
on Linux CI)."""
from __future__ import annotations

import math

import pytest

from contracts.lut import LUT_BYTES, LutRef
from contracts.models import CoverFit, FrameProfile, Light, Mat
from contracts.node_frame import LutMix, PictureOperations
from contracts.tuning import TestPattern
from player.geometry import (
    BLACK_LEVEL_STEPS,
    GREY_RAMP_STEPS,
    SIGNAL_LEVELS,
    final_pixel,
    homography,
    inside_opening,
    inverse,
    mat_shade,
    mat_shadow,
    pattern_signal,
    transform,
)
from player.native import _FINAL, _PATTERN_CODES

PROFILE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=27)
LIT_MAT = Mat(depth_mm=8, lights=(Light(from_degrees=0, steep_degrees=30, softness_degrees=20,
                                        strength_tenths=15, colour_kelvin=3000),))
LUT = LutMix(LutRef("ab" * 32, "lut33-test"), 1.0)
GLOWING_TABLE = bytes([200]) * LUT_BYTES        # every colour to a bright grey: a LUT nobody misses


def operations(light=1.0, slope=1.0, *, floor=0.0, lut=None, pattern=None) -> PictureOperations:
    return PictureOperations("HDMI-A-1", 1, (1.0, 1.0, 1.0), light, slope, 1.0,
                             black_floor=floor, lut=lut, test_pattern=pattern)


def tilted_opening(degrees: float) -> tuple[tuple[float, float], ...]:
    """A 0.5 x 0.3 rectangle about the output's centre turned `degrees` clockwise (y down)."""
    turn = math.radians(degrees)
    cos, sin = math.cos(turn), math.sin(turn)
    return tuple((.5 + dx * cos - dy * sin, .5 + dx * sin + dy * cos)
                 for dx, dy in ((-.25, -.15), (.25, -.15), (.25, .15), (-.25, .15)))


def test_white_through_brightness_is_a_white_photo_pixel():
    neutral = operations(pattern=TestPattern.WHITE)
    assert final_pixel((0.0, 0.0, 0.0), neutral, inside=True, opening_uv=(.3, .7)) == pytest.approx(
        (1.0, 1.0, 1.0, 1.0), abs=1e-12)
    assert final_pixel((0.0, 0.0, 0.0), neutral, inside=False, opening_uv=(.3, .7)) == (0.0, 0.0, 0.0, 1.0)
    shade = (.8, .75, .7)
    half = operations(.5, floor=.05)
    pattern = operations(.5, floor=.05, pattern=TestPattern.WHITE)
    photo_white = final_pixel((1.0, 1.0, 1.0), half, inside=True, shade=shade)
    assert final_pixel((.2, .4, .6), pattern, inside=True, shade=shade, opening_uv=(.5, .5)) == photo_white
    assert photo_white[0] < .9          # brightness 50 % really dims it: the comparison is not vacuous


def test_grey_ramp_steps_are_signal_levels_and_its_bottom_half_is_smooth():
    ramp = operations(pattern=TestPattern.GREY_RAMP)
    for step in range(GREY_RAMP_STEPS):
        u = (step + .5) / GREY_RAMP_STEPS
        drawn = final_pixel((0.0, 0.0, 0.0), ramp, inside=True, opening_uv=(u, .25))
        assert drawn == pytest.approx((step / 10, step / 10, step / 10, 1.0), abs=1e-9), step
    for u in (0.0, .13, .5, .77, 1.0):
        drawn = final_pixel((0.0, 0.0, 0.0), ramp, inside=True, opening_uv=(u, .75))
        assert drawn == pytest.approx((u, u, u, 1.0), abs=1e-9), u
    assert pattern_signal(TestPattern.GREY_RAMP, 1.4, .25) == (1.0, 1.0, 1.0)     # u clamped


def test_black_level_is_raw_whatever_the_settings():
    lifted = operations(1.5, floor=.1, lut=LUT, pattern=TestPattern.BLACK_LEVEL)
    shade = mat_shade(mat_shadow(CoverFit(), PROFILE, LIT_MAT), .5, .01)
    def drawn(u, v, inside=True):
        return final_pixel((.5, .5, .5), lifted, inside=inside, shade=shade, lut=GLOWING_TABLE,
                           opening_uv=(u, v))
    for k in range(1, BLACK_LEVEL_STEPS + 1):
        u = (k - .5) / BLACK_LEVEL_STEPS
        assert drawn(u, .5) == (k / SIGNAL_LEVELS,) * 3 + (1.0,), k
        assert drawn(u, .5, inside=False) == (0.0, 0.0, 0.0, 1.0)
        assert drawn(u - .45 / BLACK_LEVEL_STEPS, .5) == (0.0, 0.0, 0.0, 1.0)    # the cell's margin
    for u, v in ((.5, .1), (.5, .9), (.02, .34), (.99, .66)):
        assert drawn(u, v) == (0.0, 0.0, 0.0, 1.0), (u, v)
    # The same settings do lift a photo's black: the raw path really skips them.
    assert final_pixel((0.0, 0.0, 0.0), operations(1.5, floor=.1), inside=True)[0] > .1


@pytest.mark.parametrize("pattern", list(TestPattern))
def test_no_pattern_paints_outside_the_opening(pattern):
    corners = tilted_opening(30)
    fit = CoverFit(corners=corners, rotation=30)
    shadow = mat_shadow(fit, PROFILE, LIT_MAT)
    to_opening = inverse(homography(corners))
    lifted = operations(1.5, .5, floor=.1, pattern=pattern)
    inside_lit = outside = 0
    for i in range(40):
        for j in range(40):
            x, y = (i + .5) / 40, (j + .5) / 40
            inside = inside_opening(corners, x, y)
            drawn = final_pixel((0.0, 0.0, 0.0), lifted, inside=inside, shade=mat_shade(shadow, x, y),
                                opening_uv=transform(to_opening, x, y))
            if inside:
                inside_lit += drawn[:3] != (0.0, 0.0, 0.0)
            else:
                outside += 1
                assert drawn == (0.0, 0.0, 0.0, 1.0), (pattern, x, y)
    assert outside > 500 and inside_lit > 20, (outside, inside_lit)     # Black level: its cells alone


def test_a_pattern_needs_the_openings_uv():
    with pytest.raises(ValueError, match="opening_uv"):
        final_pixel((0.0, 0.0, 0.0), operations(pattern=TestPattern.WHITE), inside=True)


def test_the_shader_names_every_pattern():
    assert set(_PATTERN_CODES) == {None, *TestPattern} and _PATTERN_CODES[None] == 0
    assert len(set(_PATTERN_CODES.values())) == len(_PATTERN_CODES)
    for pattern in TestPattern:
        assert f"#define PATTERN_{pattern.name} {_PATTERN_CODES[pattern]}\n" in _FINAL
