"""The wall plan through the real Planner (roadmap 5a, run ledger .claude/runs/photo-5a.md, slice
W1): every Frame planned together an hour ahead, one photo per Frame, never on two Frames at
once (central/wall_plan/assignment.py).

Owner, 2026-10-11: "Never show the same photo on two frames at the same time"; "this “no repeat”
idea is secondary to always ensuring that frames have content"; "I suggest an algorithm that
computes all frame+photo assignments well in advance".
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from support.frame_outputs import frame_outputs_of
from support.frames import Screen
from support.house_time import UTC_HOUSE
from support.key_content_builders import face, measured
from support.position import LIGHTING

from central.catalog import Candidate, SourcePool
from central.everyday.model import CoverCause, SkipKind
from central.everyday.policy import cover_for, photo_of, showing_at
from central.everyday.ports import FrameFacts, LayerOrigin, ShownPhoto
from central.planner import PlannerLimits, Projection, project
from central.runtime import Contribution, Runtime, Scene, Transition
from central.wall_plan.assignment import (
    MAX_GUARD_UNCERTAINTY,
    FixedShowing,
    PlanWindow,
    Reservation,
    _Spans,
    plan_wall,
)
from central.wall_plan.fit_settings import FitSettings
from central.wall_plan.key_content import MeasureFailed
from central.wall_plan.order import KeptStill, PhotoShowing, PoolLines, WallHistory
from central.wall_plan.selection import SceneSelection
from contracts.models import AfterEnd, Layer, OutputBinding, Variant
from contracts.on_screen import Extent, on_screen_extent, replaces_kept_at
from contracts.photo_crop import CENTRED

PROFILES = {
    "wide": Screen(1920, 1080, inches=24),
    "tall": Screen(1080, 1920, inches=24),
    "four_three": Screen(1440, 1080, inches=24),
    "square": Screen(1080, 1080, inches=24),
}
SIZES = {"wide": (3840, 2160), "tall": (2160, 3840), "four_three": (4000, 3000),
         "three_four": (3000, 4000), "two_three": (2400, 3600), "square": (3000, 3000)}
UNCERTAINTY = 0.1
POOL = "pool:1"


def photo(asset: str, shape: str, *, captured: float = 0, prepared: bool = True,
          is_measured: bool = True, faces=()) -> Candidate:
    width, height = SIZES[shape]
    variant = Variant(asset_id=asset, recipe_id="e" * 64, sha256=hashlib.sha256(asset.encode()).hexdigest(), size=10,
                      media_type="image/jpeg", width=width, height=height) if prepared else None
    if is_measured:
        return measured(asset, width, height, faces=faces, captured_at=captured, variant=variant)
    return Candidate(asset_id=asset, kind="image", original_width=width, original_height=height,
                     captured_at=captured, variant=variant, readable_size=(width, height))


@dataclass(frozen=True)
class FrameSetup:
    shape: str
    after_end: AfterEnd = "leave_as_is"
    authored: tuple[str, ...] = ()


CUT = Transition()


def fade_in_of(transition: Transition) -> float:
    """Every photo's fade-in under `transition` (a looping Scene's: central/runtime.py
    `_transition_at`): a cross-fade's seconds, half of fade out and in's."""
    return transition.seconds if transition.style == "cross_fade" else transition.seconds / 2


def scene(frames: dict[str, FrameSetup], *, cycle: float = 30, order="newest_first",
          no_repeat_days: int = 0, transition: Transition = CUT) -> Runtime:
    instance = Runtime(house=UTC_HOUSE)
    instance.set_scene(Scene(
        scene_id="scene", cycle_seconds=cycle, loop=True, transition=transition,
        selection=SceneSelection(order=order, no_repeat_days=no_repeat_days),
        contributions=tuple(Contribution(
            target=f"frame:{frame}", after_end=setup.after_end,
            **({"asset_refs": setup.authored} if setup.authored else {"source_refs": (POOL,)}))
            for frame, setup in frames.items())))
    instance.activate("scene", "run", 0)
    return instance


def plan(instance: Runtime, frames: dict[str, FrameSetup], photos, *, now: float = 0,
         horizon: float = 300, plan_seconds: float = 3600, locks=(), authored=(),
         history: WallHistory = WallHistory(), limits: PlannerLimits = PlannerLimits(),
         fit: FitSettings = FitSettings(), reserved=(), frame_uncertainty=None, covers=(),
         never: frozenset[str] = frozenset()) -> Projection:
    """Each Frame full screen on a Display of its profile's size (its Frame output)."""
    return project(
        instance, now,
        bindings_by_player={"player": tuple(OutputBinding(
            output_id=f"out-{frame}", frame_id=frame, generation=1,
            lighting=LIGHTING, display_mm=None) for frame, setup in frames.items())},
        lines=PoolLines({POOL: SourcePool(source_ref=POOL, ready=True,
                                          candidates=tuple(photos))}),
        authored_candidates={candidate.asset_id: candidate for candidate in authored},
        locked_assignments={layer.assignment_id: layer for layer in locks},
        history=history, horizon_seconds=horizon, plan_seconds=plan_seconds,
        max_clock_uncertainty=UNCERTAINTY, limits=limits, settings=fit,
        reserved=reserved, frame_uncertainty=frame_uncertainty, covers=covers, never=never,
        frame_outputs=frame_outputs_of({
            frame: (PROFILES[setup.shape].width_px, PROFILES[setup.shape].height_px)
            for frame, setup in frames.items()}, inches=24))


def layers(projection: Projection) -> tuple[Layer, ...]:
    return projection.for_player("player").layers


