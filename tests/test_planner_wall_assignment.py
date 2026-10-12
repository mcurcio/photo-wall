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
from support.key_content_builders import face, measured

from central.catalog import Candidate, SourcePool
from central.planner import PlannerLimits, Projection, project
from central.runtime import Contribution, Runtime, Scene
from central.wall_plan.fit_settings import FitSettings
from central.wall_plan.key_content import MeasureFailed
from central.wall_plan.order import KeptStill, PhotoShowing, WallHistory
from central.wall_plan.selection import SceneSelection
from contracts.models import AfterEnd, FrameProfile, Layer, OutputBinding, Variant
from contracts.on_screen import Extent, on_screen_extent, replaces_kept_at
from contracts.photo_crop import CENTRED

PROFILES = {
    "wide": FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24),
    "tall": FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24),
    "four_three": FrameProfile(width_px=1440, height_px=1080, diagonal_inches=24),
    "square": FrameProfile(width_px=1080, height_px=1080, diagonal_inches=24),
}
SIZES = {"wide": (3840, 2160), "tall": (2160, 3840), "four_three": (4000, 3000),
         "three_four": (3000, 4000), "two_three": (2400, 3600), "square": (3000, 3000)}
UNCERTAINTY = 0.1
POOL = "pool:1"


def photo(asset: str, shape: str, *, captured: float = 0, prepared: bool = True,
          is_measured: bool = True, faces=()) -> Candidate:
    width, height = SIZES[shape]
    variant = Variant(sha256=hashlib.sha256(asset.encode()).hexdigest(), size=10,
                      media_type="image/jpeg", width=width, height=height) if prepared else None
    if is_measured:
        return measured(asset, width, height, faces=faces, captured_at=captured, variant=variant)
    return Candidate(asset_id=asset, kind="image", original_width=width, original_height=height,
                     captured_at=captured, variant=variant)


@dataclass(frozen=True)
class FrameSetup:
    shape: str
    fade_in: float = 0
    after_end: AfterEnd = "leave_as_is"
    authored: tuple[str, ...] = ()


def scene(frames: dict[str, FrameSetup], *, cycle: float = 30, order="newest_first",
          no_repeat_days: int = 0) -> Runtime:
    instance = Runtime()
    instance.set_scene(Scene(
        scene_id="scene", cycle_seconds=cycle, loop=True,
        selection=SceneSelection(order=order, no_repeat_days=no_repeat_days),
        contributions=tuple(Contribution(
            target=f"frame:{frame}", fade_in_seconds=setup.fade_in, after_end=setup.after_end,
            **({"asset_refs": setup.authored} if setup.authored else {"source_refs": (POOL,)}))
            for frame, setup in frames.items())))
    instance.activate("scene", "run", 0)
    return instance


def plan(instance: Runtime, frames: dict[str, FrameSetup], photos, *, now: float = 0,
         horizon: float = 300, plan_seconds: float = 3600, locks=(), authored=(),
         history: WallHistory = WallHistory(), limits: PlannerLimits = PlannerLimits(),
         fit: FitSettings = FitSettings()) -> Projection:
    """Each Frame full screen on a Display of its profile's size (its Frame output)."""
    return project(
        instance, now,
        bindings_by_player={"player": tuple(OutputBinding(
            output_id=f"out-{frame}", frame_id=frame, generation=1,
            profile=PROFILES[setup.shape]) for frame, setup in frames.items())},
        source_pools={POOL: SourcePool(source_ref=POOL, ready=True, candidates=tuple(photos))},
        authored_candidates={candidate.asset_id: candidate for candidate in authored},
        locked_assignments={layer.assignment_id: layer for layer in locks},
        history=history, horizon_seconds=horizon, plan_seconds=plan_seconds,
        max_clock_uncertainty=UNCERTAINTY, limits=limits, settings=fit,
        frame_outputs={frame: (PROFILES[setup.shape].width_px, PROFILES[setup.shape].height_px)
                       for frame, setup in frames.items()})


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
                               kept: tuple[KeptStill, ...] = ()) -> None:
    """Rule 1 over the whole plan with every showing known: the fixed (offered) layers, the plan's
    new and tentative photos, and the kept stills from the history."""
    on_frame: dict[str, list[Showing]] = defaultdict(list)
    seen = set()
    for row in fixed:
        on_frame[row.frame_id].append(Showing(row.start, row.end, row.fade_in, row.after_end,
                                              row.asset_id))
        seen.add(row.assignment_id)
    for entry in projection.plan:
        if entry.asset_id is None or entry.assignment_id in seen:
            continue
        setup = frames[entry.frame_id]
        on_frame[entry.frame_id].append(Showing(entry.start, entry.end, setup.fade_in,
                                                setup.after_end, entry.asset_id))
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
        shape=draw(st.sampled_from(SHAPES)), fade_in=draw(st.sampled_from((0, 2))),
        after_end=draw(st.sampled_from(("leave_as_is", "keep_this_photo", "keep_nothing"))))
        for n in range(count)}
    size = draw(st.integers(1, 8))
    photos = [photo(f"p{n}", draw(st.sampled_from(PHOTO_SHAPES)), captured=n,
                    prepared=draw(st.booleans()) or n == 0) for n in range(size)]
    # Hand-placed Frames (F-13): some Frames show one of the pool's photos, by hand, so a
    # hand-placed photo contends with the Scene lines' and with another hand-placed Frame's.
    for name in [name for name in frames if draw(st.booleans()) and name != "frame-0"]:
        frames[name] = FrameSetup(shape=frames[name].shape, fade_in=frames[name].fade_in,
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
    return (frames, photos, refreshed, kept, draw(st.sampled_from((10, 30))),
            draw(st.sampled_from(("shuffle", "newest_first"))), draw(st.integers(5, 45)))


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(walls())
def test_no_photo_is_on_two_frames_at_once_across_replans(case):
    """Case 2 (property): random pools, Frame shapes, fades and after-states, a kept still,
    hand-placed Frames, the first plan's offered layers locked into a re-plan after a pool
    refresh: no photo has
    overlapping extents on two Frames anywhere in either plan, locked layers and kept stills
    included, and the re-plan never changes a locked layer. Mutation probe: check extents only
    within one instant (forget earlier picks' extents)."""
    frames, photos, refreshed, kept, cycle, order, later = case
    instance = scene(frames, cycle=cycle, order=order)
    history = WallHistory(kept=kept)
    first = plan(instance, frames, photos, horizon=60, plan_seconds=600, history=history,
                 authored=photos)
    assert_never_on_two_frames(frames, first, kept=kept)
    locks = layers(first)
    selected = offered_history(first)
    second = plan(instance, frames, refreshed, now=later, horizon=60, plan_seconds=600,
                  locks=locks, history=WallHistory(selected=selected, kept=kept),
                  authored=photos)
    replanned = {layer.assignment_id: layer for layer in layers(second)}
    for lock in locks:
        if lock.end > later:
            assert replanned[lock.assignment_id] == lock
    assert_never_on_two_frames(frames, second, fixed=selected, kept=kept)


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
    the preparation horizon is still planned and filled. Mutation probe: refuse the plan
    (PlanningBudgetExceeded) as before."""
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
                          sha256=hashlib.sha256(b"clip").hexdigest(), size=10,
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
