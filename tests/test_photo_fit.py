"""The photo fit rule (roadmap 5a design rule 2; run ledger .claude/runs/photo-5a.md, slice R1,
cases 1-9; follow-ups B1, cases 1-7: the Frame output and every tuning value a setting; frame box
run fb-8: a Frame is judged by its Frame output alone). Frames are 3840 x 2160 at 55 inches,
full screen on a 3840 x 2160 Display, unless named."""
from __future__ import annotations

import math

import pytest
from support.frame_outputs import target_of
from support.key_content_builders import banded_grid, face, key_content, measured
from support.real_crops import EASTER, EASTER_SIZE, FIREPLACE_PX, MICKEY, MICKEY_SIZE

from central.catalog import Candidate
from central.installation_models import FrameOutput
from central.wall_plan.fit import (
    DoesNotFit,
    Fits,
    FitTarget,
    FitVerdict,
    band,
    crop_share,
    faces_judged,
    fit_target,
    interest_judged,
    interest_kept,
    judge,
    large_frame,
    place,
    shape_fit,
)
from central.wall_plan.fit_settings import FitSettings
from central.wall_plan.key_content import KeyContent, UnitBox
from contracts.frame_fit import FreeAxis, free_axis
from contracts.models import Calibration, PhotoCrop, PositionBox, frame_output, frame_size_mm
from contracts.photo_crop import crop_window

UHD = target_of(3840, 2160, inches=55)
UHD_40 = target_of(3840, 2160, inches=40)
NO_VIDEO = target_of(3840, 2160, inches=55, video=False)
TALL = target_of(1080, 1920, inches=32)
NEARLY_SQUARE = target_of(950, 1000, inches=20)
FOUR_THREE = target_of(1024, 768, inches=10)
DISPLAY = (3840, 2160)
HOUSE = FitSettings()
LIMIT = HOUSE.crop_limit


def _judge(candidate: Candidate, target: FitTarget = UHD,
           crop_limit: float = LIMIT, settings: FitSettings = HOUSE) -> FitVerdict:
    key = candidate.analysis.key if candidate.analysis is not None else None
    return judge(candidate.kind, candidate.original_width, candidate.original_height, key,
                 target, crop_limit, settings)


def _refused(verdict: FitVerdict) -> str:
    assert isinstance(verdict, DoesNotFit), verdict
    return verdict.reason


def _opening(width: float) -> FitTarget:
    """UHD's Frame drawn as a centred box `width` of the 3840 x 2160 Display's width and height."""
    margin = (1 - width) / 2
    calibration = Calibration(fit=PositionBox(left=margin, top=margin, width=width, height=width))
    return UHD.model_copy(update={"output_px": frame_output(calibration, DISPLAY, 0)})


MAT = _opening(.4)                                           # a 40 % x 40 % opening


def test_an_evenly_spread_four_by_three_photo_fits_sixteen_by_nine_centred() -> None:
    verdict = _judge(measured("p", 4000, 3000))
    assert isinstance(verdict, Fits)
    assert verdict.crop_share == pytest.approx(.25)
    assert verdict.crop.offset == .5
    assert verdict.interest_kept == pytest.approx(.75)


def test_interest_in_the_top_quarter_keeps_the_top() -> None:
    verdict = _judge(measured("p", 4000, 3000, grid=banded_grid(rows=12, top=.25, weight=.9)))
    assert isinstance(verdict, Fits)
    assert verdict.crop.offset == 0


def test_interest_at_both_the_top_and_the_bottom_cannot_be_cropped() -> None:
    grid = banded_grid(rows=12, top=.25, bottom=.25, weight=.9)
    assert _refused(_judge(measured("p", 4000, 3000, grid=grid))) == "interest_cut"


def test_the_crop_limit_is_the_backstop() -> None:
    assert _refused(_judge(measured("p", 2000, 3000))) == "beyond_crop_limit"
    assert _refused(_judge(measured("p", 4000, 4000))) == "beyond_crop_limit"
    assert shape_fit("image", 4000, 4000, UHD, .5, HOUSE, size_known=True) is None
    assert isinstance(_judge(measured("p", 4000, 4000), crop_limit=.5), Fits)


