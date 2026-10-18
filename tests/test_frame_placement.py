"""A Frame's rectangle as whole pixels on its screen as it hangs (contracts/frame_fit.py `place`)."""
from __future__ import annotations

import math

import pytest

from contracts.frame_fit import FrameRectangle, Placement, place

WIDE = 16 / 9


def test_a_full_width_rectangle_fills_a_4k_screen():
    assert place(FrameRectangle(0.5, 0.5, 1.0), (3840, 2160), WIDE) == Placement(0, 0, 3840, 2160)


def test_on_a_turned_screen_the_same_rectangle_rounds_half_up():
    """y = floor(1920 - 607.5 + 0.5) = 1313: half up, never Python's half-to-even."""
    assert place(FrameRectangle(0.5, 0.5, 1.0), (2160, 3840), WIDE) == Placement(0, 1313, 2160, 1215)


def test_a_smaller_rectangle_sits_at_its_centre():
    placed = place(FrameRectangle(0.25, 0.25, 0.5), (3840, 2160), WIDE)
    assert placed == Placement(0, 0, 1920, 1080)
    assert place(FrameRectangle(0.5, 0.5, 0.0001), (3840, 2160), 1.0) == Placement(1920, 1080, 1, 1)


@pytest.mark.parametrize("fields", [
    (-0.01, 0.5, 0.5), (1.01, 0.5, 0.5), (0.5, -0.01, 0.5), (0.5, 1.01, 0.5),
    (0.5, 0.5, 0.0), (0.5, 0.5, 1.01), (math.nan, 0.5, 0.5), (0.5, math.inf, 0.5),
    (True, 0.5, 0.5), (0.5, 0.5, True), ("0.5", 0.5, 0.5), (0.5, None, 0.5),
])
def test_a_rectangle_off_the_unit_square_is_refused(fields):
    with pytest.raises(ValueError, match="^frame_rectangle$"):
        FrameRectangle(*fields)


def test_a_rectangle_that_spills_off_its_screen_is_refused():
    with pytest.raises(ValueError, match="^placement_outside$"):
        place(FrameRectangle(0.1, 0.5, 0.5), (3840, 2160), WIDE)
    with pytest.raises(ValueError, match="^placement_outside$"):   # too tall at this aspect
        place(FrameRectangle(0.5, 0.5, 1.0), (3840, 2160), 1.0)


@pytest.mark.parametrize("screen", [(0, 2160), (3840, 0), (True, 2160), (3840.0, 2160)])
def test_a_screen_without_whole_pixels_is_refused(screen):
    with pytest.raises(ValueError, match="^placement_screen$"):
        place(FrameRectangle(0.5, 0.5, 1.0), screen, WIDE)


@pytest.mark.parametrize("aspect", [0, -1.0, math.nan, math.inf, True, "1"])
def test_a_degenerate_aspect_is_refused(aspect):
    with pytest.raises(ValueError, match="^placement_aspect$"):
        place(FrameRectangle(0.5, 0.5, 1.0), (3840, 2160), aspect)
