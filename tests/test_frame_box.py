"""The Frame's box and its pixels (frame box run, .claude/runs/frame-box/page.md fb-1): pure."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.frame_fit import FrameBox, Placement, box_size_mm, place_box
from contracts.models import PositionBox, frame_size_mm

SCREENS = [(1, 1), (7, 3), (1920, 1080), (1080, 1920), (3840, 2160)]
STEPS = sorted({round(i * 0.05, 10) for i in range(21)} | {0.0, 1e-6, 1.0})


@pytest.mark.parametrize("screen_width", [1919, 1920, 3840])
def test_boxes_that_share_an_edge_share_its_pixel_column(screen_width: int) -> None:
    first = place_box(FrameBox(left=0, top=0, width=0.5, height=1), (screen_width, 1080))
    second = place_box(FrameBox(left=0.5, top=0, width=0.5, height=1), (screen_width, 1080))
    assert first.x == 0
    assert first.x + first.width == second.x
    assert second.x + second.width == screen_width


def test_three_thirds_tile_a_screen() -> None:
    third = 1 / 3
    placements = [place_box(FrameBox(left=i * third, top=0, width=third, height=1), (1920, 1080))
                  for i in range(3)]
    assert placements[0].x == 0
    for before, after in zip(placements, placements[1:]):
        assert before.x + before.width == after.x
    assert placements[-1].x + placements[-1].width == 1920


@pytest.mark.parametrize("screen", SCREENS)
def test_every_box_places_on_its_screen(screen: tuple[int, int]) -> None:
    screen_width, screen_height = screen
    for left in STEPS:
        for width in STEPS:
            if width == 0 or left + width > 1:
                continue
            box = FrameBox(left=left, top=left, width=width, height=width)
            placement = place_box(box, screen)
            assert 0 <= placement.x and placement.x + placement.width <= screen_width
            assert placement.width >= 1
            assert 0 <= placement.y and placement.y + placement.height <= screen_height
            assert placement.height >= 1


@pytest.mark.parametrize("screen", [(1, 1), (7, 3), (64, 36), (36, 64)])
def test_every_whole_pixel_rectangle_round_trips(screen: tuple[int, int]) -> None:
    screen_width, screen_height = screen
    for x in range(screen_width):
        for width in range(1, screen_width - x + 1):
            for y in range(screen_height):
                for height in range(1, screen_height - y + 1):
                    expected = Placement(x=x, y=y, width=width, height=height)
                    box = FrameBox(left=x / screen_width, top=y / screen_height,
                                   width=width / screen_width, height=height / screen_height)
                    assert place_box(box, screen) == expected


@pytest.mark.parametrize("screen", [(1920, 1080), (1080, 1920), (3840, 2160)])
def test_large_screens_round_trip_on_a_sampled_grid(screen: tuple[int, int]) -> None:
    screen_width, screen_height = screen
    for x in range(0, screen_width, 97):
        for width in (1, 2, 333, screen_width - x):
            if x + width > screen_width:
                continue
            for y, height in ((0, screen_height), (screen_height // 3, 1), (screen_height - 1, 1)):
                expected = Placement(x=x, y=y, width=width, height=height)
                box = FrameBox(left=x / screen_width, top=y / screen_height,
                               width=width / screen_width, height=height / screen_height)
                assert place_box(box, screen) == expected


@pytest.mark.parametrize("fields", [
    {"left": True, "top": 0, "width": 0.5, "height": 0.5},
    {"left": 0, "top": 0, "width": float("nan"), "height": 0.5},
    {"left": -0.1, "top": 0, "width": 0.5, "height": 0.5},
    {"left": 0, "top": 0, "width": 0, "height": 0.5},
    {"left": 0.5, "top": 0, "width": 0.5 + 1e-6, "height": 0.5},
    {"left": 0, "top": 0.5, "width": 0.5, "height": 0.5 + 1e-6},
])
def test_frame_box_refuses_a_box_off_its_screen(fields: dict) -> None:
    with pytest.raises(ValueError, match="frame_box"):
        FrameBox(**fields)


def test_frame_box_accepts_floating_point_edges() -> None:
    box = FrameBox(left=0.7, top=0.7, width=0.3, height=0.3)
    assert place_box(box, (1920, 1080)) == Placement(x=1344, y=756, width=576, height=324)


@pytest.mark.parametrize("screen", [(0, 1080), (1920, True), (1920.0, 1080)])
def test_place_box_refuses_a_screen_that_is_not_whole_pixels(screen: tuple) -> None:
    with pytest.raises(ValueError, match="placement_screen"):
        place_box(FrameBox(left=0, top=0, width=1, height=1), screen)


def test_position_box_defaults_to_the_whole_screen() -> None:
    position = PositionBox()
    assert position.box() == FrameBox(left=0, top=0, width=1, height=1)
    assert place_box(position.box(), (1920, 1080)) == Placement(x=0, y=0, width=1920, height=1080)


def test_position_box_refuses_a_box_past_the_screen() -> None:
    with pytest.raises(ValidationError):
        PositionBox(left=0.6, width=0.5)


def test_frame_size_follows_the_display_as_it_hangs() -> None:
    assert frame_size_mm(PositionBox(width=0.5, height=0.25), (950, 540), 90) == (270.0, 237.5)
    assert frame_size_mm(PositionBox(width=0.5, height=0.25), (950, 540), 0) == (475.0, 135.0)
    assert frame_size_mm(PositionBox(), None, 90) is None


def test_box_size_refuses_a_size_that_is_not_whole_mm() -> None:
    with pytest.raises(ValueError, match="screen_mm"):
        box_size_mm(FrameBox(left=0, top=0, width=1, height=1), (0, 540))
    with pytest.raises(ValueError, match="screen_mm"):
        box_size_mm(FrameBox(left=0, top=0, width=1, height=1), (True, 540))


def test_placement_aspect_is_its_width_over_its_height() -> None:
    assert Placement(0, 0, 480, 540).aspect == 480 / 540