def test_ratio_not_labels_a_landscape_photo_fills_a_nearly_square_portrait_frame() -> None:
    verdict = _judge(measured("p", 2100, 2000), NEARLY_SQUARE)
    assert isinstance(verdict, Fits)
    assert verdict.crop_share == pytest.approx(1 - .95 / 1.05)


def test_faces_are_kept_whole() -> None:
    left = face(0, .3, .1, .4)
    right = face(.9, .3, 1, .4)
    verdict = _judge(measured("p", 2000, 3000, faces=(left,)), TALL)
    assert isinstance(verdict, Fits)
    assert verdict.crop.offset == 0
    assert _refused(_judge(measured("p", 2000, 3000, faces=(left, right)), TALL)) == "face_cut"


def test_the_kept_window_needs_the_frame_output() -> None:
    assert isinstance(_judge(measured("p", 4000, 3000)), Fits)
    assert _refused(_judge(measured("p", 3264, 2448))) == "below_frame_pixels"


# --- Follow-ups B1: the Frame output, not the Display -----------------------------------------

def test_a_mat_opening_needs_only_its_own_pixels_not_the_displays() -> None:
    """B1 case 1: a 40 % x 40 % opening on a 3,840 x 2,160 Display is a 1,536 x 864
    Frame output; a 1,600 x 900 photo fills it, and judged against the Display it would not."""
    assert MAT.output_px == (1536, 864)
    photo = measured("p", 1600, 900)
    assert isinstance(_judge(photo, MAT), Fits)
    assert UHD.output_px == DISPLAY
    assert _refused(_judge(photo, UHD)) == "below_frame_pixels"


def test_an_unmeasured_size_is_judged_by_shape_only() -> None:
    """An image whose readable size is not measured yet: rule 2 is skipped (`size_known` False),
    so nothing is refused on a size no one measured."""
    assert shape_fit("image", 400, 300, FOUR_THREE, LIMIT, HOUSE, size_known=False) is None
    assert shape_fit("image", 400, 300, FOUR_THREE, LIMIT, HOUSE, size_known=True) == \
        DoesNotFit(reason="below_frame_pixels")
    assert _refused(_judge(measured("p", 400, 300), FOUR_THREE)) == "below_frame_pixels"


# --- Follow-ups B1: every tuning value is a setting, applied when judged ------------------------

def _settings(**values) -> FitSettings:
    return HOUSE.model_copy(update=values)


def test_face_padding_is_applied_when_judged() -> None:
    """B1 case 4: one close-up face from 2 % to 74 % of a 4:3 photo's height, on a 16:9 Frame
    (a window of 75 % of the height): padded by 20 % it overflows the window, unpadded it fits.
    One KeyContent, nothing measured again."""
    photo = measured("p", 4000, 3000, faces=(face(.4, .02, .6, .74),))
    assert photo.analysis.key == key_content(4000, 3000, faces=(face(.4, .02, .6, .74),))
    assert _refused(_judge(photo, settings=_settings(face_padding=.2))) == "face_cut"
    assert isinstance(_judge(photo, settings=_settings(face_padding=0)), Fits)


def test_small_faces_do_not_decide_a_crop_unless_the_setting_says_so() -> None:
    """B1 case 5: two faces of 0.1 % of the area, at the top and the bottom edge of a 4:3 photo
    on a 16:9 Frame: ignored at min_face_share 0.002, face_cut at 0."""
    top, bottom = face(.48, 0, .52, .025), face(.48, .975, .52, 1)
    photo = measured("p", 4000, 3000, faces=(top, bottom))
    assert isinstance(_judge(photo, settings=_settings(min_face_share=.002)), Fits)
    assert _refused(_judge(photo, settings=_settings(min_face_share=0))) == "face_cut"