def by_frame(projection: Projection) -> dict[str, list]:
    planned = defaultdict(list)
    for entry in projection.plan:
        planned[entry.frame_id].append(entry)
    return planned


@dataclass(frozen=True, eq=False)
class Showing:
    start: float
    end: float
    fade_in: float
    after_end: AfterEnd
    asset_id: str


def offered_history(*projections: Projection) -> tuple[PhotoShowing, ...]:
    """What coordination records when it offers each projection's new layers."""
    rows = []
    for projection in projections:
        selections = {s.assignment_id: s for s in projection.selections if s.asset_id}
        for layer in layers(projection):
            selection = selections.get(layer.assignment_id)
            if selection is not None:
                rows.append(PhotoShowing(
                    assignment_id=layer.assignment_id, asset_id=selection.asset_id,
                    frame_id=layer.frame_id, scene_id=selection.scene_id,
                    pass_number=selection.pass_number, start=layer.start, end=layer.end,
                    fade_in=layer.fade_in, after_end=layer.after_end))
    return tuple(rows)


def assert_never_on_two_frames(frames: dict[str, FrameSetup], projection: Projection,
                               fixed: tuple[PhotoShowing, ...] = (),
                               kept: tuple[KeptStill, ...] = (), *,
                               transition: Transition = CUT) -> None:
    """Rule 1 over the whole plan with every showing known: the fixed (offered) layers, the plan's
    new and tentative photos (each from when it starts to show, faded in by `transition`), and
    the kept stills from the history."""
    on_frame: dict[str, list[Showing]] = defaultdict(list)
    seen = set()
    for row in fixed:
        on_frame[row.frame_id].append(Showing(row.start, row.end, row.fade_in, row.after_end,
                                              row.asset_id))
        seen.add(row.assignment_id)
    offered = {layer.assignment_id: layer for layer in layers(projection)}
    for entry in projection.plan:
        if entry.asset_id is None or entry.assignment_id in seen:
            continue
        setup = frames[entry.frame_id]
        # An offered photo is on screen as its layer says (what the Player draws).
        layer = offered.get(entry.assignment_id)
        on_frame[entry.frame_id].append(
            Showing(layer.start, layer.end, layer.fade_in, layer.after_end, entry.asset_id)
            if layer is not None else
            Showing(entry.start, entry.end, fade_in_of(transition), setup.after_end, entry.asset_id))
    extents: list[tuple[str, str, Extent]] = [
        (showing.asset_id, frame, on_screen_extent(showing, showings, UNCERTAINTY))
        for frame, showings in on_frame.items() for showing in showings]
    for still in kept:
        replaced = [at for showing in on_frame[still.frame_id]
                    if (at := replaces_kept_at(showing)) is not None and at >= still.since]
        guard = 2 * UNCERTAINTY
        extents.append((still.asset_id, still.frame_id, Extent(
            start=still.since - guard,
            end=max(still.since, min(replaced)) + guard if replaced else None)))
    for (asset, frame, extent), (other_asset, other_frame, other) in combinations(extents, 2):
        assert not (asset == other_asset and frame != other_frame and extent.overlaps(other)), (
            asset, frame, extent, other_frame, other)


def test_the_whole_hour_in_one_call():
    """Case 1: a plan slot for every 30 s cycle of both Frames over the hour."""
    frames = {"left": FrameSetup("wide"), "right": FrameSetup("tall")}
    photos = [photo(f"wide-{n}", "wide", captured=n) for n in range(6)] + [
        photo(f"tall-{n}", "tall", captured=n) for n in range(6)]
    projection = plan(scene(frames), frames, photos)
    planned = by_frame(projection)
    assert {frame: len(entries) for frame, entries in planned.items()} == {"left": 120, "right": 120}
    assert all(entry.state in ("offered", "tentative") for entry in projection.plan)
    assert {entry.start for entry in planned["left"]} == {30.0 * n for n in range(120)}
    assert len([layer for layer in layers(projection)]) == 2 * 10
    assert_never_on_two_frames(frames, projection)


SHAPES = ("wide", "tall", "four_three", "square")
PHOTO_SHAPES = ("wide", "tall", "four_three", "three_four", "square")


@st.composite
def walls(draw):
    count = draw(st.integers(2, 4))
    frames = {f"frame-{n}": FrameSetup(
        shape=draw(st.sampled_from(SHAPES)),
        after_end=draw(st.sampled_from(("leave_as_is", "keep_this_photo", "keep_nothing"))))
        for n in range(count)}
    size = draw(st.integers(1, 8))
    photos = [photo(f"p{n}", draw(st.sampled_from(PHOTO_SHAPES)), captured=n,
                    prepared=draw(st.booleans()) or n == 0) for n in range(size)]
    # Hand-placed Frames (F-13): some Frames show one of the pool's photos, by hand, so a
    # hand-placed photo contends with the Scene lines' and with another hand-placed Frame's.
    for name in [name for name in frames if draw(st.booleans()) and name != "frame-0"]:
        frames[name] = FrameSetup(shape=frames[name].shape,
                                  after_end=frames[name].after_end, authored=(
                                      draw(st.sampled_from([c.asset_id for c in photos])),))
    refreshed = [candidate for candidate in photos if draw(st.booleans())] + [
        photo(f"q{n}", draw(st.sampled_from(PHOTO_SHAPES)), captured=100 + n)
        for n in range(draw(st.integers(0, 3)))]
    kept = ()
    if draw(st.booleans()):
        kept = (KeptStill(frame_id="frame-0",
                          asset_id=draw(st.sampled_from([c.asset_id for c in photos])),
                          since=-5),)
    transition = draw(st.sampled_from((CUT, Transition(style="cross_fade", seconds=3),
                                       Transition(style="fade_out_and_in", seconds=2))))
    return (frames, photos, refreshed, kept, draw(st.sampled_from((10, 30))),
            draw(st.sampled_from(("shuffle", "newest_first"))), draw(st.integers(5, 45)),
            transition)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(walls())
