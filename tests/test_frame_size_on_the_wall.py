"""A Frame's size on the wall, worked out, never typed (frame box run, .claude/runs/frame-box/page.md,
bead fb-14; owner, 2026-10-19: "diagonal inches is a weird concept when we already have width and
height").

The inventory's `size_mm` is `contracts.models.frame_size_mm` of the Frame's Position box and the
size of the Display seen on its Output, as its screen hangs: the wall plan draws each Frame at it.
A Frame bound to nothing, or on a Display that states no size, has none. Reports travel the real
path (the display line's Output report, judged in Central's link store) and reads the operator API.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from support.position import save_position
from test_display_identity import ADMIN, AUTH, _bind, _frame, _generation, enroll_pi
from test_display_size import _sized

from central.app import create_app
from contracts.models import Calibration, PositionBox

QN43 = (950, 540)   # the owner's Samsung's picture area, from its EDID base block


def _size_mm(registry, frame_id: str) -> list[float] | None:
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        frames = client.get("/v1/operator/inventory", headers=AUTH).json()["frames"]
    return next(frame for frame in frames if frame["id"] == frame_id)["size_mm"]


def test_a_bound_frame_is_its_displays_size_and_an_unbound_one_has_none(registry):
    pi = enroll_pi(registry, "pi-a")
    hall = _frame(registry, "hall")
    assert _size_mm(registry, hall) is None                  # bound to nothing: not known
    _bind(registry, hall, pi, "HDMI-A-1")
    _sized(registry, "pi-a", "HDMI-A-1", None)
    assert _size_mm(registry, hall) is None                  # the Display states no size
    _sized(registry, "pi-a", "HDMI-A-1", QN43)
    assert _size_mm(registry, hall) == [950, 540]            # the whole-screen box: the whole picture


def test_a_frame_is_its_boxs_share_of_the_display(registry):
    pi = enroll_pi(registry, "pi-a")
    hall = _frame(registry, "hall")
    _bind(registry, hall, pi, "HDMI-A-1")
    _sized(registry, "pi-a", "HDMI-A-1", QN43)
    save_position(registry, hall, Calibration(fit=PositionBox(left=0.25, top=0.5, width=0.5, height=0.25)))
    assert _size_mm(registry, hall) == [475, 135]


def test_a_turned_frame_is_its_display_as_it_hangs(registry):
    """Mutation probe: answer size_mm without the turn -> the turned Frame reads 950 x 540."""
    pi = enroll_pi(registry, "pi-a")
    hall = _frame(registry, "hall")
    _bind(registry, hall, pi, "HDMI-A-1")
    _sized(registry, "pi-a", "HDMI-A-1", QN43)
    registry.turn_frame(hall, 90, expected_generation=_generation(registry, hall))
    assert _size_mm(registry, hall) == [540, 950]