def test_a_flat_saliency_map_is_judged_as_even() -> None:
    """B1 case 6: interest split between the top and bottom rows with a spread of 0.03: even
    (fits) at min_salience_spread 0.05, interest_cut at 0.01."""
    grid = banded_grid(rows=12, top=.25, bottom=.25, weight=.9)
    photo = measured("p", 4000, 3000, grid=grid, spread=.03)
    assert isinstance(_judge(photo, settings=_settings(min_salience_spread=.05)), Fits)
    assert _refused(_judge(photo, settings=_settings(min_salience_spread=.01))) == "interest_cut"


def test_the_interest_density_limit_is_a_setting() -> None:
    grid = banded_grid(rows=12, top=.25, bottom=.25, weight=.9)
    photo = measured("p", 4000, 3000, grid=grid)
    assert _refused(_judge(photo)) == "interest_cut"
    assert isinstance(_judge(photo, settings=_settings(max_lost_interest_density=3)), Fits)


def test_the_house_defaults_admit_ordinary_photos_the_old_ones_refused() -> None:
    """Owner, 2026-10-12: "the default values are very conservative and exclude many photos".
    A 4:3 photo on a 16:9 Frame whose cut quarters hold 1.5 times their share of the interest
    (texture at the edges), and a close-up face from 8 % to 70 % of its height, fit at the house
    defaults; at the old ones (density 1.25, face room 20 %) each was refused."""
    textured = measured("p", 4000, 3000, grid=banded_grid(rows=12, top=.25, bottom=.25, weight=.5))
    close_up = measured("q", 4000, 3000, faces=(face(.35, .08, .65, .70),))
    old = _settings(max_lost_interest_density=1.25, face_padding=.2)
    assert isinstance(_judge(textured), Fits) and isinstance(_judge(close_up), Fits)
    assert _refused(_judge(textured, settings=old)) == "interest_cut"
    assert _refused(_judge(close_up, settings=old)) == "face_cut"


def test_the_window_steps_are_a_setting() -> None:
    """A face whole only in a window off the coarse steps' grid: found at 256 steps."""
    # The 16:9 window over a 4:3 photo slides over 25 % of its height; the face needs the window
    # to start within 0.1 % of 3/256 of that travel.
    start = .25 * 3 / 256
    head = face(.45, start, .55, start + .75 - 1e-4)
    photo = measured("p", 4000, 3000, faces=(head,))
    fine = _judge(photo, settings=_settings(window_steps=256, face_padding=0))
    assert isinstance(fine, Fits) and fine.crop.offset == pytest.approx(3 / 256)
    assert _refused(_judge(photo, settings=_settings(window_steps=8, face_padding=0))) == \
        "face_cut"


def test_the_large_frame_video_floor_is_a_setting() -> None:
    video = measured("v", 1280, 720, kind="video").model_copy(update={"analysis": None})
    assert _refused(_judge(video, UHD_40)) == "below_frame_pixels"
    assert isinstance(_judge(video, UHD_40, settings=_settings(large_frame_inches=41)), Fits)
    assert isinstance(_judge(video, UHD_40, settings=_settings(large_frame_video_short_side=720)),
                      Fits)


def test_a_band_is_the_ratios_rule_one_accepts() -> None:
    """B1 case 7: band(16/9, 0.35) = (16/9 x 0.65, 16/9 / 0.65); a photo exactly on either
    edge passes rule 1, one just outside does not."""
    low, high = band(16 / 9, .35)
    assert (low, high) == pytest.approx((16 / 9 * .65, 16 / 9 / .65))
    for width, height, edge in ((5200, 4500, low), (3200, 1170, high)):   # 52:45, 320:117
        assert width / height == pytest.approx(edge)
        assert crop_share(width, height, UHD.aspect) == pytest.approx(.35)
        assert shape_fit("image", width, height, UHD, .35, HOUSE, size_known=False) is None
        assert shape_fit("image", width, height, UHD, .349, HOUSE, size_known=False) == \
            DoesNotFit(reason="beyond_crop_limit")
    for aspect, limit in ((0, .35), (16 / 9, 0), (16 / 9, 1)):
        with pytest.raises(ValueError):
            band(aspect, limit)


