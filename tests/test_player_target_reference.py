"""The calibration targets v2 in the Player's final pass (player/geometry.py `final_pixel`, decision
0027): the opening's one size rule, and the two stages over the screen board (owner, 2026-10-13:
pre-software is the display alone, raw everywhere; post-software runs through Photo Wall's steps,
with no raw region left: v1's dark box is gone). Since decision 0028 the post-software target, as the
look patterns, goes through the Display's white balance, contrast and gamma only, never the room's
light, the paper or the mat. Where the board's squares sit is B1's
(tests/test_player_board_reference.py). Pure: the pattern pass's shader (player/pattern_pass.py) is held to
this by tests/native_fit_pass_harness.py."""
from __future__ import annotations

import math

import pytest

import player.geometry as geometry
from contracts.calibration_target import SCREEN_ROWS, Square, Surface, TargetCode, square
from contracts.colour import srgb_to_linear
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
    BoardDraw,
    board_draw,
    board_point,
    board_signal,
    encode_srgb,
    final_pixel,
    frame_lights,
    opening_size_mm,
)

PRE, POST = TestPattern.TARGET_PRE_SOFTWARE, TestPattern.TARGET_POST_SOFTWARE
PROFILE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=27)
DISPLAY = DisplayLight(1000.0, 0.0003)
LIT = frame_lights(
    FrameLighting(mat_depth_mm=8, paper=Paper(whiteness=.6, ink_density=1.2), lights=(
        WashLight(light="room", lux=40, reference=AT_FULL),
        FarLight(light="room", lux=200, reference=AT_FULL, from_degrees=0, steep_degrees=30,
                 softness_degrees=20))),
    RoomLightsDocument(change=1, lights={"room": FixedEntry(colour=FixedColour(kelvin=3000))}),
    CoverFit(), PROFILE, DISPLAY)
GLOWING_TABLE = bytes([200]) * LUT_BYTES
DRAW = board_draw(CoverFit(), PROFILE, target_modules(TargetCode("p-" + "ab" * 16, "HDMI-A-1", 9)))
MULTIPLIERS, SLOPE, EXPONENT = (1.0, .9, .8), .8, 1.1


def operations(pattern, slope=SLOPE) -> PictureOperations:
    return PictureOperations("HDMI-A-1", 9, MULTIPLIERS, slope, EXPONENT, DISPLAY,
                             test_pattern=pattern)


def displayed(signal: float) -> tuple[float, float, float, float]:
    """The Display's steps alone on an encoded signal: white balance, encode, contrast, gamma, clamp."""
    return (*(min(1.0, max(0.0, (encode_srgb(srgb_to_linear(signal) * m) - .5) * SLOPE + .5) ** EXPONENT)
              for m in MULTIPLIERS), 1.0)


def target(pattern, point, *, inside=True, x=.5, y=.02):
    return final_pixel((.3, .6, .9), operations(pattern), inside=inside, lights=LIT, x=x, y=y, lut=GLOWING_TABLE,
                       opening_uv=point, board=DRAW)


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

def test_the_openings_size_turns_with_the_nearest_quarter():
    diagonal = 27 * 25.4
    width, height = diagonal * 16 / math.hypot(16, 9), diagonal * 9 / math.hypot(16, 9)
    for rotation in (0, 30, 180):
        assert opening_size_mm(CoverFit(rotation=rotation), PROFILE) == pytest.approx((width, height))
    for rotation in (90, 270, 300):
        assert opening_size_mm(CoverFit(rotation=rotation), PROFILE) == pytest.approx((height, width))


def test_the_lights_take_the_openings_size_from_the_one_rule(monkeypatch):
    monkeypatch.setattr(geometry, "opening_size_mm", lambda fit, profile: (123.0, 45.0))
    assert frame_lights(FrameLighting(mat_depth_mm=0, lights=(), paper=Paper(whiteness=.9, ink_density=2)), None,
                        CoverFit(), PROFILE, DISPLAY).size_mm == (123.0, 45.0)


# -- 2. The stages -----------------------------------------------------------------------------

def test_pre_software_is_the_signal_whatever_the_settings():
    seen = set()
    for point in grid():
        signal = signal_at(*point)
        assert target(PRE, point) == (signal, signal, signal, 1.0), point
        seen.add(signal)
    assert {0.0, 1.0} < seen and len(seen) >= 5, seen       # black, white and greys: not vacuous


def test_post_software_is_the_signal_through_the_displays_steps_only():
    """Never the room's light, the paper, the mat or the paper profile's LUT."""
    for point in grid(37):
        assert target(POST, point) == pytest.approx(displayed(signal_at(*point)), abs=1e-12), point
    photo = final_pixel((1.0,) * 3, operations(None), inside=True, lights=LIT, x=.5, y=.02)
    assert photo != pytest.approx(displayed(1.0), abs=1e-3)    # the room really changes a photo: not vacuous


def test_post_software_black_is_the_contrasts_black():
    """No square is raw after software (v1's dark box is gone): a BLACK square reads the contrast's
    lift of black."""
    black = displayed(0.0)
    assert black[0] > 0
    points = black_squares()
    assert len(points) >= SCREEN_ROWS * DRAW.columns // 3, len(points)
    for point in points:
        assert signal_at(*point) == 0.0, point
        assert target(POST, point) == pytest.approx(black, abs=1e-12), point
    assert target(PRE, points[0]) == (0.0, 0.0, 0.0, 1.0)              # before software it stays raw 0


@pytest.mark.parametrize("pattern", [PRE, POST])
def test_a_target_needs_its_board_and_the_openings_uv(pattern):
    with pytest.raises(ValueError, match="board"):
        final_pixel((0.0,) * 3, operations(pattern), inside=True, lights=LIT, x=.5, y=.5, opening_uv=(.5, .5))
    with pytest.raises(ValueError, match="opening_uv"):
        final_pixel((0.0,) * 3, operations(pattern), inside=True, lights=LIT, x=.5, y=.5, board=DRAW)


@pytest.mark.parametrize("pattern", [PRE, POST])
def test_outside_the_opening_is_black(pattern):
    white = next(point for point in grid() if signal_at(*point) == 1.0)
    assert target(pattern, white, inside=False) == (0.0, 0.0, 0.0, 1.0)


def test_other_patterns_ignore_the_board():
    white = operations(TestPattern.WHITE)
    assert final_pixel((0.0,) * 3, white, inside=True, lights=LIT, x=.5, y=.5, opening_uv=(.5, .5),
                       board=DRAW) == final_pixel((0.0,) * 3, white, inside=True, lights=LIT, x=.5, y=.5,
                                                  opening_uv=(.5, .5))
