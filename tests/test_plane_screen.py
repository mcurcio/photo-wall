"""The Player's screens' pure rules (player/plane_screen.py; render redesign, run ledger
.claude/runs/render-planes/s1-page.md, bead B3): the strength's wire value, and a present past the
controller's planes refused before the frame client library is asked anything. The screens
themselves are proved under a real Weston by tests/native_screen_harness.py."""
from __future__ import annotations

import math
from uuid import uuid4

import pytest

import player.plane_screen as plane_screen
from contracts.frame_fit import Placement
from contracts.models import Layer
from player.plane_screen import FrameGrant
from player.planes import Composition, Plane, Screen
from player.rendering import LocalLayer


@pytest.mark.parametrize(("strength", "wire"), [
    (1.0, 0xFFFFFFFF),
    (0.5, 0x80000000),              # round(2147483647.5): half to even
    (1 / 255, 0x01010101),
])
def test_multiplier_is_the_strength_on_the_alpha_modifiers_scale(strength, wire):
    assert plane_screen.multiplier(strength) == wire


@pytest.mark.parametrize("strength", [0.0, -0.1, 1.0000001, math.nan, math.inf, -math.inf])
def test_multiplier_refuses_a_strength_outside_zero_to_one(strength):
    with pytest.raises(ValueError, match="plane_strength"):
        plane_screen.multiplier(strength)


class _Library:
    """A frame client library that records every call; connecting succeeds."""

    def __init__(self, _path: str) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        library = self

        class Function:
            argtypes = restype = None

            def __call__(self, *_args):
                library.calls.append(name)
                return 1

        function = Function()
        setattr(self, name, function)
        return function


def _plane(index: int) -> Plane:
    layer = Layer(assignment_id=f"black-{index}", run_id="run", output_id="HDMI-A-1", frame_id="frame",
                  binding_generation=1, start=0, end=10**9, media_origin=0, presentation="black")
    return Plane("frame", LocalLayer(layer, None, 0.0, 1.0), Placement(0, 0, 64, 48), 1.0)


def test_present_refuses_past_the_screens_planes_before_asking_the_library(monkeypatch):
    monkeypatch.setattr(plane_screen.ctypes, "CDLL", _Library)
    screens = plane_screen.PlaneScreens("libfake.so")
    library = screens._lib
    before = list(library.calls)
    composition = Composition(Screen("HDMI-A-1", (640, 480), 0),
                              tuple(_plane(index) for index in range(plane_screen.SCREEN_PLANES + 1)))
    grant = FrameGrant(uuid4(), 1, 1, "frame", True)
    with pytest.raises(ValueError, match="present_planes"):
        screens.present(composition, {}, grant, "tag", None)
    assert library.calls == before


def test_present_refuses_a_ground_not_laid_out_for_the_whole_screen_before_asking_the_library(monkeypatch):
    """The screen's ground is the whole screen's picture as it is laid out (turned with the
    screen): any other size, or a picture the screens never imported, is refused untouched."""
    monkeypatch.setattr(plane_screen.ctypes, "CDLL", _Library)
    screens = plane_screen.PlaneScreens("libfake.so")
    library = screens._lib
    screens._pictures.add(7)
    grant = FrameGrant(uuid4(), 1, 1, "frame", True)
    turned = Composition(Screen("HDMI-A-1", (480, 640), 90), ())
    before = list(library.calls)
    for ground in (plane_screen.ScreenPicture(7, (480, 640)), plane_screen.ScreenPicture(8, (640, 480))):
        assert screens.present(turned, {}, grant, "tag", None, ground) == "picture_refused"
    assert library.calls == before
