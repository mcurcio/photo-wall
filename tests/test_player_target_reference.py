"""The calibration targets v2 in the Player's final pass (player/geometry.py `final_pixel`, decision
0027): the opening's one size rule, and the two stages over the screen board (owner, 2026-10-13:
pre-software is the display alone, raw everywhere; post-software runs through the Photo Wall steps,
with no raw region left: v1's dark box is gone). Where the board's squares sit is B1's
(tests/test_player_board_reference.py). Pure: the shader is held to this by
tests/native_renderer_pixel_harness.py."""
from __future__ import annotations

import dataclasses
import math

import pytest

import player.geometry as geometry
from contracts.calibration_target import SCREEN_ROWS, Square, Surface, TargetCode, square
from contracts.lut import LUT_BYTES, LutRef
from contracts.models import CoverFit, FrameProfile, Light, Mat
from contracts.node_frame import LutMix, PictureOperations, srgb_to_linear
from contracts.tuning import TestPattern
from player.calibration_target import target_modules
from player.geometry import (
    BoardDraw,
    board_draw,
    board_point,
    board_signal,
    final_pixel,
    mat_shadow,
    opening_size_mm,
)

PRE, POST = TestPattern.TARGET_PRE_SOFTWARE, TestPattern.TARGET_POST_SOFTWARE
PROFILE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=27)
LIT_MAT = Mat(depth_mm=8, lights=(Light(from_degrees=0, steep_degrees=30, softness_degrees=20,
                                        strength_tenths=15, colour_kelvin=3000),))
LUT = LutMix(LutRef("ab" * 32, "lut33-test"), 1.0)
GLOWING_TABLE = bytes([200]) * LUT_BYTES
SHADE = (.6, .55, .5)
DRAW = board_draw(CoverFit(), PROFILE, target_modules(TargetCode("p-" + "ab" * 16, "HDMI-A-1", 9)))


def operations(pattern, light=1.0, *, floor=0.0, lut=None, saturation=1.0) -> PictureOperations:
    return PictureOperations("HDMI-A-1", 9, (1.0, .9, .8), light, 1.2, 1.1, saturation=saturation,
                             black_floor=floor, lut=lut, test_pattern=pattern)


def uv_of(column: float, row: float, draw: BoardDraw = DRAW) -> tuple[float, float]:
    """The landscape opening's (u, v) where the board's point (column, row) is drawn: `board_point`
    undone."""
    along = draw.aspect * SCREEN_ROWS
    return ((column + (along - draw.columns) / 2) / along, row / SCREEN_ROWS)


def signal_at(u: float, v: float, draw: BoardDraw = DRAW) -> float:
    return board_signal(*board_point(u, v, draw), draw)


def grid(count: int = 61) -> list[tuple[float, float]]:
    """Points over the whole opening (the surround beside the board included)."""
    return [((i + .5) / count, (j + .5) / count) for i in range(count) for j in range(count)]


def black_squares() -> list[tuple[float, float]]:
    """The opening's (u, v) at the centre of every BLACK square shown."""
    return [uv_of(column + .5, row + .5) for row in range(SCREEN_ROWS) for column in range(DRAW.columns)
            if square(Surface.SCREEN, row, column) is Square.BLACK]


# -- 1. The opening's size has one home --------------------------------------------------------

@pytest.mark.parametrize("rotation", [0, 30, 90, 180, 270])
def test_the_openings_size_is_the_mat_shadows(rotation):
    fit = CoverFit(rotation=rotation)
    assert opening_size_mm(fit, PROFILE) == mat_shadow(fit, PROFILE, LIT_MAT).size_mm


def test_the_openings_size_turns_with_the_nearest_quarter():
    diagonal = 27 * 25.4
    width, height = diagonal * 16 / math.hypot(16, 9), diagonal * 9 / math.hypot(16, 9)
    for rotation in (0, 30, 180):
        assert opening_size_mm(CoverFit(rotation=rotation), PROFILE) == pytest.approx((width, height))
    for rotation in (90, 270, 300):
        assert opening_size_mm(CoverFit(rotation=rotation), PROFILE) == pytest.approx((height, width))


