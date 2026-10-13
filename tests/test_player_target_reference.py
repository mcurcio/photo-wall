"""The calibration targets in the Player's scalar reference (player/geometry.py, decision 0026):
the opening's one size rule, the target placed at its physical size (a quarter turn clockwise in
a portrait opening), the two stages (owner, 2026-10-13: pre-software is the display alone, raw
everywhere; post-software runs through the Photo Wall steps, the dark box still raw), and the
final pass for both. Pure: the shader is held to this by tests/native_renderer_pixel_harness.py."""
from __future__ import annotations

import dataclasses
import itertools
import math

import pytest

import player.geometry as geometry
from contracts.calibration_target import (
    MARKER_BITS,
    MARKER_MODULES,
    QR_MM,
    QR_QUIET_MODULES,
    SCREEN_TARGET_MM,
    Surface,
    TargetCode,
    calibration_target,
)
from contracts.lut import LUT_BYTES, LutRef
from contracts.models import CoverFit, FrameProfile, Light, Mat
from contracts.node_frame import LutMix, PictureOperations, srgb_to_linear
from contracts.tuning import TestPattern
from player.calibration_target import target_modules
from player.geometry import (
    TargetDraw,
    final_pixel,
    mat_shadow,
    opening_size_mm,
    target_point,
    target_signal,
)

PRE, POST = TestPattern.TARGET_PRE_SOFTWARE, TestPattern.TARGET_POST_SOFTWARE
PROFILE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=27)
BIG = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=32)      # holds the whole target
LIT_MAT = Mat(depth_mm=8, lights=(Light(from_degrees=0, steep_degrees=30, softness_degrees=20,
                                        strength_tenths=15, colour_kelvin=3000),))
LUT = LutMix(LutRef("ab" * 32, "lut33-test"), 1.0)
GLOWING_TABLE = bytes([200]) * LUT_BYTES
SHADE = (.6, .55, .5)
TARGET = calibration_target()
DRAW = TargetDraw(opening_size_mm(CoverFit(), BIG), target_modules(TargetCode("p-" + "ab" * 16, "HDMI-A-1", 9)))
CENTRE = (SCREEN_TARGET_MM[0] / 2, SCREEN_TARGET_MM[1] / 2)


def operations(pattern, light=1.0, *, floor=0.0, lut=None, saturation=1.0) -> PictureOperations:
    return PictureOperations("HDMI-A-1", 9, (1.0, .9, .8), light, 1.2, 1.1, saturation=saturation,
                             black_floor=floor, lut=lut, test_pattern=pattern)


def uv_at(x_mm: float, y_mm: float, opening_mm: tuple[float, float] = DRAW.opening_mm) -> tuple[float, float]:
    """The landscape opening's (u, v) where the target's (x, y) is drawn."""
    width, height = opening_mm
    return ((x_mm - CENTRE[0]) / width + .5, (y_mm - CENTRE[1]) / height + .5)


def screen_patches():
    return [p for g in TARGET.groups for p in g.patches if p.surface is Surface.SCREEN]


def marker_centres():
    for marker in (m for m in TARGET.markers if m.surface is Surface.SCREEN):
        module = marker.rect.width_mm / MARKER_MODULES
        for row, column in itertools.product(range(MARKER_MODULES), repeat=2):
            border = row in (0, MARKER_MODULES - 1) or column in (0, MARKER_MODULES - 1)
            white = not border and MARKER_BITS[marker.marker_id][row - 1][column - 1] == "1"
            yield (marker.rect.x_mm + (column + .5)*module, marker.rect.y_mm + (row + .5)*module), white


def qr_centres(modules):
    """(centre, expected signal) of every module of the QR square, the quiet zone included."""
    side = len(modules) + 2*QR_QUIET_MODULES
    size = QR_MM / side
    for row, column in itertools.product(range(side), repeat=2):
        r, c = row - QR_QUIET_MODULES, column - QR_QUIET_MODULES
        dark = 0 <= r < len(modules) and 0 <= c < len(modules) and modules[r][c]
        yield (TARGET.qr.x_mm + (column + .5)*size, TARGET.qr.y_mm + (row + .5)*size), 0.0 if dark else 1.0


def box_points():
    box = TARGET.dark_box
    return [(box.x_mm + fx*box.width_mm, box.y_mm + fy*box.height_mm) for fx, fy in ((.02, .02), (.5, .5), (.97, .9))]


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


# -- 2. Placement ------------------------------------------------------------------------------

def test_a_landscape_opening_draws_the_target_centred_at_its_size():
    opening = (600.0, 400.0)
    assert target_point(.5, .5, opening) == pytest.approx(CENTRE)
    assert target_point(.5 + 10/600, .5, opening) == pytest.approx((CENTRE[0] + 10, CENTRE[1]))
    assert target_point(.5, .5 + 10/400, opening) == pytest.approx((CENTRE[0], CENTRE[1] + 10))
    assert target_point(0.0, 0.0, opening) == pytest.approx((CENTRE[0] - 300, CENTRE[1] - 200))


