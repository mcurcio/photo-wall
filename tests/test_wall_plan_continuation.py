"""Each Frame's continuation lists and what an offer holds (decision 0032,
central/wall_plan/continuation.py): never a photo another Frame uses, past the plan's layers.

Owner, 2026-10-11: "Never show the same photo on two frames at the same time"; 2026-11-15 (Q1,
guidance): a Frame cut off from Central cycles "its own next photos", "never a photo another Frame
is using"."""

import pytest
from support.position import LIGHTING

from central.wall_plan.continuation import (
    ContinuationSegment,
    FrameExtent,
    ListedPhoto,
    continuation_lists,
    held_reservations,
)
from contracts.models import Continuation, Layer, OutputBinding, Plan, Variant
from contracts.on_screen import Extent
from contracts.photo_crop import CENTRED


def segment(frame, photos, *, start=100.0, until=None, player="player", run="run", lead=0.0):
    return ContinuationSegment(
        player_id=player, frame_id=frame, output_id=f"out-{frame}", run_id=run, start=start,
        seconds=10, lead=lead, fade_in=0, fade_out=0, until=until,
        candidates=tuple(ListedPhoto(assignment_id=f"{frame}-{photo}", asset_id=photo)
                         for photo in photos))


def listed(lists, player="player"):
    return {c.output_id: c.photos for c in lists.get(player, ())}


def test_the_lists_of_one_pass_never_share_a_photo():
    """B4: the earlier segment keeps a photo both Frames' offered windows hold. Mutation probe:
    skip the earlier-segment exclusion (both lists name "shared")."""
    lists = continuation_lists(
        [segment("left", ("shared", "a")), segment("right", ("shared", "b"), start=101)],
        extents={}, guard={})
    assert listed(lists) == {"out-left": ("left-shared", "left-a"), "out-right": ("right-b",)}


def test_a_photo_on_another_frame_over_the_span_is_left_out_and_one_before_it_is_not():
    extents = {
        "a": [FrameExtent(frame_id="right", extent=Extent(start=90, end=105))],
        "b": [FrameExtent(frame_id="right", extent=Extent(start=0, end=99.5))],
        "c": [FrameExtent(frame_id="left", extent=Extent(start=0))],     # its own Frame
    }
    lists = continuation_lists([segment("left", ("a", "b", "c"))], extents=extents,
                               guard={"left": 1.0})
    # The guard widens the span to [99, ...): "b" ends at 99.5 on another Frame, so it is out too.
    assert listed(lists) == {"out-left": ("left-c",)}
    lists = continuation_lists([segment("left", ("a", "b", "c"))], extents=extents, guard={})
    assert listed(lists) == {"out-left": ("left-b", "left-c")}


def test_a_segment_with_no_photo_left_gives_no_continuation_and_carries_its_pace():
    extents = {"a": [FrameExtent(frame_id="right", extent=Extent(start=0))]}
    assert continuation_lists([segment("left", ("a",))], extents=extents, guard={}) == {}
    lists = continuation_lists([segment("left", ("b",), until=150, lead=2)], extents={}, guard={})
    assert lists == {"player": (Continuation(output_id="out-left", run_id="run",
                                             photos=("left-b",), start=100, seconds=10, lead=2,
                                             until=150),)}


def test_two_segments_on_one_output_in_turn_are_accepted_and_overlapping_ones_refused():
    first = segment("left", ("a",), until=130, run="show")
    second = segment("left", ("b",), start=130, run="default")
    assert len(continuation_lists([first, second], extents={}, guard={})["player"]) == 2
    with pytest.raises(ValueError, match="segments overlap"):
        continuation_lists([first, segment("left", ("b",), start=131, lead=2, run="default")],
                           extents={}, guard={})
    with pytest.raises(ValueError, match="segments overlap"):
        continuation_lists([segment("left", ("a",)), segment("left", ("b",), start=200)],
                           extents={}, guard={})


def test_an_offer_holds_each_listed_photo_on_its_frame_to_until_with_the_guard():
    still = Variant(asset_id="photo-a", recipe_id="e" * 64, sha256="a" * 64, size=20,
                    media_type="image/jpeg", width=30, height=20)
    layer = Layer(assignment_id="a", run_id="run", output_id="hdmi1", frame_id="frame1",
                  binding_generation=1, start=0, end=10, media_origin=0, variant=still,
                  crop=CENTRED)
    binding = OutputBinding(output_id="hdmi1", frame_id="frame1", generation=1,
                            lighting=LIGHTING, display_mm=None)
    common = dict(plan_id="plan", revision=1, player_id="player", authority_epoch=1, issued_at=0,
                  valid_from=0, valid_until=60, bindings=(binding,), layers=(layer,))
    open_ended = Plan(**common, continuations=(Continuation(
        output_id="hdmi1", run_id="run", photos=("a",), start=10, seconds=10, lead=1),))
    held = held_reservations(open_ended, guard=0.5)
    assert [(r.frame_id, r.asset_id, r.extent) for r in held] == [
        ("frame1", "photo-a", Extent(start=8.5))]
    ending = Plan(**common, continuations=(Continuation(
        output_id="hdmi1", run_id="run", photos=("a",), start=10, seconds=10, until=40),))
    assert held_reservations(ending, guard=0.5)[0].extent == Extent(start=9.5, end=40.5)
    assert held_reservations(Plan(**common), guard=0.5) == ()