def test_the_mat_shadow_takes_its_size_from_the_one_rule(monkeypatch):
    monkeypatch.setattr(geometry, "opening_size_mm", lambda fit, profile: (123.0, 45.0))
    assert mat_shadow(CoverFit(), PROFILE, LIT_MAT).size_mm == (123.0, 45.0)


# -- 2. The stages -----------------------------------------------------------------------------

def test_pre_software_is_the_signal_whatever_the_settings():
    lifted = operations(PRE, 1.5, floor=.1, lut=LUT, saturation=.5)
    seen = set()
    for point in grid():
        signal = signal_at(*point)
        drawn = final_pixel((.3, .6, .9), lifted, inside=True, shade=SHADE, lut=GLOWING_TABLE,
                            opening_uv=point, board=DRAW)
        assert drawn == (signal, signal, signal, 1.0), point
        seen.add(signal)
    assert {0.0, 1.0} < seen and len(seen) >= 5, seen       # black, white and greys: not vacuous


def test_post_software_is_a_photo_of_the_signal_without_the_shade():
    lifted = operations(POST, 1.5, floor=.1, lut=LUT, saturation=.5)
    photo = dataclasses.replace(lifted, test_pattern=None)
    for point in grid(37):
        drawn = final_pixel((.3, .6, .9), lifted, inside=True, shade=SHADE, lut=GLOWING_TABLE,
                            opening_uv=point, board=DRAW)
        expected = final_pixel((srgb_to_linear(signal_at(*point)),) * 3, photo, inside=True, lut=GLOWING_TABLE)
        assert drawn == expected, point
    shaded = final_pixel((1.0,) * 3, photo, inside=True, shade=SHADE, lut=GLOWING_TABLE)
    assert shaded != final_pixel((1.0,) * 3, photo, inside=True, lut=GLOWING_TABLE)     # the shade is not vacuous


def test_post_software_black_is_lifted_by_the_floor():
    """No square is raw after software (v1's dark box is gone): a BLACK square reads the floor."""
    lifted = operations(POST, floor=.1)
    floor = final_pixel((0.0,) * 3, dataclasses.replace(lifted, test_pattern=None), inside=True)
    assert floor[0] > .1
    points = black_squares()
    assert len(points) >= SCREEN_ROWS * DRAW.columns // 3, len(points)
    for point in points:
        assert signal_at(*point) == 0.0, point
        assert final_pixel((.5,) * 3, lifted, inside=True, shade=SHADE, opening_uv=point, board=DRAW) == floor, point
    assert final_pixel((.5,) * 3, operations(PRE, floor=.1), inside=True, opening_uv=points[0],
                       board=DRAW) == (0.0, 0.0, 0.0, 1.0)              # before software it stays raw 0


@pytest.mark.parametrize("pattern", [PRE, POST])
def test_a_target_needs_its_board_and_the_openings_uv(pattern):
    with pytest.raises(ValueError, match="board"):
        final_pixel((0.0,) * 3, operations(pattern), inside=True, opening_uv=(.5, .5))
    with pytest.raises(ValueError, match="opening_uv"):
        final_pixel((0.0,) * 3, operations(pattern), inside=True, board=DRAW)


@pytest.mark.parametrize("pattern", [PRE, POST])
def test_outside_the_opening_is_black(pattern):
    white = next(point for point in grid() if signal_at(*point) == 1.0)
    assert final_pixel((1.0,) * 3, operations(pattern, floor=.1), inside=False,
                       opening_uv=white, board=DRAW) == (0.0, 0.0, 0.0, 1.0)


def test_other_patterns_ignore_the_board():
    white = operations(TestPattern.WHITE, floor=.1)
    assert final_pixel((0.0,) * 3, white, inside=True, opening_uv=(.5, .5), board=DRAW) == final_pixel(
        (0.0,) * 3, white, inside=True, opening_uv=(.5, .5))