def test_no_photo_is_on_two_frames_at_once_across_replans(case):
    """Case 2 (property): random pools, Frame shapes, Transitions (a cut, a cross-fade whose
    next photo shows 3 s before its turn, fade out and in) and after-states, a kept still,
    hand-placed Frames, the first plan's offered layers locked into a re-plan after a pool
    refresh: no photo has
    overlapping extents on two Frames anywhere in either plan, locked layers and kept stills
    included, and the re-plan never changes a locked layer. Mutation probe: check extents only
    within one instant (forget earlier picks' extents). Planning a slot from its turn while its
    layer starts at `shows_from` is caught by the deterministic
    `test_a_cross_fading_photo_is_never_on_two_frames_while_it_dissolves_in`, not reliably here:
    random examples seldom put the one photo on two Frames within one dissolve."""
    frames, photos, refreshed, kept, cycle, order, later, transition = case
    instance = scene(frames, cycle=cycle, order=order, transition=transition)
    history = WallHistory(kept=kept)
    first = plan(instance, frames, photos, horizon=60, plan_seconds=600, history=history,
                 authored=photos)
    assert_never_on_two_frames(frames, first, kept=kept, transition=transition)
    locks = layers(first)
    selected = offered_history(first)
    second = plan(instance, frames, refreshed, now=later, horizon=60, plan_seconds=600,
                  locks=locks, history=WallHistory(selected=selected, kept=kept),
                  authored=photos)
    replanned = {layer.assignment_id: layer for layer in layers(second)}
    for lock in locks:
        if lock.end > later:
            assert replanned[lock.assignment_id] == lock
    assert_never_on_two_frames(frames, second, fixed=selected, kept=kept, transition=transition)



def test_a_cross_fading_photo_is_never_on_two_frames_while_it_dissolves_in():
    """A cross-faded photo is on screen from when it starts to show, 3 s before its turn: the
    wall plan keeps it off every other Frame from then, here beside another Scene whose turns
    (9 s) end inside that lead. Mutation probe: plan the slot from its turn while its layer
    starts at `shows_from` (a photo shows on both Frames during the dissolve)."""
    frames = {"left": FrameSetup("wide"), "right": FrameSetup("wide")}
    instance = Runtime(house=UTC_HOUSE)
    for scene_id, frame, cycle, transition in (
            ("dissolving", "left", 10, Transition(style="cross_fade", seconds=3)),
            ("cutting", "right", 9, CUT)):
        instance.set_scene(Scene(
            scene_id=scene_id, cycle_seconds=cycle, loop=True, transition=transition,
            selection=SceneSelection(order="newest_first", no_repeat_days=0),
            contributions=(Contribution(target=f"frame:{frame}", source_refs=(POOL,)),)))
        instance.activate(scene_id, scene_id, 0)
    photos = [photo(f"p{n}", "wide", captured=n) for n in range(3)]
    projection = plan(instance, frames, photos, horizon=120, plan_seconds=240)
    assert {layer.start for layer in layers(projection) if layer.frame_id == "left"} >= {0, 7, 17}
    assert_never_on_two_frames(frames, projection)

def test_every_photo_that_fits_some_frame_is_shown_in_time():
    """Case 3 (liveness): no-repeat off, every photo fits one of the Frames: within the hour
    every photo is placed at least once."""
    frames = {"left": FrameSetup("wide"), "right": FrameSetup("tall")}
    photos = [photo(f"wide-{n}", "wide", captured=n) for n in range(5)] + [
        photo(f"tall-{n}", "three_four", captured=n) for n in range(5)]
    projection = plan(scene(frames, order="shuffle"), frames, photos)
    assert {entry.asset_id for entry in projection.plan} == {c.asset_id for c in photos}
    assert_never_on_two_frames(frames, projection)


def test_content_first_one_photo_repeats_and_never_sits_on_two_frames():
    """Case 4: one photo and one Frame keeps it every slot; two Frames and one photo: one Frame
    shows it, the other is `no_photo_free`, and the photo is never on both."""
    one = {"left": FrameSetup("wide")}
    alone = plan(scene(one), one, [photo("only", "wide")])
    assert [entry.asset_id for entry in alone.plan] == ["only"] * 120
    two = {"left": FrameSetup("wide"), "right": FrameSetup("wide")}
    shared = plan(scene(two), two, [photo("only", "wide")])
    planned = by_frame(shared)
    showing = [frame for frame, entries in planned.items()
               if all(entry.asset_id == "only" for entry in entries)]
    assert len(showing) == 1
    other = ({"left", "right"} - set(showing)).pop()
    assert {(entry.state, entry.reason) for entry in planned[other]} == {
        ("unfilled", "no_photo_free")}
    assert "no_photo_free" in {d.code for d in shared.diagnostics}
    assert_never_on_two_frames(two, shared)


def test_a_frame_keeps_its_previous_photo_when_nothing_in_its_pool_is_free():
    """Step 5: a Frame whose pool offers nothing it can show (here an unmeasured photo) shows
    its kept still again rather than nothing. Mutation probe: fill an empty slot with nothing."""
    frames = {"left": FrameSetup("wide", after_end="keep_this_photo"),
              "right": FrameSetup("tall", authored=("kept",))}
    kept = photo("kept", "wide")
    projection = plan(scene(frames), frames, [photo("new", "wide", is_measured=False)],
                      authored=[kept], history=WallHistory(kept=(
                          KeptStill(frame_id="left", asset_id="kept", since=-10),)))
    left = by_frame(projection)["left"]
    assert {entry.asset_id for entry in left} == {"kept"}
    assert [m.asset_id for m in projection.measurements] == ["new"]


