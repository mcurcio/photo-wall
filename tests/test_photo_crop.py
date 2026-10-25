"""The crop window (roadmap 5a, run ledger .claude/runs/photo-5a.md, slice R1, cases 1-5)."""
from __future__ import annotations

import math

import pytest

from contracts.frame_fit import FreeAxis, free_axis
from contracts.models import PhotoCrop
from contracts.photo_crop import CENTRED, crop_window


def _close(actual, expected) -> None:
    assert actual == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize(("aspect", "expected"), [
    (1, (.25, 0, .75, 1)),
    (2, (0, 0, 1, 1)),
    (4, (0, .25, 1, .75)),
    (.5, (.375, 0, .625, 1)),
])
def test_centred_is_the_pre_5a_centred_cover(aspect, expected) -> None:
    _close(crop_window(CENTRED, 200, 100, aspect), expected)


def test_a_wide_photo_slides_left_to_right() -> None:
    _close(crop_window(PhotoCrop(offset=0), 200, 100, 1), (0, 0, .5, 1))
    _close(crop_window(PhotoCrop(offset=1), 200, 100, 1), (.5, 0, 1, 1))


def test_a_tall_photo_at_offset_zero_keeps_the_top() -> None:
    _close(crop_window(PhotoCrop(offset=0), 100, 200, 1), (0, 0, 1, .5))
    _close(crop_window(PhotoCrop(offset=1), 100, 200, 1), (0, .5, 1, 1))


@pytest.mark.parametrize("offset", [0, .3, 1])
def test_equal_aspects_have_no_free_axis(offset) -> None:
    assert crop_window(PhotoCrop(offset=offset), 300, 200, 1.5) == (0, 0, 1, 1)


@pytest.mark.parametrize(("width", "height", "aspect", "axis"), [
    (200, 100, 1, FreeAxis("x", .5)),
    (100, 200, 1, FreeAxis("y", .5)),
    (300, 200, 1.5, FreeAxis("y", 1.0)),
])
def test_the_free_axis_is_the_one_the_window_slides_along(width, height, aspect, axis) -> None:
    """The axis follows from the two ratios; the window's centre on it at an offset."""
    assert free_axis(width, height, aspect) == axis
    assert axis.centre(1) == pytest.approx(1 - axis.length / 2)
    left, top, right, bottom = crop_window(PhotoCrop(offset=.3), width, height, aspect)
    start, end = (left, right) if axis.along == "x" else (top, bottom)
    _close((start, end), (axis.start(.3), axis.start(.3) + axis.length))


@pytest.mark.parametrize(("width", "height", "aspect"), [
    (200, 100, 0), (200, 100, -1), (200, 100, math.nan), (200, 100, math.inf),
    (0, 100, 1), (200, 0, 1), (-1, 100, 1),
])
def test_a_bad_aspect_or_size_is_refused(width, height, aspect) -> None:
    with pytest.raises(ValueError):
        crop_window(CENTRED, width, height, aspect)
    with pytest.raises(ValueError):
        free_axis(width, height, aspect)
