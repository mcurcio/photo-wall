"""The pattern pass's scalar reference (render redesign S3, run ledger .claude/runs/render-planes/
s3-page.md, bead B2): player/geometry.py `placement_uv`, `commissioning_grid` and `picture_board`,
and the chain player/pattern_pass.py's module docstring states (a target pixel through
`buffer_to_picture` over the whole screen, `placement_uv`, then `final_pixel`). Pure; the shader is
held to these in tests/native_fit_pass_harness.py."""
from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import pytest

from contracts.calibration_target import TargetCode
from contracts.frame_fit import Placement
from contracts.frame_lighting import AT_FULL, FarLight, FrameLighting, Paper, WashLight
from contracts.models import CoverFit, FrameProfile
from contracts.node_frame import PictureOperations
from contracts.node_picture import DisplayLight
from contracts.room_light import FixedColour, FixedEntry, RoomLightsDocument
from contracts.tuning import SIGNAL_LEVELS, TestPattern
from player.calibration_target import target_modules
from player.fit_pass import target_size
from player.geometry import (
    BLACK_LEVEL_PATCH,
    BLACK_LEVEL_STEPS,
    GRID_LINE,
    GRID_SLATE,
    board_draw,
    board_point,
    board_signal,
    buffer_to_picture,
    commissioning_grid,
    display_steps,
    dithered,
    final_pixel,
    picture_board,
    picture_lights,
    placement_uv,
)
from player.pattern_pass import PATTERN_CODES, PatternPass, PatternRequest

SCREEN = (64, 48)
PLACEMENT = Placement(16, 15, 32, 18)
LANDSCAPE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=27)
PORTRAIT = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=27)
MODULES = target_modules(TargetCode("p-" + "0123456789abcdef" * 2, "HDMI-A-1", 3))
LIGHTING = FrameLighting(mat_depth_mm=8, paper=Paper(whiteness=.9, ink_density=1.8), lights=(
    WashLight(light="room", lux=200, reference=AT_FULL),
    FarLight(light="lamp", lux=600, reference=AT_FULL, from_degrees=20, steep_degrees=30, softness_degrees=20)))
ROOM = RoomLightsDocument(change=1, lights={
    "room": FixedEntry(colour=FixedColour(kelvin=6500)), "lamp": FixedEntry(colour=FixedColour(kelvin=3000))})
DISPLAY = DisplayLight(250.0, .01)
LIGHTS = picture_lights(LIGHTING, ROOM, LANDSCAPE, DISPLAY)


def operations(pattern: TestPattern | None) -> PictureOperations:
    return PictureOperations("HDMI-A-1", 3, (.9, .95, 1.0), .8, 24 / 22, DISPLAY, test_pattern=pattern)


def ground(pattern: TestPattern | None, x: int, y: int, turn: int = 0, board=None):
    """The module docstring's chain at target pixel (x, y) of the whole screen's target."""
    size = target_size(Placement(0, 0, *SCREEN), turn)
    uv = placement_uv(*buffer_to_picture(x + .5, y + .5, size, turn), PLACEMENT, SCREEN)
    u, v = uv if uv is not None else (0.0, 0.0)
    photo = commissioning_grid(u, v) if pattern is None else (0.0, 0.0, 0.0)
    return final_pixel(photo, operations(pattern), inside=uv is not None, lights=LIGHTS, x=u, y=v,
                       opening_uv=(u, v), board=board, dither_at=(x, y))


# -- placement_uv ----------------------------------------------------------------------------------

def test_placement_uv_maps_the_placements_top_left_to_the_origin():
    assert placement_uv(16 / 64, 15 / 48, PLACEMENT, SCREEN) == (0.0, 0.0)
    assert placement_uv(32 / 64, 24 / 48, PLACEMENT, SCREEN) == pytest.approx((.5, .5))


@pytest.mark.parametrize("point", [(48 / 64, .5), (.5, 33 / 48), (48 / 64, 33 / 48),     # right and bottom edges
                                   (15.9 / 64, .5), (.5, 14.9 / 48), (0.0, 0.0), (1.0, 1.0)])
def test_placement_uv_is_none_on_the_right_and_bottom_edges_and_outside(point):
    assert placement_uv(*point, PLACEMENT, SCREEN) is None


# -- commissioning_grid ----------------------------------------------------------------------------

def test_the_commissioning_grid_has_lines_on_a_slate():
    assert commissioning_grid(.1, .55) == GRID_LINE == (.9, .9, .9)
    assert commissioning_grid(.55, .3) == GRID_LINE
    assert commissioning_grid(.05, .05) == GRID_SLATE == (.2, .3, .45)
    assert commissioning_grid(.0349 / 10, .5) == GRID_LINE and commissioning_grid(.036 / 10, .55) == GRID_SLATE