def test_order_first_then_each_photo_on_the_frame_it_crops_least():
    """Case 5: two Frames (16:9 and 4:3) and two photos (16:9 and 4:3), ranked either way:
    each photo lands on the Frame it crops least. Mutation probe: skip the augmenting path and
    the least-crop placement (give each slot its first offer)."""
    # Several Frame names, so the slots' ids fall in either order (ties are broken by id).
    for wide, other in (("a", "b"), ("b", "a"), ("left", "right"), ("right", "left")):
        frames = {wide: FrameSetup("wide"), other: FrameSetup("four_three")}
        for newer in ("wide", "four_three"):
            photos = [photo("w", "wide", captured=2 if newer == "wide" else 1),
                      photo("f", "four_three", captured=2 if newer == "four_three" else 1)]
            projection = plan(scene(frames), frames, photos, plan_seconds=300)
            first = {entry.frame_id: entry for entry in projection.plan if entry.start == 0}
            assert (first[wide].asset_id, first[other].asset_id) == ("w", "f")
            assert {layer.crop.offset for layer in layers(projection)} <= {0.5}


def test_a_hand_placed_photo_stays_on_its_frame_and_no_scene_line_takes_it():
    """Case 6: an authored photo stays on its Frame; a Scene line on another Frame never takes
    it in the hour, though it is in that Frame's pool."""
    frames = {"left": FrameSetup("wide", authored=("placed",)), "right": FrameSetup("wide")}
    placed = photo("placed", "wide", captured=9)
    projection = plan(scene(frames), frames, [placed, photo("other", "wide", captured=1)],
                      authored=[placed])
    planned = by_frame(projection)
    assert {entry.asset_id for entry in planned["left"]} == {"placed"}
    assert "placed" not in {entry.asset_id for entry in planned["right"]}
    assert {entry.asset_id for entry in planned["right"]} == {"other"}
    left = [s for s in projection.selections if s.asset_id == "placed"]
    assert left and all(s.scene_id == "scene" and s.pass_number == 1 for s in left)


def test_inside_the_preparation_horizon_only_prepared_photos_become_layers():
    """Case 7: the unprepared photo the line ranks first is requested and a prepared one fills
    the slot; beyond the preparation horizon the plan is tentative. Mutation probe: place
    unprepared photos inside the preparation horizon."""
    frames = {"left": FrameSetup("wide")}
    photos = [photo("new", "wide", captured=2, prepared=False), photo("old", "wide", captured=1)]
    projection = plan(scene(frames), frames, photos, horizon=60, plan_seconds=300)
    assert {layer.variant.sha256 for layer in layers(projection)} == {
        photos[1].variant.sha256}
    assert [(r.asset_id, r.earliest_start) for r in projection.acquisitions] == [("new", 0)]
    later = [entry for entry in projection.plan if entry.start >= 60]
    assert {entry.state for entry in later} == {"tentative"}
    assert "new" in {entry.asset_id for entry in later}


def test_unmeasured_photos_are_never_placed_and_are_requested_in_rank_order():
    """Case 8: at most `max_measurements` requests, the line's best first."""
    frames = {"left": FrameSetup("wide")}
    photos = [photo(f"raw-{n}", "wide", captured=10 + n, is_measured=False) for n in range(10)]
    photos.append(photo("ready", "wide", captured=1))
    projection = plan(scene(frames), frames, photos, limits=PlannerLimits(max_measurements=3))
    assert [m.asset_id for m in projection.measurements] == ["raw-9", "raw-8", "raw-7"]
    assert {entry.asset_id for entry in projection.plan} == {"ready"}


def due(candidate: Candidate) -> Candidate:
    """`candidate` with its key due a new measurement (an older analysis version, or a
    provisional faceless one past its retry: the key-content store's rule)."""
    return candidate.model_copy(update={"analysis": candidate.analysis.model_copy(
        update={"due": True})})


def test_a_photo_due_a_new_measurement_is_placed_meanwhile_and_requested_after_the_unmeasured():
    """Subject detection's re-measuring: a new analysis version marks every stored key due; the
    Planner keeps placing those photos with their old keys (the wall never empties) and requests
    them after the unmeasured ones, the placed ones first, within `max_measurements`.
    Mutation probes: request only the unmeasured -> the due never queued; skip a due photo as
    unmeasured -> it is not placed."""
    frames = {"left": FrameSetup("wide")}
    photos = [due(photo("old-key", "wide", captured=5)), photo("raw", "wide", captured=4,
                                                                  is_measured=False),
              due(photo("old-faceless", "wide", captured=3)), photo("current", "wide", captured=2)]
    projection = plan(scene(frames), frames, photos, plan_seconds=60)
    assert "old-key" in {entry.asset_id for entry in projection.plan}
    assert [m.asset_id for m in projection.measurements] == ["raw", "old-key", "old-faceless"]
    capped = plan(scene(frames), frames, photos, plan_seconds=60,
                  limits=PlannerLimits(max_measurements=2))
    assert [m.asset_id for m in capped.measurements] == ["raw", "old-key"]


