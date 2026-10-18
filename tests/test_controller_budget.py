"""The display controller's budget: planes and source pixels on one output line, judged before every
screen update (bench T2: a Pi 5 never refuses an overload, it glitches)."""
from __future__ import annotations

import pytest
from test_executor import layer

from contracts.frame_fit import Placement
from player.controller_budget import PI5, ControllerBudget, fits, load
from player.planes import Composition, Plane, Screen
from player.rendering import LocalLayer

WIDE = Screen("hdmi1", (3840, 2160), 0)
FULL = Placement(0, 0, 3840, 2160)


def plane(key: str, at: Placement = FULL, *, frame: str = "frame1", start: float = 100) -> Plane:
    local = LocalLayer(layer(key, frame=frame, start=start), None, 0.0, 1.0)
    return Plane(frame, local, at, 1.0)


def stacked(count: int, screen: Screen = WIDE, at: Placement = FULL) -> Composition:
    return Composition(screen, tuple(plane(f"p{i}", at, start=100 + i) for i in range(count)))


def test_three_full_width_4k_planes_fill_the_line_and_fit():
    assert load([stacked(3)]) == (4, 15_360)
    assert fits(PI5, [stacked(3)]) is True


def test_a_fourth_full_width_4k_plane_is_over():
    assert load([stacked(4)]) == (5, 19_200)
    assert fits(PI5, [stacked(4)]) is False


def test_only_planes_crossing_one_scanline_add_up():
    top, bottom = Placement(0, 0, 3840, 1080), Placement(0, 1080, 3840, 1080)
    side = Composition(WIDE, (plane("a", top), plane("b", bottom), plane("c", Placement(0, 0, 1000, 2160))))
    assert load([side]) == (4, 3840 + 3840 + 1000)


def test_a_turned_screen_counts_its_pictures_heights_along_the_physical_line():
    turned = Screen("hdmi1", (2160, 3840), 90)
    band = Placement(0, 1313, 2160, 1215)
    assert load([stacked(3, turned, band)]) == (4, 3840 + 3 * 1215)
    beside = Composition(turned, (plane("a", Placement(0, 0, 1080, 3840)),
                                  plane("b", Placement(1080, 0, 1080, 3840))))
    assert load([beside]) == (3, 3840 + 3840)


def test_screens_add_up():
    other = Composition(Screen("hdmi2", (1920, 1080), 0), (plane("q", Placement(0, 0, 1920, 1080)),))
    assert load([stacked(1), other]) == (4, 3840 * 2 + 1920 * 2)
    assert fits(ControllerBudget(planes=3, line_pixels=99_999), [stacked(1), other]) is False


def test_two_compositions_of_one_screen_are_refused():
    with pytest.raises(ValueError, match="^budget_screens$"):
        load([stacked(1), stacked(1)])
