"""The everyday actions' rules (decision 0036, slice 3), pure: who moves and who stays, the cover a
Skip or a Previous adds, and an asked action's state."""
from __future__ import annotations

import hashlib

import pytest

from central.everyday.model import ACK_LATE_SECONDS, AskState, CoverCause, SkipKind, StayReason
from central.everyday.policy import (
    asked_state,
    cover_for,
    joins,
    photo_of,
    previous_step,
    showing_at,
    stays,
)
from central.everyday.ports import FrameFacts, LayerOrigin, ShownPhoto
from contracts.models import Layer, Variant, stack_order
from contracts.photo_crop import CENTRED
from player.rendering import LocalLayer
from player.visibility import visible_now


def variant(asset: str, media_type: str = "image/jpeg") -> Variant:
    return Variant(asset_id=asset, recipe_id="e" * 64, sha256=hashlib.sha256(asset.encode()).hexdigest(),
                   size=10, media_type=media_type, width=1920, height=1080)


def layer(key: str, start: float, end: float, *, asset: str | None = None, run: str = "run",
          black: bool = False, **fields) -> Layer:
    media = {} if black else {"variant": variant(asset or key), "crop": CENTRED}
    return Layer(**{**dict(assignment_id=key, run_id=run, output_id="out", frame_id="f",
                            binding_generation=1, start=start, end=end, media_origin=start,
                            presentation="black" if black else "media"), **media, **fields})


TURNS = (layer("a", 0, 30, asset="photo-a"), layer("b", 30, 60, asset="photo-b"),
         layer("c", 60, 90, asset="photo-c"))


def facts(**fields) -> FrameFacts:
    base = dict(frame_id="f", layers=TURNS,
                origins={each.assignment_id: LayerOrigin(scene_id="s", transition=2) for each in TURNS})
    base.update(fields)
    return FrameFacts(**base)


# Each row of the StayReason table, with the facts that make it and every earlier row false.
ROWS = (
    (StayReason.NOT_SET_UP, dict(set_up=False)),
    (StayReason.NOT_ANSWERING, dict(answering=False)),
    (StayReason.RESTING, dict(resting=True)),
    (StayReason.PAUSED, dict(paused=True)),
    (StayReason.NOTHING_TO_SKIP, dict(layers=())),
    (StayReason.BLACK, dict(layers=(layer("dark", 0, 30, black=True),))),
    (StayReason.HAND_PLACED, dict(origins={"a": LayerOrigin(scene_id="s", hand_placed=True)})),
    (StayReason.NO_PREVIOUS, dict(history=())),
)


@pytest.mark.parametrize("index", range(len(ROWS)))
def test_each_stay_reason_in_order(index):
    """Each row matches on its own facts, and wins over every later row also true (precedence).
    Mutation probe: swap two rows of `stays`."""
    reason, own = ROWS[index]
    later = {}
    for _, more in ROWS[index + 1:]:
        later.update(more)
    kind = SkipKind.PREVIOUS
    assert stays(facts(**own), kind, 10) == reason
    assert stays(facts(**{**later, **own}), kind, 10) == reason


def test_an_ending_is_dark_on_purpose_and_a_frame_with_history_moves():
    ending = {"a": LayerOrigin(scene_id="s", ending=True)}
    assert stays(facts(origins=ending), SkipKind.NEXT, 10) is StayReason.BLACK
    shown = (ShownPhoto("photo-a", "a", 5), ShownPhoto("photo-z", "z", -30))
    assert stays(facts(history=shown), SkipKind.PREVIOUS, 10) is None
    assert stays(facts(), SkipKind.NEXT, 10) is None


@pytest.mark.parametrize("at", [0, 5, 29.5, 30, 31, 31.9, 32, 45, 89.9])
@pytest.mark.parametrize("fade", [0, 2])
@pytest.mark.parametrize("png", [False, True])
def test_showing_at_agrees_with_the_players_covered_rule(at, fade, png):
    """`showing_at` is the topmost layer the Player's covered rule draws opaque (player/visibility.py
    `visible_now`), over a cross-fade, a cover above its Run and a PNG that is never opaque.
    Mutation probe: take the topmost drawn layer whatever its alpha."""
    cover = layer("cover", 31, 62, asset="photo-c", action=True, fade_in=fade)
    stack = (layer("a", 0, 32, asset="photo-a"),
             layer("b", 30, 60, asset="photo-b", fade_in=fade,
                   variant=variant("photo-b", "image/png" if png else "image/jpeg")),
             cover, layer("c", 60, 90, asset="photo-c"))
    drawn = [LocalLayer(each, None, 0.0, each.alpha(at)) for each in sorted(stack, key=stack_order)
             if each.start <= at < each.end]
    visible = visible_now(tuple(drawn))
    opaque = [local.layer for local in visible if local.alpha >= 1 and not (
        local.layer.variant and local.layer.variant.media_type == "image/png")]
    expected = opaque[-1] if opaque else (visible[-1].layer if visible else None)
    assert showing_at(stack, at) == expected