def test_the_layer_carries_the_crop_that_keeps_the_faces():
    """Case 9: a 2:3 photo with a face at its left edge on a 9:16 Frame becomes a layer whose
    crop keeps the left (offset 0)."""
    frames = {"left": FrameSetup("tall")}
    portrait = photo("portrait", "two_three", faces=(face(0.0, 0.3, 0.15, 0.5),))
    projection = plan(scene(frames), frames, [portrait], plan_seconds=300)
    assert {layer.crop.offset for layer in layers(projection)} == {0.0}
    assert {entry.crop.offset for entry in projection.plan} == {0.0}


def test_a_wall_past_the_slot_budget_plans_a_shorter_hour_and_fills_every_frame():
    """Content first: 16 Frames at 5 s cycles hold 11,520 slots in the hour, past the wall
    plan's 8,192. The tentative tail is cut at one instant (`plan_shortened`), and every slot of
    the preparation horizon is still planned and filled. Mutation probe: raise in
    `_fit_slot_budget` instead of cutting (the pass fails)."""
    frames = {f"f{n:02}": FrameSetup("wide") for n in range(16)}
    photos = [photo(f"p{n:02}", "wide", captured=n) for n in range(24)]
    projection = plan(scene(frames, cycle=5), frames, photos,
                      limits=PlannerLimits(max_events=PlannerLimits().max_events * 11))
    assert "plan_shortened" in {d.code for d in projection.diagnostics}
    assert len(projection.plan) <= 8192
    inside = [entry for entry in projection.plan if entry.start < 300]
    assert len(inside) == 16 * 60
    assert all(entry.asset_id is not None for entry in inside)
    last = max(entry.start for entry in projection.plan)
    assert 300 <= last < 3600
    assert {frame: max(e.start for e in entries) for frame, entries in
            by_frame(projection).items()} == {frame: last for frame in frames}


def test_a_hand_placed_photo_is_never_on_two_frames_at_once():
    """F-13 for hand-placed slots: two Frames hand-placed with the same photo show it one at a
    time, and a hand-placed photo another Frame still keeps waits until that Frame lets it go.
    Mutation probe: take a hand-placed photo without asking whether it is free."""
    same = photo("same", "wide", captured=1)
    both = {"left": FrameSetup("wide", authored=("same",)),
            "right": FrameSetup("wide", authored=("same",))}
    projection = plan(scene(both), both, [], authored=[same])
    assert_never_on_two_frames(both, projection)
    assert {entry.asset_id for entry in projection.plan} == {"same", None}
    kept = (KeptStill(frame_id="keeper", asset_id="same", since=-10),)
    frames = {"keeper": FrameSetup("wide", after_end="keep_this_photo"),
              "placed": FrameSetup("wide", authored=("same",))}
    projection = plan(scene(frames), frames, [photo("other", "wide")], authored=[same],
                      history=WallHistory(kept=kept))
    assert_never_on_two_frames(frames, projection, kept=kept)
    # Hand-placed slots are decided first (step 1), while the keeper's still is open-ended.
    assert {(entry.asset_id, entry.reason) for entry in by_frame(projection)["placed"]} == {
        (None, "no_photo_free")}


def test_no_repeat_only_ranks_a_photo_shown_yesterday_still_fills_every_slot():
    """No-repeat only ranks (owner, 2026-10-11: it is "secondary to always ensuring that frames
    have content"): every pool photo was shown yesterday and the Scene does not repeat within
    7 days, yet every slot of the hour is filled. Mutation probe: leave the recently shown
    photos out of the Scene's line."""
    frames = {"left": FrameSetup("wide"), "right": FrameSetup("wide")}
    photos = [photo(f"p{n}", "wide", captured=n) for n in range(4)]
    yesterday = tuple(PhotoShowing(
        assignment_id=f"old-{n}", asset_id=candidate.asset_id, frame_id="left",
        scene_id="scene", pass_number=1, start=-86400 + 30 * n, end=-86400 + 30 * (n + 1),
        fade_in=0, after_end="leave_as_is") for n, candidate in enumerate(photos))
    projection = plan(scene(frames, no_repeat_days=7), frames, photos,
                      history=WallHistory(shown=yesterday))
    assert projection.plan and all(entry.state != "unfilled" for entry in projection.plan)
    assert_never_on_two_frames(frames, projection)


def test_failed_photos_are_neither_placed_nor_requested_again():
    """A photo whose measurement failed is not measured again before its retry, and one whose
    preparation failed is never placed, while a measured, prepared photo is there to show.
    Mutation probe: ignore both failures in the slot's standing."""
    frames = {"left": FrameSetup("wide")}
    failed_measure = photo("measure-failed", "wide", captured=3, is_measured=False)
    failed_measure = failed_measure.model_copy(
        update={"analysis": MeasureFailed(reason="decode")})
    failed_prepare = photo("prepare-failed", "wide", captured=2).model_copy(
        update={"preparation_failure": "decode_failed"})
    projection = plan(scene(frames), frames,
                      [failed_measure, failed_prepare, photo("good", "wide", captured=1)])
    assert {entry.asset_id for entry in projection.plan} == {"good"}
    assert not projection.measurements and not projection.acquisitions


def test_a_kept_scenes_video_keeps_nothing():
    """Only a still is kept: a video's layer in a "keep this photo" Scene ends "leave as is".
    Mutation probe: let a video keep its frame."""
    frames = {"left": FrameSetup("wide", after_end="keep_this_photo")}
    video = Candidate(asset_id="clip", kind="video", original_width=1920, original_height=1080,
                      captured_at=5, variant=Variant(
                          asset_id="asset-" + hashlib.sha256(b"clip").hexdigest()[:16], recipe_id="e" * 64, sha256=hashlib.sha256(b"clip").hexdigest(), size=10,
                          media_type="video/mp4", width=1920, height=1080, duration=4))
    projection = plan(scene(frames), frames, [video, photo("still", "wide", captured=1)],
                      plan_seconds=300)
    after = {layer.after_end for layer in layers(projection)
             if layer.variant.sha256 == video.variant.sha256}
    assert after == {"leave_as_is"}
    assert {layer.after_end for layer in layers(projection)
            if layer.variant.sha256 != video.variant.sha256} <= {"keep_this_photo"}


