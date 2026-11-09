"""Why a Frame is black while content is wanted: the Player's one rule (player/frame_black.py)."""
from dataclasses import replace

import pytest
from test_plane_renderer import OUTPUT, Rig, photo

from contracts.node_app_link import FrameBlackCause
from contracts.node_frame import neutral_operations
from contracts.node_picture import DisplayLight
from contracts.tuning import TestPattern
from player.frame_black import DueLayer, frame_black


def layer(order=1, *, presentation="media", secured=True, prepared=True, granted=False, failure=None):
    return DueLayer(presentation, (order,), secured=secured, prepared=prepared, granted=granted,
                    failure=failure)


def judged(*due, showing=False, own_drawing=False, has_plan=True, clock_ok=True, unfilled=False):
    return frame_black(showing=showing, own_drawing=own_drawing, has_plan=has_plan,
                       clock_ok=clock_ok, due=due, unfilled=unfilled)


@pytest.mark.parametrize("kept_black", [
    {"showing": True},          # a kept photo drawn as fallback, a layer drawn at 0 lx
    {"own_drawing": True},      # a test pattern or calibration target
])
def test_a_frame_showing_or_drawing_its_own_says_nothing(kept_black):
    """Mutation probes: ignore `showing`; ignore `own_drawing`."""
    assert judged(layer(), has_plan=False, clock_ok=False, unfilled=True, **kept_black) is None


def test_no_current_plan_outranks_everything_a_plan_would_say():
    assert judged(has_plan=False) is FrameBlackCause.NO_PLAN


def test_nothing_due_is_silent_unless_a_photo_was_wanted():
    """Mutation probe: no due layer treated as NO_CONTENT without a span."""
    assert judged(unfilled=False) is None
    assert judged(unfilled=True) is FrameBlackCause.NO_CONTENT


def test_the_topmost_due_layer_decides_and_a_black_layer_is_deliberate():
    """Mutation probe: a black layer counts as content due."""
    assert judged(layer(2, presentation="black"), layer(1, secured=False)) is None
    assert judged(layer(1, presentation="black"), layer(2, secured=False)) is FrameBlackCause.NO_PHOTO


@pytest.mark.parametrize("due,clock_ok,expected", [
    (layer(), False, FrameBlackCause.NO_TIME),
    (layer(secured=False), True, FrameBlackCause.NO_PHOTO),
    (layer(failure="download"), True, FrameBlackCause.NO_PHOTO),
    (layer(failure="integrity"), True, FrameBlackCause.NO_PHOTO),
    (layer(failure="decode"), True, FrameBlackCause.CANNOT_DRAW),
    (layer(failure="capacity"), True, FrameBlackCause.CANNOT_DRAW),
    (layer(prepared=False), True, FrameBlackCause.CANNOT_DRAW),
    (layer(), True, FrameBlackCause.NOT_STARTED),
    (layer(granted=True), True, FrameBlackCause.CANNOT_DRAW),
])
def test_each_cause_of_a_due_photo(due, clock_ok, expected):
    assert judged(due, clock_ok=clock_ok) is expected


def test_the_plane_renderer_says_when_its_last_commit_drew_a_pattern():
    """`patterned` reads the same `_patterned` rule the pattern ground is drawn by."""
    rig = Rig()
    assert rig.renderer.patterned(OUTPUT) is False               # nothing presented
    still = photo("still", start=0)
    rig.settle(still)
    assert rig.renderer.patterned(OUTPUT) is False
    white = replace(neutral_operations(OUTPUT, 1, DisplayLight(200, .01)), test_pattern=TestPattern.WHITE)
    rig.screens.operations = white
    rig.clock.tick()
    rig.settle(still)
    assert rig.renderer.patterned(OUTPUT) is True
    rig.screens.operations = None
    rig.clock.tick()
    rig.settle(still)
    assert rig.renderer.patterned(OUTPUT) is False