def test_videos_are_centred_and_judged_by_their_own_floor() -> None:
    video = measured("v", 1920, 1080, kind="video").model_copy(update={"analysis": None})
    assert _refused(_judge(video, NO_VIDEO)) == "no_video_here"
    small = video.model_copy(update={"original_width": 1280, "original_height": 720})
    assert _refused(_judge(small, UHD_40)) == "below_frame_pixels"
    verdict = _judge(video)
    assert isinstance(verdict, Fits)
    assert verdict.crop.offset == .5
    assert verdict.interest_kept == 1


def test_an_image_is_judged_only_once_measured_and_within_a_sane_limit() -> None:
    with pytest.raises(ValueError):
        judge("image", 4000, 3000, None, UHD, LIMIT, HOUSE)
    key = measured("p", 4000, 3000).analysis.key
    for limit in (0, 1, -.1, 1.5):
        with pytest.raises(ValueError):
            judge("image", 4000, 3000, key, UHD, limit, HOUSE)


@pytest.mark.parametrize(("kind", "width", "height", "target", "limit"), [
    ("image", 2000, 3000, UHD, .35),
    ("image", 4000, 4000, UHD, .35),
    ("image", 3264, 2448, UHD, .35),
    ("video", 1920, 1080, NO_VIDEO, .35),
    ("video", 1280, 720, UHD_40, .35),
])
def test_shape_fit_agrees_with_judge_on_every_size_only_refusal(kind, width, height, target,
                                                                 limit) -> None:
    key = measured("p", width, height).analysis.key if kind == "image" else None
    refused = shape_fit(kind, width, height, target, limit, HOUSE, size_known=True)
    assert refused is not None
    assert judge(kind, width, height, key, target, limit, HOUSE) == refused


def test_interest_kept_is_the_grid_weight_inside_the_window() -> None:
    """The row and column totals a whole-width or whole-height window sums equal the cell by
    cell share (the oracle here), for banded and uneven grids."""
    grids = (banded_grid(rows=12, top=.25, weight=.9), banded_grid(left=.3, right=.1, weight=.7),
             key_content(4000, 3000).interest)

    def cell_by_cell(grid, window) -> float:
        left, top, right, bottom = window
        total = 0.0
        for row in range(grid.rows):
            for column in range(grid.columns):
                x0, x1 = column / grid.columns, (column + 1) / grid.columns
                y0, y1 = row / grid.rows, (row + 1) / grid.rows
                wide = max(0.0, min(right, x1) - max(left, x0)) / (x1 - x0)
                tall = max(0.0, min(bottom, y1) - max(top, y0)) / (y1 - y0)
                total += grid.weights[row * grid.columns + column] * wide * tall
        return total

    for grid in grids:
        for window in ((0, .1, 1, .85), (.2, 0, .93, 1), (0, 0, 1, 1), (.1, .2, .6, .9)):
            assert interest_kept(grid, window) == pytest.approx(cell_by_cell(grid, window))


# --- Frame box run fb-8: the Frame output is the Frame -------------------------------------------

def _frame(box: PositionBox, *, screen: tuple[int, int] | None = (1920, 1080),
           display_mm: tuple[int, int] | None = (950, 540), video: bool = True) -> FrameOutput:
    """A Frame whose Position is `box` on a screen of `screen` pixels (None: not known) on a
    Display of `display_mm` (None: it states no size), hung upright, as frame_outputs_in reads it."""
    calibration = Calibration(fit=box)
    return FrameOutput(frame_id="hall", turn=0, video=video, display_px=screen,
                       output_px=frame_output(calibration, screen, 0),
                       size_mm=frame_size_mm(box, display_mm, 0))


def _target(frame: FrameOutput) -> FitTarget:
    target = fit_target(frame)
    assert target is not None
    return target


TALL_BOX = PositionBox(left=.5, top=.25, width=.25, height=.5)
WHOLE = PositionBox()
HALF = PositionBox(left=.25, top=.25, width=.5, height=.5)


