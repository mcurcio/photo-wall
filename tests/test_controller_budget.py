"""The display controller's budget: planes and source pixels on one output line, and the cut that
keeps a Pi 5 under them (bench T2: it never refuses an overload, it glitches)."""
from __future__ import annotations

import pytest
from test_executor import layer

from contracts.frame_fit import Placement
from player.controller_budget import PI5, ControllerBudget, fits, load, within
from player.planes import Composition, FrameLayers, Plane, Screen, compose
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


def test_over_budget_the_bottom_planes_give_up_their_place_bottom_first():
    cut, gone = within(PI5, [stacked(5)])
    assert [p.layer.layer.assignment_id for p in cut[0].planes] == ["p2", "p3", "p4"]
    assert gone == ("p0", "p1")
    assert fits(PI5, cut) is True


def test_within_budget_nothing_is_cut():
    assert within(PI5, [stacked(3)]) == ((stacked(3),), ())


def test_the_frame_drawing_the_most_planes_gives_up_first_and_never_its_only_plane():
    half = Placement(0, 0, 1920, 1080)
    busy = Composition(WIDE, (plane("lone", half, frame="a"),
                              plane("b0", half, frame="b", start=100),
                              plane("b1", half, frame="b", start=101),
                              plane("b2", half, frame="b", start=102)))
    cut, gone = within(ControllerBudget(planes=3, line_pixels=99_999), [busy])
    assert gone == ("b0", "b1")
    assert [p.layer.layer.assignment_id for p in cut[0].planes] == ["lone", "b2"]
    cut, gone = within(ControllerBudget(planes=2, line_pixels=99_999), [busy])
    assert gone == ("b0", "b1") and fits(ControllerBudget(planes=2, line_pixels=99_999), cut) is False


def test_the_cut_composes_with_compose():
    frames = [FrameLayers("frame1", FULL, tuple(
        LocalLayer(layer(f"p{i}", start=100 + i), None, 0.0, 0.5 if i else 1.0) for i in range(5)))]
    cut, gone = within(PI5, [compose(WIDE, frames)])
    assert gone == ("p0", "p1") and len(cut[0].planes) == 3
