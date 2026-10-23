"""Where the debug overlay draws (overlay/area.py, issue #124): inside the Frame region Central sent
when it can be met, on the whole Output otherwise, one test per fallback reason. Pure."""
from __future__ import annotations

from appliance.display_host.overlay.area import (
    MIN_AREA,
    FrameArea,
    OutputArea,
    area_rect,
    explain_area,
)
from contracts.node_output import FrameRegion, OutputRect

OUTPUT = (1920, 1080)
RECT = OutputRect(480, 135, 960, 810)


def region(rect: OutputRect = RECT, screen: tuple[int, int] = OUTPUT) -> FrameRegion:
    return FrameRegion("frame-1", screen, rect)


def test_a_region_met_on_the_output_is_the_frame_area():
    area = explain_area(region(), OUTPUT)
    assert area == FrameArea(RECT)
    assert area_rect(area, OUTPUT) == (480, 135, 960, 810)


def test_no_region_is_the_whole_output():
    area = explain_area(None, OUTPUT)
    assert area == OutputArea("no_region")
    assert area_rect(area, OUTPUT) == (0, 0, 1920, 1080)


def test_a_region_placed_on_another_screen_is_the_whole_output():
    """Central placed on 1920 x 1080; the screen hangs turned (or runs another mode) now."""
    assert explain_area(region(), (1080, 1920)) == OutputArea("screen_differs")
    assert explain_area(region(), (3840, 2160)) == OutputArea("screen_differs")


def test_a_region_smaller_than_the_slates_smallest_page_is_the_whole_output():
    narrow = OutputRect(0, 0, MIN_AREA[0] - 1, 600)
    short = OutputRect(0, 0, 900, MIN_AREA[1] - 1)
    assert explain_area(region(narrow), OUTPUT) == OutputArea("too_small")
    assert explain_area(region(short), OUTPUT) == OutputArea("too_small")
    smallest = OutputRect(10, 20, *MIN_AREA)
    assert explain_area(region(smallest), OUTPUT) == FrameArea(smallest)