def test_the_frames_shape_is_its_box_not_sixteen_by_nine() -> None:
    """A box 0.25 x 0.5 on a 1,920 x 1,080 screen is a 480 x 540 Frame: a 16:9 photo is beyond a
    30 % crop limit there, a 9:10 one fits."""
    target = _target(_frame(TALL_BOX))
    assert target.output_px == (480, 540)
    assert target.aspect == pytest.approx(480 / 540)
    assert shape_fit("image", 3840, 2160, target, .3, HOUSE, size_known=True) == \
        DoesNotFit(reason="beyond_crop_limit")
    assert shape_fit("image", 2700, 3000, target, .3, HOUSE, size_known=True) is None


def test_a_720p_video_is_judged_by_the_frames_size_from_its_display() -> None:
    """On a Display of 950 x 540 mm (43 inches across) with large Frames from 40 inches: refused
    on a whole-screen Frame, accepted on a box half as wide and high (21.5 inches), refused on a
    Frame whose Display states no size (an unknown size counts as large)."""
    video = measured("v", 1280, 720, kind="video").model_copy(update={"analysis": None})
    settings = _settings(large_frame_inches=40)
    whole, half = _target(_frame(WHOLE)), _target(_frame(HALF))
    unsized = _target(_frame(WHOLE, display_mm=None))
    assert math.hypot(*whole.size_mm) / 25.4 == pytest.approx(43.03, abs=.01)
    assert _refused(_judge(video, whole, settings=settings)) == "below_frame_pixels"
    assert isinstance(_judge(video, half, settings=settings), Fits)
    assert unsized.size_mm is None
    assert large_frame(unsized, settings)
    assert _refused(_judge(video, unsized, settings=settings)) == "below_frame_pixels"


def test_a_frame_that_shows_no_videos_gets_none() -> None:
    video = measured("v", 1920, 1080, kind="video").model_copy(update={"analysis": None})
    assert _refused(_judge(video, _target(_frame(WHOLE, video=False)))) == "no_video_here"
    assert isinstance(_judge(video, _target(_frame(WHOLE))), Fits)


def test_a_frame_with_no_known_screen_has_no_target() -> None:
    """No screen size known: no Frame output, so no shape, and nothing is judged for it."""
    frame = _frame(WHOLE, screen=None)
    assert frame.output_px is None
    assert fit_target(frame) is None


# --- Where the window sits: the faces at their place in the photo (`place`) ----------------------
# Owner, 2026-10-25: "The last two photos on the frame were oddly cropped. The one with Mickey and
# the one with Easter bunny"; then: "A lone face should sit “in the same relative position within
# the image as the original (if possible), and cropped only as much as needed to fit the scaled
# image into the frame." On a photo with faces the window keeps the faces' area-weighted centre at
# its place in the photo; a photo without faces keeps the most interest.

FIREPLACE = FitTarget(output_px=FIREPLACE_PX, video=True, size_mm=None)
TWO_BY_THREE = target_of(720, 1080, inches=20)
STEP = 1 / HOUSE.window_steps


def _head(centre: float, tall: float, *, x: float = .5) -> UnitBox:
    """A face `tall` of the photo's height and three quarters of that wide, centred at
    (`x`, `centre`)."""
    wide = tall * .75
    return face(x - wide / 2, centre - tall / 2, x + wide / 2, centre + tall / 2)


def _placed(width: int, height: int, key: KeyContent, target: FitTarget) -> tuple[float, float]:
    """The chosen offset and the judged faces' area-weighted centre along the free axis (a share
    of the photo); the photo must fit with every face whole."""
    verdict = judge("image", width, height, key, target, .6, HOUSE)
    assert isinstance(verdict, Fits), verdict
    window = crop_window(verdict.crop, width, height, target.aspect)
    axis = free_axis(width, height, target.aspect)
    faces = faces_judged(key, HOUSE)
    assert faces and all(_inside(one, window) for one in faces)
    spans = [(one.left, one.right) if axis.along == "x" else (one.top, one.bottom)
             for one in faces]
    areas = [(one.right - one.left) * (one.bottom - one.top) for one in faces]
    centre = sum(area * (start + end) / 2 for area, (start, end) in zip(areas, spans)) / sum(areas)
    return verdict.crop.offset, centre