def test_an_unmeasured_hand_placed_photo_is_asked_to_be_measured():
    """Mutation probe: never request for a hand-placed slot."""
    frames = {"left": FrameSetup("wide", authored=("raw",))}
    raw = photo("raw", "wide", is_measured=False)
    projection = plan(scene(frames), frames, [], authored=[raw], plan_seconds=300)
    assert [m.asset_id for m in projection.measurements] == ["raw"]


def test_prepared_photos_play_centred_while_none_is_measured():
    """Content first (C7, owner 2026-10-11): with Immich out of reach, or a new analysis to run,
    a pool of prepared photos none of which is measured still fills every Frame, centred and
    never on two Frames at once, while the measurements are requested. Mutation probe: leave a
    slot unfilled when nothing measured is free."""
    frames = {"left": FrameSetup("wide"), "right": FrameSetup("wide")}
    photos = [photo(f"p{n:02}", "wide", captured=n, is_measured=False) for n in range(30)]
    projection = plan(scene(frames, order="shuffle"), frames, photos)
    assert projection.plan and all(entry.state != "unfilled" for entry in projection.plan)
    assert layers(projection) and {layer.crop for layer in layers(projection)} == {CENTRED}
    assert projection.measurements
    assert_never_on_two_frames(frames, projection)


def test_a_lock_with_no_recorded_selection_still_holds_its_photo():
    """A lock offered before 5a has no selection row, so the history does not name its photo:
    the Planner finds it by the prepared copy the lock draws, and no other Frame takes that photo
    while the lock shows it. Mutation probe: treat such a lock as showing no photo."""
    one = {"left": FrameSetup("wide")}
    four_three = photo("x", "four_three", captured=2)
    first = plan(scene(one), one, [four_three], horizon=120, plan_seconds=120)
    two = {"left": FrameSetup("wide"), "right": FrameSetup("four_three")}
    second = plan(scene(two), two, [four_three, photo("y", "wide", captured=1)], now=10,
                  horizon=120, plan_seconds=600, locks=layers(first), history=WallHistory())
    assert_never_on_two_frames(two, second, fixed=offered_history(first))


def test_rule_1s_index_reads_past_a_short_extent_inside_a_long_one():
    """Rule 1's index (`_Spans`) stops reading back only once no earlier extent can reach the
    query: a long extent on Frame A (0-100) with a later, shorter one on Frame B (10-20) still
    keeps the photo off Frame C at 50-60.

    Mutation probe (P-reach-early-stop): stop on the extent's own end instead of the running
    latest end -> C may show it at 50-60."""
    spans = _Spans()
    spans.add(Extent(start=0, end=100), "a")
    spans.add(Extent(start=10, end=20), "b")
    assert spans.overlaps_another("c", Extent(start=50, end=60))
    assert not spans.overlaps_another("a", Extent(start=50, end=60))
    assert not spans.overlaps_another("c", Extent(start=100, end=110))


def test_a_reservation_keeps_its_photo_off_another_frame_and_never_blocks_its_own():
    """Decision 0032 (B5): a photo a Player may be cycling past its plan (or carries) is held on
    its Frame over its extent: no other Frame takes it then, its own Frame still may. Mutation
    probe: check reservations on every Frame (the left Frame never shows "wide-3" before 200)."""
    frames = {"left": FrameSetup("wide"), "right": FrameSetup("wide")}
    photos = [photo(f"wide-{n}", "wide", captured=n) for n in range(4)]
    held = Reservation(frame_id="left", asset_id="wide-3", extent=Extent(start=0, end=200))
    planned = by_frame(plan(scene(frames), frames, photos, reserved=(held,)))
    assert not any(entry.asset_id == "wide-3" and entry.start < 200 + 2 * UNCERTAINTY
                   for entry in planned["right"])
    assert any(entry.asset_id == "wide-3" and entry.start < 200 for entry in planned["left"])
    assert any(entry.asset_id == "wide-3" for entry in planned["right"])     # once it lapses


def test_each_frames_hand_over_guard_is_its_own_players_reported_bound():
    """Decision 0032 (B6): a Frame whose Player reports a 3 s show-clock bound guards each
    hand-over by 6 s; a Frame with no report by the limit; a bound past the cap by the cap.
    Mutation probe: use the global guard for every Frame."""
    showings = [FixedShowing(slot_id=f"s-{frame}", frame_id=frame, asset_id=f"photo-{frame}",
                             start=100, end=130, fade_in=0, after_end="leave_as_is")
                for frame in ("reported", "silent", "lost")]
    wall = plan_wall((), showings, (), {}, {}, {}, window=PlanWindow(
        now=0, preparation_end=300, plan_end=3600), max_clock_uncertainty=UNCERTAINTY,
        settings=FitSettings(), frame_uncertainty={"reported": 3.0, "lost": 1000.0})
    extents = {entry.frame_id: entry.extent for entry in wall.on_screen}
    assert extents["reported"] == Extent(start=94, end=136)
    assert extents["silent"] == Extent(start=100 - 2 * UNCERTAINTY, end=130 + 2 * UNCERTAINTY)
    cap = 2 * MAX_GUARD_UNCERTAINTY
    assert extents["lost"] == Extent(start=100 - cap, end=130 + cap)


