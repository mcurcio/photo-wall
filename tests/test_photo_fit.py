"""The photo fit rule (roadmap 5a design rule 2; run ledger .claude/runs/photo-5a.md, slice R1,
cases 1-9; follow-ups B1, cases 1-7: the Frame output and every tuning value a setting). Frames
are 3840 x 2160 at 55 inches, full screen on a 3840 x 2160 Display, unless named."""
from __future__ import annotations

import pytest
from support.key_content_builders import banded_grid, face, key_content, measured

from central.catalog import Candidate
from central.wall_plan.fit import (
    DoesNotFit,
    Fits,
    FitTarget,
    FitVerdict,
    band,
    crop_share,
    interest_kept,
    judge,
    shape_fit,
)
from central.wall_plan.fit_settings import FitSettings
from contracts.models import Calibration, CoverFit, FrameProfile, frame_output

UHD = FrameProfile(width_px=3840, height_px=2160, diagonal_inches=55)
UHD_40 = FrameProfile(width_px=3840, height_px=2160, diagonal_inches=40)
NO_VIDEO = FrameProfile(width_px=3840, height_px=2160, diagonal_inches=55, video=False)
TALL = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=32)
NEARLY_SQUARE = FrameProfile(width_px=950, height_px=1000, diagonal_inches=20)
FOUR_THREE = FrameProfile(width_px=1024, height_px=768, diagonal_inches=10)
DISPLAY = (3840, 2160)
HOUSE = FitSettings()
LIMIT = HOUSE.crop_limit


def full_screen(profile: FrameProfile) -> FitTarget:
    """A Frame filling a Display of its profile's own size: its Frame output is the profile's."""
    return FitTarget(profile=profile, output_px=(profile.width_px, profile.height_px))


def _judge(candidate: Candidate, target: FitTarget | FrameProfile = UHD,
           crop_limit: float = LIMIT, settings: FitSettings = HOUSE) -> FitVerdict:
    if isinstance(target, FrameProfile):
        target = full_screen(target)
    key = candidate.analysis.key if candidate.analysis is not None else None
    return judge(candidate.kind, candidate.original_width, candidate.original_height, key,
                 target, crop_limit, settings)


def _refused(verdict: FitVerdict) -> str:
    assert isinstance(verdict, DoesNotFit), verdict
    return verdict.reason


def _opening(corners) -> FitTarget:
    """UHD's Frame drawn through `corners` of the 3840 x 2160 Display (Cover fit)."""
    calibration = Calibration(fit=CoverFit(corners=corners))
    return FitTarget(profile=UHD, output_px=frame_output(UHD, calibration, DISPLAY))


MAT = _opening(((.3, .3), (.7, .3), (.7, .7), (.3, .7)))     # a 40 % x 40 % opening


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
    assert shape_fit("image", 4000, 4000, full_screen(UHD), .5, HOUSE) is None
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
    """B1 case 1: a 40 % x 40 % Cover opening on a 3,840 x 2,160 Display is a 1,536 x 864
    Frame output; a 1,600 x 900 photo fills it, and judged against the Display it would not."""
    assert MAT.output_px == (1536, 864)
    photo = measured("p", 1600, 900)
    assert isinstance(_judge(photo, MAT), Fits)
    assert _refused(_judge(photo, FitTarget(profile=UHD, output_px=DISPLAY))) == \
        "below_frame_pixels"


def test_a_frame_turned_off_axis_needs_more_than_the_display() -> None:
    """B1 case 2: full screen turned 10 degrees, the picture is larger than the Display, so a
    photo of exactly the Display's size is too small."""
    turned = Calibration(fit=CoverFit(rotation=10))
    target = FitTarget(profile=UHD, output_px=frame_output(UHD, turned, DISPLAY))
    assert target.output_px[0] > 3840 and target.output_px[1] > 2160
    assert _refused(_judge(measured("p", 3840, 2160), target)) == "below_frame_pixels"
    assert isinstance(_judge(measured("p", 3840, 2160)), Fits)


def test_a_frame_without_a_frame_output_is_judged_by_shape_only() -> None:
    """B1 case 3: no Output size reported, rule 2 is skipped (the analysis says so)."""
    unreported = FitTarget(profile=FOUR_THREE, output_px=None)
    assert shape_fit("image", 400, 300, unreported, LIMIT, HOUSE) is None
    assert isinstance(_judge(measured("p", 400, 300), unreported), Fits)
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
    unreported = FitTarget(profile=UHD, output_px=None)
    for width, height, edge in ((5200, 4500, low), (3200, 1170, high)):   # 52:45, 320:117
        assert width / height == pytest.approx(edge)
        assert crop_share(width, height, UHD) == pytest.approx(.35)
        assert shape_fit("image", width, height, unreported, .35, HOUSE) is None
        assert shape_fit("image", width, height, unreported, .349, HOUSE) == \
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
        judge("image", 4000, 3000, None, full_screen(UHD), LIMIT, HOUSE)
    key = measured("p", 4000, 3000).analysis.key
    for limit in (0, 1, -.1, 1.5):
        with pytest.raises(ValueError):
            judge("image", 4000, 3000, key, full_screen(UHD), limit, HOUSE)


@pytest.mark.parametrize(("kind", "width", "height", "profile", "limit"), [
    ("image", 2000, 3000, UHD, .35),
    ("image", 4000, 4000, UHD, .35),
    ("image", 3264, 2448, UHD, .35),
    ("video", 1920, 1080, NO_VIDEO, .35),
    ("video", 1280, 720, UHD_40, .35),
])
def test_shape_fit_agrees_with_judge_on_every_size_only_refusal(kind, width, height, profile,
                                                                 limit) -> None:
    key = measured("p", width, height).analysis.key if kind == "image" else None
    refused = shape_fit(kind, width, height, full_screen(profile), limit, HOUSE)
    assert refused is not None
    assert judge(kind, width, height, key, full_screen(profile), limit, HOUSE) == refused


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