def _where(centre: float, offset: float, axis: FreeAxis) -> float:
    """Where `centre` (a share of the photo along `axis`) sits in the window at `offset`, as a
    share of the window's length."""
    return (centre - axis.start(offset)) / axis.length


def _inside(box: UnitBox, window) -> bool:
    left, top, right, bottom = window
    return (box.left >= left - 1e-9 and box.right <= right + 1e-9
            and box.top >= top - 1e-9 and box.bottom <= bottom + 1e-9)


@pytest.mark.parametrize(("size", "key", "offset"), [
    (MICKEY_SIZE, MICKEY, 22 / 64),     # the most-interest rule: 0.453, a face at the top edge
    (EASTER_SIZE, EASTER, 21 / 64),     # the most-interest rule: 0, the children cut at the chin
])
def test_the_two_photos_cropped_badly_on_the_fireplace_frame_keep_their_faces_in_place(
        size, key, offset) -> None:
    """Their stored key content on the fireplace Frame: the faces' centre (Mickey 0.347, Easter
    0.327 of the photo's height) sits at the same share of the window, within half a step, and
    the verdict's interest kept is that window's.
    Mutation probes: choose the window keeping the most interest -> Mickey 0.453, Easter 0;
    aim the window's centre at the faces' -> Mickey 0.172, Easter 0.125; the faces' window with
    its room shared by their size (PR 138's first aim) -> Mickey 0.172, Easter 0.141; weight the
    faces equally -> Mickey 0.359; report the most interest any passing window keeps -> another
    window's share."""
    placed, centre = _placed(*size, key, FIREPLACE)
    axis = free_axis(*size, FIREPLACE.aspect)
    assert placed == offset
    assert abs(_where(centre, placed, axis) - centre) <= STEP * (1 - axis.length) / axis.length / 2
    verdict = judge("image", *size, key, FIREPLACE, .6, HOUSE)
    window = crop_window(verdict.crop, *size, FIREPLACE.aspect)
    assert verdict.interest_kept == interest_kept(interest_judged(key, HOUSE), window)


@pytest.mark.parametrize(("size", "key", "offset"), [
    (MICKEY_SIZE, MICKEY, 1), (EASTER_SIZE, EASTER, 0),
])
def test_without_faces_the_window_keeping_the_most_interest_wins(size, key, offset) -> None:
    """The same photos with no faces found keep today's rule: Mickey's floor and the bunny's tree
    hold the most interest, and the window goes to them.
    Mutation probe: centre a photo without faces -> 0.5."""
    verdict = judge("image", *size, key.model_copy(update={"faces": ()}), FIREPLACE, .6, HOUSE)
    assert isinstance(verdict, Fits) and verdict.crop.offset == offset


def test_a_lone_face_keeps_its_place_in_the_photo() -> None:
    """One face at 30 % of a 3 : 4 photo's height sits at 30 % of the window's, within half a
    step, on even interest and with half the interest in the bottom quarter (which a window
    without the face would take, at offset 1).
    Mutation probes: choose the window keeping the most interest -> 0.5; aim the window's centre
    at the face -> 0.078125 (the face centred)."""
    width, height = 4500, 6000
    axis = free_axis(width, height, FIREPLACE.aspect)
    for grid in (None, banded_grid(rows=12, bottom=.25, weight=.5)):
        offset, centre = _placed(width, height, key_content(width, height,
                                                            faces=(_head(.3, .06),), grid=grid),
                                 FIREPLACE)
        assert centre == pytest.approx(.3)
        assert offset == 19 / 64
        assert abs(_where(.3, offset, axis) - .3) <= STEP * (1 - axis.length) / axis.length / 2


@pytest.mark.parametrize(("centre", "offset", "toward"), [(.05, 1 / 64, 1), (.95, 63 / 64, -1)])
def test_a_face_near_an_edge_moves_only_as_far_as_keeping_it_whole_needs(centre, offset,
                                                                         toward) -> None:
    """A face so near the photo's edge that the window at its place would cut it: the window
    moves off that place only to the nearest window holding it whole, not to the photo's edge.
    Mutation probes: let windows cut faces -> the face cut; aim the window's centre at the face
    -> 0 and 1, the photo's edges."""
    width, height = 4500, 6000
    key = key_content(width, height, faces=(_head(centre, .06),))
    placed, _ = _placed(width, height, key, FIREPLACE)
    assert placed == offset
    nearer = crop_window(PhotoCrop(offset=offset + toward * STEP), width, height, FIREPLACE.aspect)
    assert not all(_inside(one, nearer) for one in faces_judged(key, HOUSE))