# --- Decision 0036 (slice 3): an everyday action's covers ------------------------------------------


def facts_of(projection: Projection, frame: str, *, history=(), listed=frozenset()) -> FrameFacts:
    """A Frame as the everyday rules read it from a projection's offered layers."""
    on_frame = tuple(layer for layer in layers(projection) if layer.frame_id == frame)
    return FrameFacts(frame_id=frame, layers=on_frame, history=history, listed=listed,
                      origins={layer.assignment_id: LayerOrigin(scene_id="scene")
                               for layer in on_frame if not layer.action})


def covered_plan(frames, photos, *, kind=SkipKind.NEXT, cycle=30, at=10, cue=None, history=(),
                 frame="left", authored=(), never=frozenset()):
    """A plan, then the cover a skip asked at `at - 2` adds on `frame`, then the re-plan at
    `at - 2` with the first plan's layers locked."""
    instance = scene(frames, cycle=cycle)
    first = plan(instance, frames, photos, authored=authored)
    cover = cover_for(facts_of(first, frame, history=history), kind,
                      CoverCause.NEXT if kind is SkipKind.NEXT else CoverCause.PREVIOUS,
                      request_id="request", at=at, now=at - 2, cue=cue)
    assert cover is not None
    second = plan(instance, frames, photos, now=at - 2, locks=layers(first),
                  history=WallHistory(selected=offered_history(first)), covers=(cover,),
                  authored=authored, never=never)
    return first, cover, second


def test_a_joining_cover_is_emitted_unchanged_above_its_layer():
    """A Skip on a Frame whose next turn is offered: its cover shows that turn's photo from the
    shared instant until that turn's layer is fully in, above it, and every offered layer stays as
    it was. Mutation probe: drop `joins` covers in `add_covers`."""
    frames = {"left": FrameSetup("wide"), "right": FrameSetup("wide")}
    photos = [photo(f"wide-{n}", "wide", captured=n) for n in range(8)]
    first, cover, second = covered_plan(frames, photos)
    assert cover.how == "joins"
    replanned = {layer.assignment_id: layer for layer in layers(second)}
    assert replanned[cover.cover_id] == cover.layer
    assert replanned[cover.cover_id].action and replanned[cover.cover_id].start == 10
    for lock in layers(first):
        assert replanned[lock.assignment_id] == lock
    joined = replanned[cover.joined]
    assert photo_of(replanned[cover.cover_id]) == photo_of(joined)
    assert showing_at(tuple(layer for layer in layers(second) if layer.frame_id == "left"),
                      15).assignment_id == cover.cover_id


def test_a_pick_cover_takes_the_lines_next_never_the_photo_left():
    """A Skip on a Frame whose turn outlasts the offer (a 10-minute turn): the cover is a slot of
    the wall plan, filled from the Scene's line, never with the photo left, and never a repeat.
    Mutation probe: let a pick take `excluded`."""
    frames = {"left": FrameSetup("wide")}
    photos = [photo(f"wide-{n}", "wide", captured=n) for n in range(3)]
    first, cover, second = covered_plan(frames, photos, cycle=600)
    assert cover.how == "pick"
    placed = next(layer for layer in layers(second) if layer.assignment_id == cover.cover_id)
    assert placed.action and placed.start == 10 and placed.end == cover.end
    assert photo_of(placed) != cover.left and photo_of(placed) in {p.asset_id for p in photos}
    assert not any(span for span in second.for_player("player").unfilled
                   if span.start == 10), "a cover is never an unfilled span"


def test_a_pick_cover_with_no_photo_but_the_one_left_waits_unfilled():
    """A pick with nothing else to take is not a repeat: it stays unplaced (the next pass plans it
    again), and the Frame keeps its layer."""
    frames = {"left": FrameSetup("wide")}
    _, cover, second = covered_plan(frames, [photo("only", "wide")], cycle=600)
    assert cover.how == "pick"
    assert cover.cover_id not in {layer.assignment_id for layer in layers(second)}


def test_a_previous_cover_places_its_photo_even_where_another_frame_shows_it():
    """The owner's S4: Previous shows the photo the Frame showed before even when another Frame
    shows it now; the plan never adds an overlap itself. Mutation probe: ask whether a
    hand-placed cover's photo is free."""
    frames = {"left": FrameSetup("wide"), "right": FrameSetup("wide")}
    photos = [photo(f"wide-{n}", "wide", captured=n) for n in range(8)]
    first = plan(scene(frames), frames, photos)
    on_right = photo_of(showing_at([layer for layer in layers(first) if layer.frame_id == "right"], 10))
    on_left = photo_of(showing_at([layer for layer in layers(first) if layer.frame_id == "left"], 10))
    history = (ShownPhoto(asset_id=on_left, assignment_id="a-now", presented_at=5),
               ShownPhoto(asset_id=on_right, assignment_id="a-before", presented_at=-30))
    _, cover, second = covered_plan(frames, photos, kind=SkipKind.PREVIOUS, history=history,
                                    authored=photos)
    assert cover.how == "pinned" and cover.pinned == on_right
    placed = next(layer for layer in layers(second) if layer.assignment_id == cover.cover_id)
    assert photo_of(placed) == on_right and placed.action


