"""The fit pass's scalar reference (render redesign S1, bead B4; run ledger
.claude/runs/render-planes/s1-page.md): the target's layout for each turn, where a target pixel falls
in the picture as it hangs, the photo's crop window over the picture, and the Frame's lights with the
picture at its profile's own size. Pure; the shader is held to these in tests/native_fit_pass_harness.py."""
from __future__ import annotations

import math

import pytest

from contracts.display_turn import TURNS
from contracts.frame_fit import Placement
from contracts.frame_lighting import AT_FULL, CloseLight, FarLight, FrameLighting, Paper, WashLight
from contracts.models import FrameProfile, PhotoCrop
from contracts.node_picture import DisplayLight
from contracts.photo_crop import CENTRED, crop_window
from contracts.room_light import FixedColour, FixedEntry, RoomLightsDocument
from player.fit_pass import target_size
from player.geometry import buffer_to_picture, fit_source_uv, picture_lights

PLACEMENT = Placement(x=40, y=10, width=300, height=200)


@pytest.mark.parametrize("turn, size", [(0, (300, 200)), (90, (200, 300)), (180, (300, 200)), (270, (200, 300))])
def test_the_target_is_turned_for_its_screen(turn, size):
    assert target_size(PLACEMENT, turn) == size


@pytest.mark.parametrize("turn", [45, 360, -90, True])
def test_a_turn_that_is_not_a_quarter_is_refused(turn):
    with pytest.raises(ValueError, match="display_turn"):
        target_size(PLACEMENT, turn)
    with pytest.raises(ValueError, match="display_turn"):
        buffer_to_picture(0.5, 0.5, (2, 2), turn)


# The target's corners, laid out (top-left, top-right, bottom-right, bottom-left in memory order), as
# the picture's corners as it hangs. A screen hung turned 90 degrees clockwise has its top edge on the
# viewer's right, so the target's top-left is the picture's top-right; 270, the picture's bottom-left.
PICTURE_CORNERS = {
    0: ((0, 0), (1, 0), (1, 1), (0, 1)),
    90: ((1, 0), (1, 1), (0, 1), (0, 0)),
    180: ((1, 1), (0, 1), (0, 0), (1, 0)),
    270: ((0, 1), (0, 0), (1, 0), (1, 1)),
}


@pytest.mark.parametrize("turn", TURNS)
def test_the_targets_corners_are_the_pictures_corners(turn):
    width, height = target_size(PLACEMENT, turn)
    corners = ((0, 0), (width, 0), (width, height), (0, height))
    for (x, y), expected in zip(corners, PICTURE_CORNERS[turn], strict=True):
        assert buffer_to_picture(x, y, (width, height), turn) == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize("turn", TURNS)
def test_a_pixel_centre_lands_in_the_picture_proportionally(turn):
    """A quarter of the way along each of the target's sides is a quarter (or three quarters) of the
    way along the picture's; the picture is stretched evenly, never skewed."""
    width, height = target_size(PLACEMENT, turn)
    u, v = buffer_to_picture(width / 4, height / 4, (width, height), turn)
    assert {round(u, 12), round(v, 12)} <= {0.25, 0.75}


@pytest.mark.parametrize("crop", [None, PhotoCrop(offset=0), PhotoCrop(offset=0.3), PhotoCrop(offset=1)])
@pytest.mark.parametrize("source, aspect", [((4000, 3000), 16 / 9), ((3000, 4000), 16 / 9), ((1920, 1080), 16 / 9),
                                            ((4000, 3000), 9 / 16)])
def test_the_crop_window_is_stretched_over_the_picture(crop, source, aspect):
    left, top, right, bottom = crop_window(crop or CENTRED, *source, aspect)
    assert fit_source_uv(0, 0, aspect, source, crop) == pytest.approx((left, top))
    assert fit_source_uv(1, 1, aspect, source, crop) == pytest.approx((right, bottom))
    assert fit_source_uv(0.25, 0.75, aspect, source, crop) == pytest.approx(
        (left + 0.25 * (right - left), top + 0.75 * (bottom - top)))


def test_the_crop_offset_picks_the_photos_edge():
    """A 4:3 photo on a 16:9 picture: offset 0 shows its top edge, 1 its bottom edge."""
    assert fit_source_uv(0.5, 0, 16 / 9, (4000, 3000), PhotoCrop(offset=0))[1] == 0
    assert fit_source_uv(0.5, 1, 16 / 9, (4000, 3000), PhotoCrop(offset=1))[1] == pytest.approx(1)


PROFILE = FrameProfile(width_px=1600, height_px=900, diagonal_inches=27)
PAPER = Paper(whiteness=0.9, ink_density=1.8)
LIGHTING = FrameLighting(mat_depth_mm=8, paper=PAPER, lights=(
    WashLight(light="room", lux=300, reference=AT_FULL),
    FarLight(light="lamp", lux=600, reference=AT_FULL, from_degrees=20, steep_degrees=30, softness_degrees=20),
    CloseLight(light="spot", lux=1500, reference=AT_FULL, across_mm=40, up_mm=350, out_mm=250,
               inner_beam_degrees=20, outer_beam_degrees=40)))
ROOM = RoomLightsDocument(change=1, lights={
    "room": FixedEntry(colour=FixedColour(kelvin=6500)), "lamp": FixedEntry(colour=FixedColour(kelvin=3000)),
    "spot": FixedEntry(colour=FixedColour(kelvin=4000))})
DISPLAY = DisplayLight(matched_lux=250, black=0.01)


WIDTH_MM, HEIGHT_MM = 27 * 25.4 * 16 / math.hypot(16, 9), 27 * 25.4 * 9 / math.hypot(16, 9)


@pytest.mark.parametrize("document", [ROOM, None])
def test_picture_lights_are_the_pictures_at_its_profiles_own_size(document):
    """The lights' mat space is the picture's own rectangle at the profile's physical size, with the
    mat, the paper, each light that adds light, and the peak over it (no light: none)."""
    picture = picture_lights(LIGHTING, document, PROFILE, DISPLAY)
    assert picture.size_mm == pytest.approx((WIDTH_MM, HEIGHT_MM))
    assert (picture.depth_mm, picture.paper) == (8, PAPER)
    assert len(picture.lights) == (3 if document else 0)
    assert (picture.peak > 0) is (document is not None)


def test_picture_lights_take_no_quarter_turn():
    """A screen's turn turns the screen, not the wall: the far light's `toward` is its own
    from_degrees and the close light sits across and up from the picture's centre."""
    _, far, close = picture_lights(LIGHTING, ROOM, PROFILE, DISPLAY).lights
    turn = math.radians(20)
    assert far.toward == pytest.approx((math.sin(turn), -math.cos(turn)))
    assert close.position_mm == pytest.approx((WIDTH_MM / 2 + 40, HEIGHT_MM / 2 - 350, 250))
