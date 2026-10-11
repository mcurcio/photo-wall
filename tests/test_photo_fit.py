"""The photo fit rule (roadmap 5a design rule 2; run ledger .claude/runs/photo-5a.md, slice R1,
cases 1-9). Frames are 3840 x 2160 at 55 inches unless named."""
from __future__ import annotations

import pytest
from support.key_content_builders import banded_grid, face, measured

from central.catalog import Candidate
from central.wall_plan.fit import (
    DEFAULT_CROP_LIMIT,
    DoesNotFit,
    Fits,
    FitVerdict,
    judge,
    shape_fit,
)
from contracts.models import FrameProfile

UHD = FrameProfile(width_px=3840, height_px=2160, diagonal_inches=55)
UHD_40 = FrameProfile(width_px=3840, height_px=2160, diagonal_inches=40)
NO_VIDEO = FrameProfile(width_px=3840, height_px=2160, diagonal_inches=55, video=False)
TALL = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=32)
NEARLY_SQUARE = FrameProfile(width_px=950, height_px=1000, diagonal_inches=20)


def _judge(candidate: Candidate, profile: FrameProfile = UHD,
           crop_limit: float = DEFAULT_CROP_LIMIT) -> FitVerdict:
    key = candidate.analysis.key if candidate.analysis is not None else None
    return judge(candidate.kind, candidate.original_width, candidate.original_height, key,
                 profile, crop_limit)


def _refused(verdict: FitVerdict) -> str:
    assert isinstance(verdict, DoesNotFit), verdict
    return verdict.reason


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
    assert shape_fit("image", 4000, 4000, UHD, .5) is None
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


def test_the_kept_window_needs_the_frames_pixels() -> None:
    assert isinstance(_judge(measured("p", 4000, 3000)), Fits)
    assert _refused(_judge(measured("p", 3264, 2448))) == "below_frame_pixels"


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
        judge("image", 4000, 3000, None, UHD, DEFAULT_CROP_LIMIT)
    key = measured("p", 4000, 3000).analysis.key
    for limit in (0, 1, -.1, 1.5):
        with pytest.raises(ValueError):
            judge("image", 4000, 3000, key, UHD, limit)


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
    refused = shape_fit(kind, width, height, profile, limit)
    assert refused is not None
    assert judge(kind, width, height, key, profile, limit) == refused