def test_skip_joins_the_frames_next_regular_photo():
    """Next: the cover shows the Run's next photo from the shared instant until its own layer is
    fully in, above it, with its Scene's Transition as its fade-in; it skips an ending, the photo
    left, a listed photo and another Run's layer. Mutation probe: join the first later layer of
    any Run."""
    other = layer("other", 20, 50, asset="photo-o", run="another")
    found = facts(layers=(*TURNS, other))
    assert joins(found, TURNS[0], 10) == TURNS[1]
    assert joins(facts(listed=frozenset({"photo-b"})), TURNS[0], 10) == TURNS[2]
    ending = {**found.origins, "b": LayerOrigin(scene_id="s", ending=True)}
    assert joins(facts(origins=ending), TURNS[0], 10) == TURNS[2]
    cover = cover_for(facts(), SkipKind.NEXT, CoverCause.NEXT, request_id="r", at=10, now=8, cue=None)
    assert cover.how == "joins" and cover.joined == "b"
    assert (cover.layer.start, cover.layer.end, cover.layer.fade_in) == (10, 30, 2)
    assert cover.layer.action and photo_of(cover.layer) == "photo-b" and not cover.layer.awaits_grant
    assert stack_order(cover.layer) > stack_order(TURNS[1])
    wall = cover_for(facts(), SkipKind.NEXT, CoverCause.NEXT, request_id="r", at=10, now=8, cue="skip-r")
    assert wall.layer.awaits_grant
    alone = cover_for(facts(layers=TURNS[:1]), SkipKind.NEXT, CoverCause.NEXT, request_id="r", at=10,
                      now=8, cue=None)
    assert alone.how == "pick" and alone.left == "photo-a" and alone.end == 30


HISTORY = tuple(ShownPhoto(asset, f"shown-{n}", -10.0 * n)
                for n, asset in enumerate(["photo-a", "photo-a", "photo-x", "photo-x", "photo-y", "photo-x",
                                           "photo-z"]))


def test_previous_steps_back_one_more_each_press():
    """From the photo showing, each step one photo further back in the Frame's shown history (a
    photo counted once per run of showings), passing over what is no longer usable. Mutation
    probe: count each history row."""
    assert previous_step(HISTORY, "photo-a", 1, lambda _: True) == "photo-x"
    assert previous_step(HISTORY, "photo-a", 2, lambda _: True) == "photo-y"
    assert previous_step(HISTORY, "photo-a", 3, lambda _: True) == "photo-x"
    assert previous_step(HISTORY, "photo-a", 2, lambda asset: asset != "photo-y") == "photo-x"
    assert previous_step(HISTORY, "photo-a", 9, lambda _: True) is None
    # From the anchor's newest showing, wherever it is in the history.
    assert previous_step(HISTORY, "photo-y", 1, lambda _: True) == "photo-x"
    first = cover_for(facts(history=HISTORY), SkipKind.PREVIOUS, CoverCause.PREVIOUS, request_id="one",
                      at=10, now=8, cue=None)
    assert (first.pinned, first.anchor, first.steps) == ("photo-x", "photo-a", 1)
    assert (first.end, first.fade_out) == (30, 0)
    again = cover_for(facts(history=HISTORY, recent=(first,)), SkipKind.PREVIOUS, CoverCause.PREVIOUS,
                      request_id="two", at=14, now=12, cue=None)
    assert (again.pinned, again.anchor, again.steps) == ("photo-y", "photo-a", 2)


def test_previous_chain_ends_when_another_photo_is_presented():
    """A Previous after the last one's cover gave way (the Frame presented its next photo) steps
    back from the photo showing then, not further along the old chain."""
    first = cover_for(facts(history=HISTORY), SkipKind.PREVIOUS, CoverCause.PREVIOUS, request_id="one",
                      at=10, now=8, cue=None)
    later = (ShownPhoto("photo-b", "b", 31), *HISTORY)
    after = cover_for(facts(history=later, recent=(first,)), SkipKind.PREVIOUS, CoverCause.PREVIOUS,
                      request_id="two", at=40, now=38, cue=None)
    assert (after.anchor, after.steps, after.pinned) == ("photo-b", 1, "photo-a")


def test_asked_is_requested_then_done_or_late():
    assert asked_state(100, None, 101) is AskState.REQUESTED
    assert asked_state(100, 103, 200) is AskState.DONE
    assert asked_state(100, None, 100 + ACK_LATE_SECONDS + 1) is AskState.LATE
