"""The cover rectangle and the corner rule (roadmap 1c, slice T0; contracts/frame_fit.py; the
rectangle has the Frame profile's aspect at every angle, owner 2026-10-11).

The vectors in tests/support/frame_fit_vectors.json are the one set of test vectors for the corner
rule and the cover rectangle: the console's convex.js check reads them too.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from contracts.frame_fit import FULL_SCREEN, CoverRectangle, convex_clockwise, cover_rectangle

VECTORS = json.loads((Path(__file__).parent / "support" / "frame_fit_vectors.json").read_text())
OUTPUT = tuple(VECTORS["output"])
TOLERANCE = 1e-9
ASYMMETRIC = ((0.1, 0.1), (0.9, 0.05), (0.8, 0.95), (0.2, 0.8))


def _corners(points) -> tuple:
    return tuple(tuple(point) for point in points)


def _in_picture_axes(rectangle: CoverRectangle, x: float, y: float) -> tuple[float, float]:
    """(u, v) of an output pixel relative to the rectangle's centre, along the picture's axes."""
    turn = math.radians(rectangle.rotation)
    dx, dy = x - rectangle.centre_x, y - rectangle.centre_y
    return dx * math.cos(turn) + dy * math.sin(turn), -dx * math.sin(turn) + dy * math.cos(turn)


@pytest.mark.parametrize("corners", VECTORS["convex"])
def test_convex_vectors_pass_the_corner_rule(corners):
    assert convex_clockwise(corners)


@pytest.mark.parametrize("corners", VECTORS["folded"])
def test_folded_vectors_fail_the_corner_rule_and_have_no_cover(corners):
    assert not convex_clockwise(corners)
    with pytest.raises(ValueError, match="^cover_corners$"):
        cover_rectangle(_corners(corners), 0, OUTPUT, 16 / 9)


def _aspect(case) -> float:
    return case["profile"][0] / case["profile"][1]


@pytest.mark.parametrize("case", VECTORS["cover"], ids=[case["name"] for case in VECTORS["cover"]])
def test_the_cover_rectangle_matches_the_shared_vectors(case):
    rectangle = cover_rectangle(_corners(case["corners"]), case["rotation"], OUTPUT, _aspect(case))
    assert rectangle.centre_x == pytest.approx(case["centre"][0], abs=1e-6)
    assert rectangle.centre_y == pytest.approx(case["centre"][1], abs=1e-6)
    assert rectangle.width == pytest.approx(case["width"], abs=1e-6)
    assert rectangle.height == pytest.approx(case["height"], abs=1e-6)
    assert rectangle.rotation == case["rotation"]


@pytest.mark.parametrize("profile", [(1920, 1080), (1080, 1920), (1000, 1000)])
@pytest.mark.parametrize("case", VECTORS["cover"], ids=[case["name"] for case in VECTORS["cover"]])
def test_every_corner_lies_inside_the_smallest_rectangle_of_the_profiles_aspect(case, profile):
    """Owner 2026-10-11: the angle is how the Frame is hung, not its aspect. The rectangle has the
    profile's width-to-height at every angle, holds every corner, and is tight on one pair of
    opposite sides (the other pair has room to spare when the aspect, not the corners, sets it)."""
    aspect = profile[0] / profile[1]
    rectangle = cover_rectangle(_corners(case["corners"]), case["rotation"], OUTPUT, aspect)
    assert rectangle.width / rectangle.height == pytest.approx(aspect, rel=1e-12)
    half_width, half_height = rectangle.width / 2, rectangle.height / 2
    spans_u, spans_v = [], []
    for x, y in case["corners"]:
        u, v = _in_picture_axes(rectangle, x * OUTPUT[0], y * OUTPUT[1])
        assert abs(u) <= half_width + TOLERANCE and abs(v) <= half_height + TOLERANCE
        spans_u.append(u)
        spans_v.append(v)
    # Centred on both spans' midpoints, so the binding pair touches on both sides.
    assert min(spans_u) + max(spans_u) == pytest.approx(0, abs=1e-6)
    assert min(spans_v) + max(spans_v) == pytest.approx(0, abs=1e-6)
    assert (max(spans_u) == pytest.approx(half_width, abs=1e-6)
            or max(spans_v) == pytest.approx(half_height, abs=1e-6))


def test_at_0_degrees_an_opening_of_the_profiles_shape_is_its_own_cover():
    opening = ((0.25, 0.1), (0.75, 0.1), (0.75, 0.6), (0.25, 0.6))     # 960 x 540 pixels
    assert cover_rectangle(opening, 0, OUTPUT, 16 / 9) == CoverRectangle(960.0, 378.0, 960.0, 540.0, 0.0)


def test_at_90_degrees_a_landscape_frame_stays_landscape():
    """A 16:9 Frame turned a quarter on a full 1920 x 1080 screen: the corners span 1080 along the
    picture's width and 1920 along its height, so the height is max(1080 / (16/9), 1920) = 1920
    and the width 16/9 of it. A 9:16 Frame (a display hung on its side) fits exactly."""
    landscape = cover_rectangle(FULL_SCREEN, 90, (1920, 1080), 16 / 9)
    assert (landscape.width, landscape.height) == pytest.approx((1920 * 16 / 9, 1920))
    assert (landscape.centre_x, landscape.centre_y) == pytest.approx((960, 540))
    portrait = cover_rectangle(FULL_SCREEN, 90, (1920, 1080), 9 / 16)
    assert (portrait.width, portrait.height) == pytest.approx((1080, 1920))


def test_the_centre_is_the_spreads_midpoint_not_the_corners_average():
    """Mutation probe: centre on the corners' average -> this and the inside cases fail."""
    rectangle = cover_rectangle(ASYMMETRIC, 0, OUTPUT, 16 / 9)
    average = (sum(x for x, _ in ASYMMETRIC) / 4 * OUTPUT[0], sum(y for _, y in ASYMMETRIC) / 4 * OUTPUT[1])
    assert (rectangle.centre_x, rectangle.centre_y) == pytest.approx((0.5 * 1920, 0.5 * 1080))
    assert (rectangle.centre_x, rectangle.centre_y) != pytest.approx(average, abs=1)


def test_the_rectangle_is_worked_in_pixels_not_fractions():
    in_pixels = cover_rectangle(ASYMMETRIC, 7, OUTPUT, 16 / 9)
    in_fractions = cover_rectangle(ASYMMETRIC, 7, (1, 1), 16 / 9)   # the same corners on a square output
    scaled = (in_fractions.width * OUTPUT[0], in_fractions.height * OUTPUT[1])
    assert (in_pixels.width, in_pixels.height) != pytest.approx(scaled, abs=1)


@pytest.mark.parametrize("rotation", [-1, 360, float("nan"), float("inf"), True])
def test_a_rotation_outside_0_to_360_refuses(rotation):
    with pytest.raises(ValueError, match="^cover_rotation$"):
        cover_rectangle(FULL_SCREEN, rotation, OUTPUT, 16 / 9)


@pytest.mark.parametrize("output", [(0, 1080), (1920, 0), (1920.0, 1080)])
def test_an_empty_output_refuses(output):
    with pytest.raises(ValueError, match="^cover_output$"):
        cover_rectangle(FULL_SCREEN, 0, output, 16 / 9)


@pytest.mark.parametrize("aspect", [0, -1, float("nan"), float("inf"), True])
def test_an_aspect_that_is_not_a_positive_number_refuses(aspect):
    with pytest.raises(ValueError, match="^cover_aspect$"):
        cover_rectangle(FULL_SCREEN, 0, OUTPUT, aspect)