def test_a_bigger_face_pulls_harder() -> None:
    """A big face above a small one: their centre, weighted by area, lies near the big face
    (0.393 of the photo, not the midpoint 0.4925), and the window keeps it there.
    Mutation probes: weight the faces equally -> 0.5; aim the window's centre at their centre
    -> 0.28125."""
    width, height = 4500, 6000
    offset, centre = _placed(width, height, key_content(width, height, faces=(
        _head(.36, .16, x=.4), _head(.625, .06, x=.6))), FIREPLACE)
    assert centre == pytest.approx(.3927, abs=1e-4)
    assert offset == 25 / 64


def test_four_faces_of_a_group_keep_their_place() -> None:
    """A real group photo's four faces (6,000 x 4,000) on a 2 : 3 Frame: their weighted centre,
    0.476 of the photo's width, sits at the same share of the window.
    Mutation probes: weight the faces equally -> 0.5; aim the window's centre at their centre
    -> 0.453125, the right face near the window's edge."""
    group = (face(.3472222222222222, .10625, .4023148148148148, .23125),
             face(.44305555555555554, .44027777777777777, .49166666666666664, .53125),
             face(.4583333333333333, .2611111111111111, .5074074074074074, .3611111111111111),
             face(.6449074074074074, .27847222222222223, .6893518518518519, .36180555555555555))
    offset, centre = _placed(6000, 4000, key_content(6000, 4000, faces=group), TWO_BY_THREE)
    assert centre == pytest.approx(.4763, abs=1e-4)
    assert offset == 30 / 64


def test_the_nearest_of_the_windows_that_pass_wins() -> None:
    """Interest in the top and bottom tenths of a 4 : 3 photo on a 16 : 9 Frame passes only the
    windows near either end (two runs of passing offsets); a face just above the middle takes the
    passing window nearest its place, at the top run's end.
    Mutation probe: choose the window keeping the most interest -> 0."""
    width, height = 4000, 3000
    grid = banded_grid(rows=30, top=.1, bottom=.1, weight=.5)
    centred = crop_window(PhotoCrop(offset=.5), width, height, UHD.aspect)
    assert 1 - interest_kept(grid, centred) > HOUSE.max_lost_interest_density * .25
    offset, _ = _placed(width, height, key_content(width, height,
                                                   faces=(face(.45, .44, .55, .54),),
                                                   grid=grid), UHD)
    assert offset == 6 / 64


def test_place_needs_a_window_and_centres_an_equal_shape() -> None:
    """`place` refuses no windows; a photo of the Frame's own shape has one window, offset 0.5."""
    with pytest.raises(ValueError):
        place((), free_axis(4000, 3000, 1), ())
    assert place(((0.0, 1.0), (.5, 1.0), (1.0, 1.0)), free_axis(1600, 900, 16 / 9),
                 (face(0, 0, .1, .1),)) == .5


@pytest.mark.parametrize(("passing", "centre", "offset"), [
    (((.0, 1.0), (.5, .2)), .25, .5),       # offsets 0 and .5: the one nearer 0.5
    (((.75, 1.0), (.25, .2)), .5, .25),     # offsets .75 and .25: the lower offset
])
def test_a_face_midway_between_two_windows_takes_the_central_then_the_lower(passing, centre,
                                                                            offset) -> None:
    """A lone face midway between two passing windows' places, the interest favouring the other:
    the window whose offset is nearer 0.5 wins, then the lower offset.
    Mutation probes: drop the nearest-0.5 tie -> 0; drop the lower-offset tie -> 0.75."""
    lone = face(.45, centre - .05, .55, centre + .05)
    assert place(passing, FreeAxis("y", .5), (lone,)) == offset