# -- picture_board ---------------------------------------------------------------------------------

@pytest.mark.parametrize("profile", [LANDSCAPE, PORTRAIT])
@pytest.mark.parametrize("modules", [None, MODULES])
def test_picture_board_is_an_unturned_frames_board(profile, modules):
    assert picture_board(profile, modules) == board_draw(CoverFit(), profile, modules)
    assert picture_board(profile, modules).portrait is (profile is PORTRAIT)


# -- the codes ---------------------------------------------------------------------------------------

def test_every_pattern_and_the_grid_has_its_own_code():
    assert set(PATTERN_CODES) == {None, *TestPattern} and PATTERN_CODES[None] == 0
    assert len(set(PATTERN_CODES.values())) == len(PATTERN_CODES)


# -- the chain, per pattern ----------------------------------------------------------------------------

@pytest.mark.parametrize("pattern", [None, *TestPattern])
@pytest.mark.parametrize("turn", [0, 90])
def test_outside_the_placement_every_ground_is_black(pattern, turn):
    board = picture_board(LANDSCAPE, MODULES)
    width, height = target_size(Placement(0, 0, *SCREEN), turn)
    inside = 0
    for y in range(height):
        for x in range(width):
            pixel = ground(pattern, x, y, turn, board)
            if placement_uv(*buffer_to_picture(x + .5, y + .5, (width, height), turn), PLACEMENT, SCREEN) is None:
                assert pixel == (0.0, 0.0, 0.0, 1.0), (pattern, turn, x, y)
            else:
                inside += 1
    assert inside == PLACEMENT.width * PLACEMENT.height


def test_white_is_the_displays_white_inside_the_placement():
    pixel = ground(TestPattern.WHITE, 20, 20)
    expected = tuple(min(1.0, max(0.0, dithered(value, 20, 20))) for value in display_steps((1.0,) * 3, operations(None)))
    assert pixel == pytest.approx((*expected, 1.0), abs=1e-12)


def test_black_levels_cells_sit_across_the_placements_middle_third():
    # Cell k's centre: u = (k - .5) / 20 of the 32-pixel placement, v = 0.5 (row 15 + 9).
    for k in (1, 5, 20):
        u = (k - .5) / BLACK_LEVEL_STEPS
        x = 16 + int(u * PLACEMENT.width)
        assert ground(TestPattern.BLACK_LEVEL, x, 24) == ((k_at(x),) * 3 + (1.0,))
    assert ground(TestPattern.BLACK_LEVEL, 30, 16) == (0.0, 0.0, 0.0, 1.0)     # above the band


def k_at(x: int) -> float:
    """The Black level's raw step at target column x (v = 0.5, the band's middle)."""
    u = (x + .5 - PLACEMENT.x) / PLACEMENT.width
    position = u * BLACK_LEVEL_STEPS
    cell = min(int(position), BLACK_LEVEL_STEPS - 1)
    margin = (1 - BLACK_LEVEL_PATCH) / 2
    return (cell + 1) / SIGNAL_LEVELS if margin <= position - cell < 1 - margin else 0.0


def test_the_grid_is_the_look_of_its_lines_and_slate():
    """Target column 16 + 6 is u = 6.5 / 32 (fract(2.03) < 0.035): a line; 16 + 3 is u = 3.5 / 32
    (fract(1.09)): slate. Row 20 is v = 5.5 / 18 (fract(3.06)): no line across."""
    for column, colour in ((6, GRID_LINE), (3, GRID_SLATE)):
        u, v = (column + .5) / 32, 5.5 / 18
        assert ground(None, 16 + column, 20) == final_pixel(colour, operations(None), inside=True, lights=LIGHTS,
                                                            x=u, y=v, dither_at=(16 + column, 20))
    assert ground(None, 22, 20)[:3] != ground(None, 19, 20)[:3]


@pytest.mark.parametrize("pattern", [TestPattern.TARGET_PRE_SOFTWARE, TestPattern.TARGET_POST_SOFTWARE])
def test_a_calibration_target_is_the_board_over_the_frames_picture(pattern):
    board = picture_board(LANDSCAPE, MODULES)
    for x, y in ((20, 17), (31, 24), (45, 31)):
        u, v = (x + .5 - 16) / 32, (y + .5 - 15) / 18
        value = board_signal(*board_point(u, v, board), board)
        pixel = ground(pattern, x, y, board=board)
        if pattern is TestPattern.TARGET_PRE_SOFTWARE:
            assert pixel == (value, value, value, 1.0)
        else:
            assert pixel == final_pixel((0.0,) * 3, operations(pattern), inside=True, lights=LIGHTS, x=u, y=v,
                                        opening_uv=(u, v), board=board, dither_at=(x, y))
    with pytest.raises(ValueError, match="board"):
        ground(pattern, 20, 20)