def test_a_wall_cover_is_a_cue_member_and_a_frames_own_is_not():
    frames = {"left": FrameSetup("wide")}
    photos = [photo(f"wide-{n}", "wide", captured=n) for n in range(4)]
    _, cover, second = covered_plan(frames, photos, cue="skip-request")
    assert cover.cover_id in {cue.assignment_id for cue in second.cues}
    _, own, alone = covered_plan(frames, photos)
    assert own.cover_id not in {cue.assignment_id for cue in alone.cues}


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(walls(), st.integers(0, 3), st.booleans())
def test_no_placement_overlaps_an_action_layer(case, which, previous):
    """Property: a random wall, a Skip or Previous asked on one Frame, the wall re-planned with
    the cover: every photo the re-plan placed itself (not a lock, not the cover) is off every
    other Frame while the cover shows it. Walking covers after the other slots is caught by the
    deterministic `test_a_slot_planned_with_a_cover_never_takes_its_photo_on_another_frame`, not
    reliably here (random examples seldom give a fresh slot the cover's photo)."""
    frames, photos, _, kept, cycle, order, _, transition = case
    instance = scene(frames, cycle=cycle, order=order, transition=transition)
    first = plan(instance, frames, photos, horizon=60, plan_seconds=600, authored=photos,
                 history=WallHistory(kept=kept))
    frame = sorted(frames)[which % len(frames)]
    history = tuple(ShownPhoto(asset_id=c.asset_id, assignment_id=f"h{n}", presented_at=-10 * n)
                    for n, c in enumerate(photos))
    kind = SkipKind.PREVIOUS if previous else SkipKind.NEXT
    facts = facts_of(first, frame, history=history)
    cover = cover_for(facts, kind, CoverCause.NEXT, request_id="r", at=12, now=10, cue=None)
    if cover is None:
        return
    second = plan(instance, frames, photos, now=10, horizon=60, plan_seconds=600,
                  locks=layers(first), history=WallHistory(selected=offered_history(first), kept=kept),
                  authored=photos, covers=(cover,))
    placed = next((layer for layer in layers(second) if layer.assignment_id == cover.cover_id), None)
    if placed is None or photo_of(placed) is None:
        return
    guard = 2 * UNCERTAINTY
    locked = {layer.assignment_id for layer in layers(first)}
    for entry in second.plan:
        if (entry.frame_id != frame and entry.asset_id == photo_of(placed)
                and entry.assignment_id not in locked):
            assert entry.end + guard <= placed.start - guard or entry.start - guard >= placed.end + guard, (
                entry, placed)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(walls(), st.data())
def test_a_listed_photo_is_never_assigned_again_across_replans(case, data):
    """Property: with some photos on the Never show list, no slot of either plan takes one (an
    empty pool is not relaxed: the slot is unfilled), a hand-placed one included, and no
    continuation lists one; only a layer offered before it was listed (a lock) still shows it.
    Mutation probe: leave `never` out of `_Walk._standing`."""
    frames, photos, refreshed, kept, cycle, order, later, transition = case
    listed = frozenset(data.draw(st.sets(st.sampled_from([c.asset_id for c in photos]))))
    instance = scene(frames, cycle=cycle, order=order, transition=transition)
    first = plan(instance, frames, photos, horizon=60, plan_seconds=600, authored=photos,
                 history=WallHistory(kept=kept), never=listed)
    assert not {entry.asset_id for entry in first.plan} & listed
    locks = layers(first)
    second = plan(instance, frames, refreshed, now=later, horizon=60, plan_seconds=600,
                  locks=locks, history=WallHistory(selected=offered_history(first), kept=kept),
                  authored=photos, never=listed)
    locked = {layer.assignment_id for layer in locks}
    assert not {entry.asset_id for entry in second.plan if entry.assignment_id not in locked} & listed
    offered = {layer.assignment_id: photo_of(layer) for layer in layers(second)}
    for continuation in second.for_player("player").continuations:
        assert not {offered[photo] for photo in continuation.photos} & listed


def test_a_slot_planned_with_a_cover_never_takes_its_photo_on_another_frame():
    """The duplicate line: a Previous pins Q on the left while the right's turn, not offered yet,
    would take Q from the line; the plan gives the right another photo, as the cover is walked
    first and its extent counts. Mutation probe: walk covers after the other slots."""
    frames = {"left": FrameSetup("wide"), "right": FrameSetup("wide")}
    photos = [photo(f"wide-{n}", "wide", captured=n) for n in range(8)]
    instance = scene(frames)
    first = plan(instance, frames, photos)
    left_only = tuple(layer for layer in layers(first) if layer.frame_id == "left")
    history = WallHistory(selected=tuple(row for row in offered_history(first) if row.frame_id == "left"))
    alone = plan(instance, frames, photos, now=8, locks=left_only, history=history)
    wanted = photo_of(showing_at([layer for layer in layers(alone) if layer.frame_id == "right"], 10))
    on_left = photo_of(showing_at(left_only, 10))
    assert wanted != on_left
    facts = FrameFacts(frame_id="left", layers=left_only,
                       origins={layer.assignment_id: LayerOrigin(scene_id="scene") for layer in left_only},
                       history=(ShownPhoto(asset_id=on_left, assignment_id="a-now", presented_at=5),
                                ShownPhoto(asset_id=wanted, assignment_id="a-before", presented_at=-30)))
    cover = cover_for(facts, SkipKind.PREVIOUS, CoverCause.PREVIOUS, request_id="r", at=10, now=8, cue=None)
    assert cover is not None and cover.pinned == wanted
    second = plan(instance, frames, photos, now=8, locks=left_only, history=history, covers=(cover,),
                  authored=photos)
    placed = next(layer for layer in layers(second) if layer.assignment_id == cover.cover_id)
    assert photo_of(placed) == wanted
    right_now = showing_at([layer for layer in layers(second) if layer.frame_id == "right"], 10)
    assert photo_of(right_now) != wanted
