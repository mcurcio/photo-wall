"""The display harness's app (tests/native_screen_harness.py, run only in the node-pid1 display
harness container) answers one JSON line per command: its replies must encode, and each harness
picture must be its own picture, now that a plane's key is what it draws (player/planes.py
`drawn_identity`, a tuple that never leaves the Player's process). Held here, where the harness's
container is not needed."""
from __future__ import annotations

import importlib
import json
import sys

import pytest

from contracts.frame_fit import Placement


@pytest.fixture
def harness():
    """The harness module, imported with sys.path as it was (it points sys.path at its container's
    mounts on import)."""
    kept = list(sys.path)
    try:
        yield importlib.import_module("native_screen_harness")
    finally:
        sys.path[:] = kept


def test_two_pictures_of_one_placement_are_two_pictures_and_answer_in_json(harness):
    at = Placement(40, 40, 200, 150)
    red, green = harness.harness_plane("red", at, 1.0), harness.harness_plane("green", at, 0.5)
    assert red.key != green.key
    assert harness.harness_plane("red", at, 0.25).key == red.key
    assert red.size == (200, 150)
    shown = [("red", red, "picture-red"), ("green", green, "picture-green")]
    reply = {"result": 3, "busy": harness.busy_reply(shown, lambda picture: picture == "picture-red")}
    assert json.loads(json.dumps(reply)) == {"result": 3, "busy": {"red": True, "green": False}}


def test_a_picture_with_a_source_is_finished_at_its_source_size(harness):
    plane = harness.harness_plane("quadrants", Placement(0, 0, 200, 150), 1.0, (100, 75))
    assert plane.size == (100, 75) and plane.enlarged