def test_a_portrait_opening_turns_the_target_a_quarter_clockwise():
    opening = (400.0, 600.0)
    assert target_point(.5, .5, opening) == pytest.approx(CENTRE)
    assert target_point(.5, .5 + 10/600, opening) == pytest.approx((CENTRE[0] + 10, CENTRE[1]))     # x along v
    assert target_point(.5 + 10/400, .5, opening) == pytest.approx((CENTRE[0], CENTRE[1] - 10))     # top at the right
    assert target_point(1.0, .5, opening)[1] < target_point(0.0, .5, opening)[1]


# -- 3. The stages -----------------------------------------------------------------------------

def test_pre_software_is_raw_everywhere():
    points = ([p.rect.centre() for p in screen_patches()] + [c for c, _ in marker_centres()]
              + [c for c, _ in qr_centres(DRAW.modules)] + box_points())
    for point in points:
        assert target_signal(PRE, *uv_at(*point), DRAW)[1] is True, point


def test_post_software_is_raw_only_in_the_dark_box():
    for patch in screen_patches():
        assert target_signal(POST, *uv_at(*patch.rect.centre()), DRAW) == ((patch.signal,) * 3, False), patch
    for centre, _ in itertools.chain(marker_centres(), qr_centres(DRAW.modules)):
        assert target_signal(POST, *uv_at(*centre), DRAW)[1] is False, centre
    for point in box_points():
        assert target_signal(POST, *uv_at(*point), DRAW) == ((0.0,) * 3, True), point


@pytest.mark.parametrize("pattern", [PRE, POST])
def test_the_qr_square_draws_the_modules_and_a_light_quiet_zone(pattern):
    quiet = dark = 0
    for centre, expected in qr_centres(DRAW.modules):
        assert target_signal(pattern, *uv_at(*centre), DRAW)[0] == (expected,) * 3, centre
        dark += expected == 0.0
        quiet += expected == 1.0
    assert dark > 100 and quiet > 100


@pytest.mark.parametrize("pattern", [PRE, POST])
def test_markers_draw_their_bits(pattern):
    for centre, white in marker_centres():
        assert target_signal(pattern, *uv_at(*centre), DRAW)[0] == ((1.0 if white else 0.0),) * 3, centre


def test_only_the_targets_are_target_signal():
    for pattern in TestPattern:
        if pattern not in (PRE, POST):
            with pytest.raises(ValueError, match="pattern"):
                target_signal(pattern, .5, .5, DRAW)
    with pytest.raises(ValueError, match="pattern"):
        geometry.pattern_signal(PRE, .5, .5)


# -- 4. The final pass -------------------------------------------------------------------------

def test_pre_software_is_the_signal_whatever_the_settings():
    lifted = operations(PRE, 1.5, floor=.1, lut=LUT, saturation=.5)
    points = [(p.rect.centre(), p.signal) for p in screen_patches()] + [(p, 0.0) for p in box_points()]
    for point, signal in points:
        drawn = final_pixel((.3, .6, .9), lifted, inside=True, shade=SHADE, lut=GLOWING_TABLE,
                            opening_uv=uv_at(*point), target=DRAW)
        assert drawn == (signal, signal, signal, 1.0), point


def test_post_software_is_a_photo_of_the_signal_without_the_shade():
    lifted = operations(POST, 1.5, floor=.1, lut=LUT, saturation=.5)
    photo = dataclasses.replace(lifted, test_pattern=None)
    for patch in screen_patches():
        drawn = final_pixel((.3, .6, .9), lifted, inside=True, shade=SHADE, lut=GLOWING_TABLE,
                            opening_uv=uv_at(*patch.rect.centre()), target=DRAW)
        expected = final_pixel((srgb_to_linear(patch.signal),) * 3, photo, inside=True, lut=GLOWING_TABLE)
        assert drawn == expected, patch
    shaded = final_pixel((1.0,) * 3, photo, inside=True, shade=SHADE, lut=GLOWING_TABLE)
    assert shaded != final_pixel((1.0,) * 3, photo, inside=True, lut=GLOWING_TABLE)     # the shade is not vacuous


def test_post_softwares_dark_box_stays_black_under_a_floor():
    lifted = operations(POST, 1.5, floor=.1, lut=LUT)
    for point in box_points():
        assert final_pixel((.5,) * 3, lifted, inside=True, shade=SHADE, lut=GLOWING_TABLE,
                           opening_uv=uv_at(*point), target=DRAW) == (0.0, 0.0, 0.0, 1.0), point
    assert final_pixel((0.0,) * 3, dataclasses.replace(lifted, test_pattern=None), inside=True)[0] > .1


@pytest.mark.parametrize("pattern", [PRE, POST])
def test_a_target_needs_its_draw_and_the_openings_uv(pattern):
    with pytest.raises(ValueError, match="target"):
        final_pixel((0.0,) * 3, operations(pattern), inside=True, opening_uv=(.5, .5))
    with pytest.raises(ValueError, match="opening_uv"):
        final_pixel((0.0,) * 3, operations(pattern), inside=True, target=DRAW)


@pytest.mark.parametrize("pattern", [PRE, POST])
def test_outside_the_opening_is_black(pattern):
    white = next(p for p in screen_patches() if p.signal == 1.0)
    assert final_pixel((1.0,) * 3, operations(pattern, floor=.1), inside=False,
                       opening_uv=uv_at(*white.rect.centre()), target=DRAW) == (0.0, 0.0, 0.0, 1.0)
