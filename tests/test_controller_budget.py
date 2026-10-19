"""The display controller's budget: planes, pixels on one output line and enlarged pictures, judged
before every screen update (bench T2: a Pi 5 never refuses an overload, it glitches)."""
from __future__ import annotations

import pytest
from test_executor import layer, media

from contracts.frame_fit import Placement
from player.controller_budget import PI5, ControllerBudget, fits, load
from player.planes import Composition, Plane, Screen
from player.rendering import LocalLayer

WIDE = Screen("hdmi1", (3840, 2160), 0)
FULL = Placement(0, 0, 3840, 2160)


def plane(key: str, at: Placement = FULL, *, frame: str = "frame1", start: float = 100,
          source: tuple[int, int] | None = None) -> Plane:
    """A photo plane at `at` whose source is `source` (default: the placement's own size, shown 1:1)."""
    width, height = source or (at.width, at.height)
    variant = media(key.encode()).model_copy(update={"media_type": "image/jpeg", "width": width, "height": height})
    local = LocalLayer(layer(key, frame=frame, start=start, variant=variant), None, 0.0, 1.0)
    return Plane(frame, local, at, 1.0)


def stacked(count: int, screen: Screen = WIDE, at: Placement = FULL,
            source: tuple[int, int] | None = None) -> Composition:
    return Composition(screen, tuple(plane(f"p{i}", at, start=100 + i, source=source) for i in range(count)))


def test_three_full_width_4k_planes_fill_the_line_and_fit():
    assert load([stacked(3)]) == (4, 15_360, 0)
    assert fits(PI5, [stacked(3)]) is True


def test_a_fourth_full_width_4k_plane_is_over():
    assert load([stacked(4)]) == (5, 19_200, 0)
    assert fits(PI5, [stacked(4)]) is False


def test_only_planes_crossing_one_scanline_add_up():
    top, bottom = Placement(0, 0, 3840, 1080), Placement(0, 1080, 3840, 1080)
    side = Composition(WIDE, (plane("a", top), plane("b", bottom), plane("c", Placement(0, 0, 1000, 2160))))
    assert load([side]) == (4, 3840 + 3840 + 1000, 0)


def test_a_turned_screen_counts_its_pictures_heights_along_the_physical_line():
    turned = Screen("hdmi1", (2160, 3840), 90)
    band = Placement(0, 1313, 2160, 1215)
    assert load([stacked(3, turned, band)]) == (4, 3840 + 3 * 1215, 0)
    beside = Composition(turned, (plane("a", Placement(0, 0, 1080, 3840)),
                                  plane("b", Placement(1080, 0, 1080, 3840))))
    assert load([beside]) == (3, 3840 + 3840, 0)


def test_screens_add_up():
    other = Composition(Screen("hdmi2", (1920, 1080), 0), (plane("q", Placement(0, 0, 1920, 1080)),))
    assert load([stacked(1), other]) == (4, 3840 * 2 + 1920 * 2, 0)
    assert fits(ControllerBudget(planes=3, line_pixels=99_999, enlarged=9), [stacked(1), other]) is False


def test_two_compositions_of_one_screen_are_refused():
    with pytest.raises(ValueError, match="^budget_screens$"):
        load([stacked(1), stacked(1)])


def test_an_enlarged_picture_counts_its_placement_along_the_line_and_as_enlarged():
    """A 1080p picture shown over a full-width 4K placement: the controller works at the placement's
    pixels (bench T2 case B: five 1080p pictures enlarged to full width glitch as five 4K ones do)."""
    assert load([stacked(3, source=(1920, 1080))]) == (4, 15_360, 3)
    assert fits(PI5, [stacked(3, source=(1920, 1080))]) is True


def test_a_fifth_enlarged_picture_is_over_though_the_line_holds_it():
    quarter = Placement(0, 0, 960, 540)
    small = Composition(WIDE, tuple(plane(f"q{i}", Placement(960 * (i % 4), 540 * (i // 4), 960, 540),
                                          start=100 + i, source=(480, 270)) for i in range(5)))
    assert load([small]) == (6, 3840 + 4 * 960, 5)
    assert fits(PI5, [small]) is False
    assert fits(PI5, [Composition(WIDE, small.planes[:4])]) is True
    shown_1_to_1 = Composition(WIDE, tuple(plane(f"r{i}", quarter, start=100 + i) for i in range(5)))
    assert load([shown_1_to_1])[2] == 0
