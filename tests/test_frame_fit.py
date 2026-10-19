"""The Frame output's one test, `window_holds` (contracts/frame_fit.py; follow-ups 2026-10-11, slice
A1; owner: compatibility "measuring against the Frame output and not the Display"). `place`'s
cases are tests/test_frame_placement.py's.
"""
from __future__ import annotations

import pytest

from contracts.frame_fit import window_holds


@pytest.mark.parametrize(("need", "holds"), [
    ((3840, 2160), True),
    ((4032, 2268), True),     # exactly the largest 16:9 window of a 4,032 x 3,024 photo
    ((4033, 2268), False),
    ((4100, 2000), False),    # a smaller area, but a wider side: both axes must hold
    ((2268, 4032), False),    # the same pixels the other way round
])
def test_a_photo_holds_the_frame_output_only_when_its_window_holds_both_sides(need, holds):
    """(5) Mutation probe: compare areas instead of both axes -> (4100, 2000) holds and this fails."""
    assert window_holds(4032, 3024, 16 / 9, need) is holds


def test_a_wide_photo_is_cut_along_its_height():
    """A 4:1 panorama's 16:9 window spans its whole height."""
    assert window_holds(8000, 2000, 16 / 9, (3555, 2000)) is True
    assert window_holds(8000, 2000, 16 / 9, (3556, 2001)) is False


@pytest.mark.parametrize(("width", "height", "aspect", "need"), [
    (0, 3024, 16 / 9, (1, 1)), (4032, -1, 16 / 9, (1, 1)), (4032, 3024, 0, (1, 1)),
    (4032, 3024, float("nan"), (1, 1)), (4032, 3024, 16 / 9, (0, 1)), (4032, 3024, 16 / 9, (1, 0)),
    (4032.0, 3024, 16 / 9, (1, 1)),
])
def test_a_size_that_is_not_positive_refuses(width, height, aspect, need):
    with pytest.raises(ValueError, match="^window_size$"):
        window_holds(width, height, aspect, need)