def test_a_turned_screens_target_shows_the_placement_turned():
    """At turn 90 the target is 48 x 64 and holds the screen turned counter-clockwise: the
    placement's top-left screen pixel (16, 15) lies at target column 15, row 64 - 1 - 16 = 47."""
    assert target_size(Placement(0, 0, *SCREEN), 90) == (48, 64)
    turned = ground(TestPattern.BLACK_LEVEL, 20, 20, 90)
    assert turned[:3] == (0.0, 0.0, 0.0)
    # The screen's (x, y) = (16 + 31, 15 + 9) (the right cell, k = 20) sits at target (24, 16).
    assert buffer_to_picture(24.5, 16.5, (48, 64), 90) == pytest.approx((47.5 / 64, 24.5 / 48))
    assert ground(TestPattern.BLACK_LEVEL, 24, 16, 90) == ((20 / SIGNAL_LEVELS,) * 3 + (1.0,))


# -- PatternPass's draw over a recording GL (no GPU: the pixels are the display harness's) ----------

def recording_gl() -> MagicMock:
    """PyOpenGL's GLES 3 module as PatternPass calls it: every call recorded, a uniform's location
    its name, textures numbered."""
    gl = MagicMock()
    gl.GL_NO_ERROR = 0
    gl.glGetError.return_value = 0
    gl.glGetUniformLocation.side_effect = lambda program, name: name
    gl.glGenTextures.side_effect = iter(range(10, 100))
    return gl


class Target:
    def __init__(self, size):
        self.size, self.framebuffer = size, 7


def uniforms(gl: MagicMock, name: str) -> list[tuple]:
    calls = [call for call in gl.method_calls if call[0].startswith("glUniform") and call[1][0] == name]
    return [call[1][1:] for call in calls]


def request(pattern: TestPattern | None, turn: int = 0, modules=MODULES):
    board = picture_board(LANDSCAPE, modules) if pattern in (TestPattern.TARGET_PRE_SOFTWARE,
                                                              TestPattern.TARGET_POST_SOFTWARE) else None
    return PatternRequest(SCREEN, PLACEMENT, turn, operations(pattern), LIGHTS, None, board)


def test_a_draw_refuses_a_wrong_target_and_a_target_without_its_board():
    gl = recording_gl()
    patterns = PatternPass(gl)
    gl.reset_mock()
    with pytest.raises(ValueError, match="pattern_target_size"):
        patterns.draw(request(TestPattern.WHITE, 90), Target((64, 48)))
    with pytest.raises(ValueError, match="board"):
        patterns.draw(dataclasses.replace(request(TestPattern.TARGET_PRE_SOFTWARE), board=None), Target((64, 48)))
    assert not gl.glDrawArrays.called


@pytest.mark.parametrize("turn", [0, 90])
def test_a_draw_maps_each_target_pixel_to_its_screen_point(turn):
    gl = recording_gl()
    patterns = PatternPass(gl)
    size = target_size(Placement(0, 0, *SCREEN), turn)
    patterns.draw(request(TestPattern.GREY_RAMP, turn), Target(size))
    (origin,), (per_x,), (per_y,) = (uniforms(gl, name) for name in ("screen_origin", "screen_per_x", "screen_per_y"))
    for x, y in ((0, 0), (5, 40), (size[0] - 1, size[1] - 1)):
        point = tuple(o + (x + .5) * px + (y + .5) * py for o, px, py in zip(origin, per_x, per_y, strict=True))
        assert point == pytest.approx(buffer_to_picture(x + .5, y + .5, size, turn))
    assert uniforms(gl, "pattern_code") == [(PATTERN_CODES[TestPattern.GREY_RAMP],)]
    assert uniforms(gl, "placement") == [(16.0, 15.0, 32.0, 18.0)] and uniforms(gl, "screen_size") == [(64.0, 48.0)]
    gl.glDrawArrays.assert_called_once()


def test_the_qr_is_uploaded_only_when_its_modules_change():
    gl = recording_gl()
    patterns = PatternPass(gl)
    uploads = len(gl.glTexImage2D.call_args_list)          # the board's raster, once
    for modules in (MODULES, MODULES, None, MODULES):
        patterns.draw(request(TestPattern.TARGET_POST_SOFTWARE, modules=modules), Target(SCREEN))
    assert len(gl.glTexImage2D.call_args_list) == uploads + 1
    assert uniforms(gl, "board_qr_modules") == [(len(MODULES),), (len(MODULES),), (0,), (len(MODULES),)]
    patterns.close()
    assert gl.glDeleteTextures.call_count == 2              # the raster and the QR
