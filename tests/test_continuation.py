"""The one generator of a continuation's slots (decision 0032, contracts/continuation.py): what a
Frame shows past its plan's layers, read alike by Central and the Player."""

import pytest
from support.position import LIGHTING

from contracts.continuation import (
    CONTINUATION_PREFIX,
    continuation_assignment_id,
    continuation_layers,
    continuation_source,
)
from contracts.models import Continuation, Layer, OutputBinding, Plan, Variant
from contracts.photo_crop import CENTRED


def _layer(assignment_id, start, end):
    still = Variant(asset_id=f"asset-{assignment_id}", recipe_id="e" * 64, sha256="a" * 64,
                    size=20, media_type="image/jpeg", width=30, height=20)
    return Layer(assignment_id=assignment_id, run_id="run", output_id="hdmi1", frame_id="frame1",
                 binding_generation=1, start=start, end=end, media_origin=start, variant=still,
                 crop=CENTRED, fade_in=1, fade_out=1, after_end="keep_this_photo",
                 awaits_grant=assignment_id == "a")


def _plan(**fields):
    continuation = Continuation(**{"output_id": "hdmi1", "run_id": "run", "photos": ("a", "b", "c"),
                                   "start": 30, "seconds": 10, "lead": 1, "fade_in": 1,
                                   "fade_out": 1, **fields})
    binding = OutputBinding(output_id="hdmi1", frame_id="frame1", generation=1, lighting=LIGHTING,
                            display_mm=None)
    plan = Plan(plan_id="plan", revision=1, player_id="player", authority_epoch=1, issued_at=0,
                valid_from=0, valid_until=60, bindings=(binding,),
                layers=(_layer("a", 0, 11), _layer("b", 10, 21), _layer("c", 20, 30)),
                continuations=(continuation,))
    return plan, continuation


def test_each_slot_is_a_turn_less_its_lead_cycling_the_listed_photos():
    """B2: slot k runs [start + k*seconds - lead, start + (k+1)*seconds) and shows the k-th photo,
    cycling, with its source's variant and crop and no grant."""
    plan, continuation = _plan()
    layers = continuation_layers(plan, continuation, held={"a", "b", "c"}, start=30, end=70)
    assert [(layer.start, layer.end) for layer in layers] == [
        (29, 40), (39, 50), (49, 60), (59, 70), (69, 80)]
    assert [layer.variant.asset_id for layer in layers] == [
        "asset-a", "asset-b", "asset-c", "asset-a", "asset-b"]
    assert all(layer.media_origin == layer.start and not layer.awaits_grant for layer in layers)
    assert all((layer.fade_in, layer.fade_out) == (1, 1) for layer in layers)
    assert all(layer.after_end == "keep_this_photo" and layer.crop == CENTRED for layer in layers)
    assert all(layer.assignment_id.startswith(CONTINUATION_PREFIX) for layer in layers)


def test_a_photo_not_held_is_skipped_and_none_held_gives_nothing():
    plan, continuation = _plan()
    layers = continuation_layers(plan, continuation, held={"a", "c"}, start=30, end=60)
    assert [layer.variant.asset_id for layer in layers] == ["asset-a", "asset-c", "asset-a", "asset-c"]
    assert continuation_layers(plan, continuation, held=set(), start=30, end=60) == ()


def test_until_cuts_the_last_slot_and_its_fades_and_no_slot_starts_at_or_past_it():
    plan, continuation = _plan(until=50.5, fade_in=1, fade_out=1)
    layers = continuation_layers(plan, continuation, held={"a", "b", "c"}, start=0, end=200)
    assert [(layer.start, layer.end) for layer in layers] == [(29, 40), (39, 50), (49, 50.5)]
    assert (layers[-1].fade_in, layers[-1].fade_out) == (1, 0.5)
    short_plan, short = _plan(until=49.5)
    last = continuation_layers(short_plan, short, held={"a"}, start=0, end=200)[-1]
    assert (last.start, last.end, last.fade_in, last.fade_out) == (49, 49.5, 0, 0.5)


def test_slot_ids_are_stable_and_name_their_source():
    """The executor's passes and Central's observation reading name one slot alike."""
    plan, continuation = _plan()
    early = continuation_layers(plan, continuation, held={"a", "b", "c"}, start=30, end=45)
    late = continuation_layers(plan, continuation, held={"a", "b", "c"}, start=40, end=60)
    assert early[1].assignment_id == late[0].assignment_id
    assert early[1].assignment_id == continuation_assignment_id(continuation, 1, "b")
    assert continuation_source(plan, early[1].assignment_id, at=45) == (continuation, 1, plan.layers[1])
    assert continuation_source(plan, "b", at=45) is None
    assert continuation_source(plan, early[1].assignment_id, at=200) is None


def test_the_generator_refuses_a_foreign_continuation_an_empty_window_and_a_negative_slot():
    plan, continuation = _plan()
    _, other = _plan(start=31)
    with pytest.raises(ValueError, match="continuation not in plan"):
        continuation_layers(plan, other, held={"a"}, start=0, end=10)
    with pytest.raises(ValueError, match="empty window"):
        continuation_layers(plan, continuation, held={"a"}, start=10, end=10)
    with pytest.raises(ValueError, match="negative slot"):
        continuation_assignment_id(continuation, -1, "a")
