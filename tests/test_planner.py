import hashlib
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from support.frame_outputs import frame_output_of, frame_outputs_of, target_of
from support.house_time import UTC_HOUSE, window_program
from support.key_content_builders import measured
from support.position import LIGHTING

import central.planner as planner_module
from central.catalog import Candidate, SourcePool
from central.house_time.model import HouseTime
from central.planner import PlannerLimits, PlanningError, _ProjectionBuilder, eligible, project
from central.runtime import Child, Contribution, RecordingActuator, Runtime, Scene, Transition
from central.wall_plan.assignment import WallLimits
from central.wall_plan.fit_settings import FitSettings
from central.wall_plan.order import PoolLines, WallHistory
from central.wall_plan.selection import SceneSelection
from contracts.models import OutputBinding, Plan, Variant, stack_order

# Roadmap 5a: a photo is placed only once its key content is measured and it fills its Frame
# (central/wall_plan/assignment.py), so these candidates are measured, with an even interest
# grid, and default to the default Frame's 9:16 shape and pixels.


def candidate(asset="a", *, width=1080, height=1920, kind="image", prepared=True, captured=0,
              variant_width=None, variant_height=None) -> Candidate:
    variant = Variant(
        asset_id="asset-" + hashlib.sha256(asset.encode()).hexdigest()[:16], recipe_id="e" * 64, sha256=hashlib.sha256(asset.encode()).hexdigest(), size=10,
        media_type="video/mp4" if kind == "video" else "image/jpeg",
        width=variant_width or width, height=variant_height or height,
        duration=10 if kind == "video" else None,
    ) if prepared else None
    return measured(asset, width, height, kind=kind, captured_at=captured, variant=variant)


def pool(source="holiday:1", *candidates, ready=True):
    return SourcePool(source_ref=source, ready=ready, candidates=candidates)


def binding(frame="left", output="hdmi-1", *, generation=1):
    return OutputBinding(output_id=output, frame_id=frame, generation=generation, lighting=LIGHTING,
                         display_mm=None)


def runtime(*, cycle=10, contributions=None, children=(), order="newest_first",
            transition=Transition()):
    """One looping Scene; newest first unless named (its order is roadmap 5a's Scene setting)."""
    instance = Runtime(house=UTC_HOUSE)
    instance.set_scene(Scene(
        scene_id="scene", cycle_seconds=cycle, loop=True, children=children, transition=transition,
        selection=SceneSelection(order=order),
        contributions=contributions if contributions is not None else (
            Contribution(target="frame:left", source_refs=("holiday:1",)),
        ),
    ))
    instance.activate("scene", "run", 0)
    return instance


def full_screen(bindings) -> dict:
    """Each bound Frame full screen on a 40-inch Display of the default Frame's 9:16 pixels, showing
    videos (its Frame output; Central works it out, the binding never carries it)."""
    return {item.frame_id: frame_output_of(item.frame_id, (1080, 1920), inches=40)
            for items in bindings.values() for item in items}


def propose(instance, *, now=0, horizon=25, assets=None, pools=None, bindings=None,
            authored=None, locks=None, limits=None, frame_outputs=None):
    bindings = bindings or {"player-one": (binding(),)}
    options = dict(
        bindings_by_player=bindings, settings=FitSettings(),
        frame_outputs=full_screen(bindings) if frame_outputs is None else frame_outputs,
        lines=PoolLines(pools if pools is not None else {
            "holiday:1": pool("holiday:1", *(assets or [candidate()])),
        }),
        authored_candidates=authored or {}, locked_assignments=locks or {},
        history=WallHistory(), horizon_seconds=horizon, plan_seconds=horizon,
    )
    if limits:
        options["limits"] = limits
    return project(instance, now, **options)


def layers(projection):
    return projection.for_player("player-one").layers


def test_live_results_change_unsecured_cycles_same_run_and_locks_preserve_deleted_bytes():
    instance = runtime()
    first = propose(instance, assets=[candidate("old")])
    locked = layers(first)[1]
    refreshed = propose(instance, now=5, assets=[candidate("new")],
                        locks={locked.assignment_id: locked})
    assert len({layer.run_id for layer in layers(refreshed)}) == 1
    by_start = {layer.start: layer for layer in layers(refreshed)}
    assert by_start[0].variant == candidate("new").variant
    assert by_start[10] == locked
    assert by_start[20].variant == candidate("new").variant
    assert layers(first)[0].assignment_id == by_start[0].assignment_id
    assert instance.advance(5).for_target("frame:left").source_refs == ("holiday:1",)
    empty = propose(instance, now=5, pools={"holiday:1": pool()}, locks={locked.assignment_id: locked})
    assert layers(empty) == (locked,)
    assert {d.code for d in empty.diagnostics} >= {"source_empty", "no_eligible_candidates"}


@pytest.mark.parametrize("kind,width,height,diagonal,video,expected", [
    ("image", 2000, 1000, 20, True, False),     # 2:1 on 9:16 crops beyond the limit
    ("image", 2160, 3840, 50, True, True),
    ("image", 2400, 3600, 50, True, True),      # 2:3 on 9:16 crops 16 %
    ("image", 1000, 2000, 50, True, False),     # the kept window has fewer than the Frame's pixels
    ("image", 2000, 2000, 50, True, False),     # 1:1 on 9:16 crops 44 %
    ("video", 720, 1280, 40, True, False),
    ("video", 720, 1280, 39, True, True),
    ("video", 1080, 1920, 40, True, True),
    ("video", 1080, 1920, 20, False, False),
])
def test_original_eligibility_precedes_derivative_dimensions(kind, width, height, diagonal,
                                                           video, expected):
    """Roadmap 5a: eligibility is the photo fit rule's size-only part on the original (ratio,
    not orientation labels; the Frame's pixels; requirements :186's video floor)."""
    asset = candidate(kind=kind, width=width, height=height,
                      variant_width=3840, variant_height=2160)
    target, house = target_of(1080, 1920, video=video, inches=diagonal), FitSettings()
    assert eligible(asset, target, house.crop_limit, house) is expected
    assert eligible(asset, target, 0.5, house) is (expected or (width, height) == (2000, 2000))


def test_upscaled_720p_is_never_sent_to_large_frame_and_authored_alternative_stays_on_role():
    low = candidate("low", kind="video", width=1280, height=720,
                    variant_width=3840, variant_height=2160)
    proper = candidate("proper", kind="video", width=1920, height=1080)
    instance = runtime(contributions=(Contribution(
        target="frame:left", asset_refs=("low", "proper"), role="left-speaker",
    ),))
    landscape = {"player-one": (binding(), binding("right", "hdmi-2"))}
    wide = frame_outputs_of({"left": (1920, 1080), "right": (1920, 1080)}, inches=40)
    result = propose(instance, authored={"low": low, "proper": proper}, bindings=landscape, frame_outputs=wide)
    assert {layer.frame_id for layer in layers(result)} == {"left"}
    assert all(layer.variant == proper.variant for layer in layers(result))
    assert all(selection.asset_id == "proper" for selection in result.selections)
    missing = propose(instance, authored={"low": low}, bindings=landscape, frame_outputs=wide)
    assert layers(missing) == ()
    assert "authored_no_eligible_alternative" in {d.code for d in missing.diagnostics}


def test_a_pool_not_yet_complete_still_plays_and_an_empty_one_says_which_it_is():
    """C7: the last pool keeps playing while Immich is down, so a pool with no complete check
    assigns what it has. Empty, it is `source_not_ready` until a check completes, then
    `source_empty`. Mutation probe: skip pools that are not ready (nothing is assigned)."""
    instance = runtime()
    playing = propose(instance, pools={"holiday:1": pool("holiday:1", candidate(), ready=False)})
    assert [layer.variant for layer in layers(playing)] == [candidate().variant] * 3
    assert not {d.code for d in playing.diagnostics} & {"source_not_ready", "source_empty"}
    waiting = propose(instance, pools={"holiday:1": pool(ready=False)})
    assert layers(waiting) == ()
    codes = {diagnostic.code for diagnostic in waiting.diagnostics}
    assert "source_not_ready" in codes and "source_empty" not in codes
    empty = propose(instance, pools={"holiday:1": pool()})
    codes = {diagnostic.code for diagnostic in empty.diagnostics}
    assert "source_empty" in codes and "source_not_ready" not in codes
    first = layers(propose(instance))[0]
    locked = propose(instance, pools={"holiday:1": pool(ready=False)},
                     locks={first.assignment_id: first})
    assert layers(locked) == (first,)


def test_two_sources_sharing_a_photo_give_one_candidate_newest_first_across_sources():
    """F-1: a contribution's photos are the union of its Sources' pools, one per asset id.
    Mutation probe: union without dedup (the shared photo takes two of the cycles)."""
    instance = runtime(contributions=(
        Contribution(target="frame:left", source_refs=("holiday:1", "garden:1")),))
    shared, older, newest = (candidate("shared", captured=2), candidate("older", captured=1),
                             candidate("newest", captured=3))
    result = propose(instance, horizon=35, pools={
        "holiday:1": pool("holiday:1", older, shared),
        "garden:1": pool("garden:1", shared, newest),
    })
    assert [s.asset_id for s in result.selections] == ["newest", "shared", "older", "newest"]


def test_on_this_day_picks_by_the_house_date():
    """Roadmap 2b: On this day is the house's day. At 23:40 on 12 Oct in New York it is already
    13 Oct in UTC; the photo taken at 23:30 on a 12 Oct (house clock; 13 Oct in UTC) is on this day,
    the one taken at noon on a 13 Oct is not.

    Mutation probe (P-utc-today): keep `datetime.fromtimestamp(now, UTC).date()` -> the 13 Oct
    photo comes first."""
    house = HouseTime(zone="America/New_York")
    zone = ZoneInfo(house.zone)
    now = datetime(2026, 10, 12, 23, 40, tzinfo=zone).timestamp()
    assert datetime.fromtimestamp(now, UTC).day == 13
    instance = Runtime(house=house)
    instance.set_scene(Scene(scene_id="scene", cycle_seconds=10, loop=True,
                             selection=SceneSelection(order="on_this_day"),
                             contributions=(Contribution(target="frame:left", source_refs=("holiday:1",)),)))
    instance.activate("scene", "run", now)
    late = candidate("late-12th", captured=datetime(2025, 10, 12, 23, 30, tzinfo=zone).timestamp())
    noon = candidate("noon-13th", captured=datetime(2024, 10, 13, 12, tzinfo=zone).timestamp())
    result = propose(instance, now=now, horizon=15, assets=[noon, late])
    assert [s.asset_id for s in result.selections][:2] == ["late-12th", "noon-13th"]


def test_a_photo_only_in_a_source_the_contribution_does_not_name_is_never_assigned():
    instance = runtime(contributions=(
        Contribution(target="frame:left", source_refs=("holiday:1",)),))
    result = propose(instance, pools={
        "holiday:1": pool("holiday:1", candidate("mine", captured=1)),
        "garden:1": pool("garden:1", candidate("theirs", captured=9)),
    })
    assert {s.asset_id for s in result.selections} == {"mine"}


def test_a_pool_of_fifty_thousand_photos_on_four_frames_plans_an_hour_with_no_budget_error():
    """C11: no window and no candidate cap; roadmap 5a: the whole wall an hour ahead. Inside the
    preparation horizon no photo is prepared, so the newest are requested; beyond it the plan
    is tentative. Mutation probe: keep a candidate-count bound."""
    frames = ("left", "right", "top", "bottom")
    instance = runtime(cycle=30, contributions=tuple(
        Contribution(target=f"frame:{frame}", source_refs=("holiday:1",)) for frame in frames))
    shared = candidate("photo-0").analysis
    photos = [Candidate(asset_id=f"photo-{n}", kind="image", original_width=1080,
                        original_height=1920, captured_at=n, analysis=shared, readable_size=(1080, 1920))
              for n in range(50_000)]
    bindings = {"player-one": tuple(binding(frame, f"hdmi-{n}") for n, frame in enumerate(frames))}
    result = project(
        instance, 0, bindings_by_player=bindings, settings=FitSettings(),
        frame_outputs=full_screen(bindings),
        lines=PoolLines({"holiday:1": pool("holiday:1", *photos)}), authored_candidates={},
        locked_assignments={}, history=WallHistory(), horizon_seconds=300, plan_seconds=3600)
    assert layers(result) == ()
    assert {r.asset_id for r in result.acquisitions} >= {f"photo-{49_999 - n}" for n in range(4)}
    assert len(result.plan) == 4 * 120
    tentative = [photo for photo in result.plan if photo.state == "tentative"]
    assert len(tentative) == 4 * 110
    assert len({photo.asset_id for photo in tentative}) == len(tentative)


def test_missing_source_diagnosed_and_missing_variant_requests_deduplicated_acquisition():
    instance = runtime(contributions=(
        Contribution(target="frame:left", source_refs=("holiday:1",)),
        Contribution(target="frame:right", source_refs=("holiday:1",)),
    ))
    options = {"player-one": (binding(), binding("right", "hdmi-2"))}
    result = propose(instance, assets=[candidate(prepared=False)], bindings=options)
    assert layers(result) == ()
    assert len(result.acquisitions) == 1
    assert len(result.acquisitions[0].assignment_ids) == 6
    assert result.acquisitions[0].earliest_start == 0
    assert {d.code for d in result.diagnostics} == {"no_photo_free"}
    missing = propose(instance, pools={})
    assert "source_missing" in {d.code for d in missing.diagnostics}


def test_horizon_preserves_current_full_cycle_origins_and_fade_under_locks():
    instance = runtime(contributions=(Contribution(
        target="frame:left", source_refs=("holiday:1",), opacity=0.8,
    ),), transition=Transition(style="fade_out_and_in", seconds=4))
    at_seven = propose(instance, now=7, horizon=10)
    first, second = layers(at_seven)
    assert (first.start, first.end, first.media_origin) == (0, 10, 0)
    assert (first.opacity, first.fade_in, first.fade_out) == (0.8, 2, 2)
    assert second.start == 10
    assert at_seven.horizon_end == 17
    assert at_seven.valid_until == 20
    at_eight = propose(instance, now=8, horizon=10, locks={first.assignment_id: first})
    assert layers(at_eight)[0] == first
    plan = Plan(plan_id="p", revision=1, player_id="player-one", authority_epoch=1,
                issued_at=7, valid_from=7, valid_until=at_seven.valid_until,
                bindings=(binding(),), layers=layers(at_seven))
    assert plan.layers[0].position(7) == 7
    exact = propose(instance, horizon=20)
    assert [layer.start for layer in layers(exact)] == [0, 10]
    assert exact.valid_until == 20


def test_short_child_projection_no_early_dark_keeps_covered_layers_and_commands_nothing():
    brief = Scene(scene_id="brief", cycle_seconds=0.05, contributions=(
        Contribution(target="frame:right", kind="black"),
    ))
    instance = runtime(children=(Child(scene=brief, delay_seconds=0.125),))
    actuator = RecordingActuator()
    before = instance.export_state()
    result = propose(instance, horizon=1,
                     bindings={"player-one": (binding(), binding("right", "hdmi-2"))})
    assert instance.export_state() == before
    assert actuator.records == []
    right = [layer for layer in layers(result) if layer.frame_id == "right"]
    assert len(right) == 1
    assert right[0].presentation == "black"
    assert (right[0].start, right[0].end) == (0.125, 0.175)
    assert instance.advance(0).for_target("frame:right") is None


def test_overlay_and_later_background_child_keep_root_precedence():
    nested = Scene(scene_id="later", cycle_seconds=5, contributions=(
        Contribution(target="frame:left", source_refs=("holiday:1",)),
    ))
    instance = runtime(children=(Child(scene=nested, delay_seconds=2),))
    instance.set_scene(Scene(scene_id="overlay", cycle_seconds=8, contributions=(
        Contribution(target="frame:left", kind="black"),
    ), transition=Transition(style="fade_out_and_in", seconds=2)))
    overlay = instance.activate("overlay", "overlay", 0).run_id
    result = propose(instance, horizon=7)
    at_three = [layer for layer in layers(result) if layer.start <= 3 < layer.end]
    assert len(at_three) == 3
    winner = max(at_three, key=lambda layer: (layer.priority, layer.root_order, layer.admission_order))
    assert winner.run_id == overlay
    child = max(at_three, key=lambda layer: layer.admission_order)
    assert child.run_id != overlay


def test_cancelled_and_stale_generation_locks_do_not_restore_output():
    instance = runtime()
    lock = layers(propose(instance))[0]
    stale = propose(instance, locks={lock.assignment_id: lock},
                    bindings={"player-one": (binding(generation=2),)})
    assert all(layer.assignment_id != lock.assignment_id for layer in layers(stale))
    assert "lock_stale_authority" in {d.code for d in stale.diagnostics}
    instance.cancel(lock.run_id, 1)
    cancelled = propose(instance, now=1, locks={lock.assignment_id: lock})
    assert layers(cancelled) == ()
    assert "lock_outside_projection" in {d.code for d in cancelled.diagnostics}


def test_stable_recent_cycle_variety_and_snapshot_order_independence():
    instance = runtime()
    assets = [candidate("old", captured=1), candidate("new", captured=2)]
    first = propose(instance, assets=assets)
    reverse = propose(instance, assets=list(reversed(assets)))
    assert first == reverse
    assert [selection.asset_id for selection in first.selections] == ["new", "old", "new"]


def _shortened(projection) -> set:
    return {d.reason for d in projection.diagnostics if d.code == "plan_shortened"}


@pytest.mark.parametrize("now,cycle", [(0, 3600), (1800, 3600), (100, 86400)])
def test_a_cycle_of_any_length_is_planned_whole(now, cycle):
    """The outage of 2026-10-27: a Scene saved with one photo an hour (cycle 3600) failed every
    planning pass ("cycle length exceeds supported preparation extent"), and so every Frame.
    A cycle of an hour or a day, asked at its start or midway, is one layer over the whole cycle,
    and the plan stays valid to its end. Mutation probe: reinsert the 300 s cap in `add`."""
    instance = Runtime(house=UTC_HOUSE)
    instance.set_scene(Scene(scene_id="hourly", cycle_seconds=cycle, loop=True, contributions=(
        Contribution(target="frame:left", source_refs=("holiday:1",)),)))
    instance.activate("hourly", "run", 0)
    result = propose(instance, now=now, horizon=300)
    assert [(layer.start, layer.end) for layer in layers(result)] == [(0, cycle)]
    assert result.valid_until == cycle
    assert result.diagnostics == ()


def test_each_planning_budget_shortens_the_plan_without_mutation(monkeypatch):
    """A budget never refuses the wall: Runtime events, layers inside the preparation horizon
    and the wall plan's slots each cut the plan at one instant, keeping what comes before it
    (`plan_shortened`, its reason the budget); diagnostics past theirs are counted. Mutation
    probe: raise in any one instead (that case fails)."""
    instance = runtime()                     # 10 s cycles over a 25 s horizon: 0, 10, 20
    before = instance.export_state()
    events = propose(instance, limits=PlannerLimits(max_events=1))
    assert [layer.start for layer in layers(events)] == [0, 10]   # the timeline stops at 10
    assert _shortened(events) == {"runtime_events"}
    assignments = propose(instance, limits=PlannerLimits(max_assignments=1))
    assert [layer.start for layer in layers(assignments)] == [0]
    assert _shortened(assignments) == {"assignments"}
    diagnostics = propose(instance, pools={}, limits=PlannerLimits(max_diagnostics=1))
    assert len(diagnostics.diagnostics) == 1 and diagnostics.diagnostics_dropped > 0
    monkeypatch.setattr(planner_module, "WallLimits",
                        lambda **limits: WallLimits(max_slots=1, **limits))
    slots = propose(instance)
    assert [layer.start for layer in layers(slots)] == [0]
    assert _shortened(slots) == {"slots"}
    assert instance.export_state() == before


def test_one_refused_intent_leaves_every_other_frame_planned(monkeypatch):
    """The per-Intent bulkhead: an Intent the Planner refuses is left out alone, as
    `intent_refused` naming its Scene and Run; the other Frame is planned as without it.
    Mutation probe: drop the try/except around `add` in `project` (the pass raises)."""
    two = {"player-one": (binding(),), "player-two": (binding("right", "hdmi-1"),)}
    left = Contribution(target="frame:left", source_refs=("holiday:1",))
    instance = runtime(contributions=(left, Contribution(target="frame:right",
                                                         source_refs=("holiday:1",))))
    baseline = propose(runtime(contributions=(left,)), bindings=two)
    assert layers(baseline)
    add = _ProjectionBuilder.add

    def refuse_right(self, intent, window):
        if intent.target == "frame:right":
            raise PlanningError("refused for the test")
        return add(self, intent, window)

    monkeypatch.setattr(_ProjectionBuilder, "add", refuse_right)
    refused = propose(instance, bindings=two)
    assert refused.for_player("player-one") == baseline.for_player("player-one")
    assert refused.for_player("player-two").layers == ()
    issue = next(d for d in refused.diagnostics if d.code == "intent_refused")
    assert (issue.scene_id, issue.run_id, issue.target, issue.reason) == (
        "scene", layers(baseline)[0].run_id, "frame:right", "refused for the test")


def test_no_count_of_players_bindings_or_sources_refuses_the_wall():
    """129 Players, each with its Frame, are all planned: the Planner bounds no count of
    Players, bindings or Sources. Mutation probe: re-add `max_players=128` with its bound."""
    assert not {"max_cycle_seconds", "max_sources", "max_authored_refs", "max_players",
                "max_bindings", "max_horizon_seconds"} & set(PlannerLimits.model_fields)
    assert not hasattr(planner_module, "PlanningBudgetExceeded")
    frames = [f"f{n:03}" for n in range(129)]
    bindings = {f"player-{frame}": (binding(frame),) for frame in frames}
    instance = runtime(contributions=tuple(
        Contribution(target=f"frame:{frame}", kind="black") for frame in frames))
    result = propose(instance, bindings=bindings, horizon=5)
    assert all(result.for_player(player).layers for player in bindings)


def test_invalid_input_and_prepared_variant_fail_safely():
    instance = runtime()
    with pytest.raises(PlanningError, match="one current binding"):
        propose(instance, bindings={"one": (binding(),), "two": (binding(),)})
    with pytest.raises(PlanningError, match="immutable source"):
        propose(instance, pools={"holiday:1": pool("wrong:1")})
    for invalid in (0, -1, float("nan"), float("inf"), True):
        with pytest.raises(PlanningError):
            propose(instance, horizon=invalid)
    bad = candidate().model_copy(update={"variant": candidate(kind="video").variant})
    result = propose(instance, assets=[bad])
    assert layers(result) == ()
    assert "no_photo_free" in {d.code for d in result.diagnostics}


def test_secured_delayed_child_keeps_exact_layer_across_unrelated_activation():
    child = Scene(scene_id="child", cycle_seconds=10, contributions=(
        Contribution(target="frame:left", source_refs=("holiday:1",)),
    ))
    instance = runtime(children=(Child(scene=child, delay_seconds=5),))
    original = propose(instance, horizon=9)
    lock = next(layer for layer in layers(original) if layer.start == 5)
    instance.set_scene(Scene(scene_id="overlay", cycle_seconds=10, contributions=(
        Contribution(target="frame:left", kind="black"),
    )))
    instance.activate("overlay", "overlay", 2, priority=10)
    actual = propose(instance, now=6, horizon=3, locks={lock.assignment_id: lock})
    assert next(layer for layer in layers(actual) if layer.assignment_id == lock.assignment_id) == lock
    assert "lock_stale_authority" not in {diagnostic.code for diagnostic in actual.diagnostics}


def test_secured_future_program_preserves_bytes_while_fresh_plan_revises_root_order():
    instance = runtime()
    instance.set_scene(Scene(scene_id="next", cycle_seconds=10, contributions=(
        Contribution(target="frame:left", source_refs=("holiday:1",)),
    )))
    instance.create_program(window_program("p", "next", 60, 120), 0)
    first = propose(instance, now=50, horizon=15)
    lock = next(layer for layer in layers(first) if layer.run_id != layers(first)[0].run_id)
    instance.set_scene(Scene(scene_id="overlay", cycle_seconds=30, contributions=(
        Contribution(target="frame:left", kind="black"),
    )))
    instance.activate("overlay", "overlay", 55)
    actual = propose(instance, now=61, horizon=4, assets=[candidate("different")],
                     locks={lock.assignment_id: lock})
    replacement = next(layer for layer in layers(actual) if layer.assignment_id == lock.assignment_id)
    assert replacement.variant == lock.variant
    assert replacement.root_order > lock.root_order
    assert replacement.model_dump(exclude={"root_order"}) == lock.model_dump(exclude={"root_order"})


def test_source_health_remains_observable_when_entire_projection_is_locked():
    instance = runtime()
    lock = layers(propose(instance, horizon=5))[0]
    result = propose(instance, horizon=5, locks={lock.assignment_id: lock},
                     pools={"holiday:1": pool(ready=False)})
    assert layers(result) == (lock,)
    assert "source_not_ready" in {diagnostic.code for diagnostic in result.diagnostics}


def test_a_locked_layer_takes_a_revised_fade_out_and_nothing_else():
    """Central may revise only the fade-out and the after-state of a layer it already offered
    (Finish makes the current cycle the last and holds a kept photo there; migration 071 left
    in-flight endings at "leave_as_is"); any other change to it stays a stale lock. Mutation
    probes: refuse the revised fade-out (the layer is dropped as stale); accept any change (the
    revised fade-in is planned)."""
    instance = runtime(transition=Transition(style="fade_out_and_in", seconds=4))
    offered = layers(propose(instance, now=3, horizon=5))[0]
    assert offered.fade_out == 2
    revised = propose(instance, now=4, horizon=5,
                      locks={offered.assignment_id: offered.model_copy(update={"fade_out": 0})})
    assert layers(revised)[0] == offered
    assert not [d for d in revised.diagnostics if d.code == "lock_stale_authority"]
    kept = propose(instance, now=4, horizon=5, locks={
        offered.assignment_id: offered.model_copy(update={"after_end": "keep_nothing"})})
    assert layers(kept)[0] == offered
    stale = propose(instance, now=4, horizon=5,
                    locks={offered.assignment_id: offered.model_copy(update={"fade_in": 0})})
    assert offered.assignment_id not in {layer.assignment_id for layer in layers(stale)}
    assert "lock_stale_authority" in {
        d.code for d in stale.diagnostics if d.assignment_id == offered.assignment_id}


def test_a_cross_faded_photo_starts_its_layer_before_its_turn():
    """Cross-fade, 3 s of a 10 s turn: from the Run's second photo on, each layer starts (and a
    video starts playing) 3 s before its turn, fades in over 3 s and stays at full to its cut;
    a layer whose photo starts to show inside the preparation horizon is offered though its
    turn starts after it; the plan lists layers by the one stacking order, a later photo above
    the one it dissolves in over. Mutation probes: start the layer at its turn; stop the
    timeline at the plan's end (the 7 s layer is missing at an 8 s horizon)."""
    instance = runtime(transition=Transition(style="cross_fade", seconds=3))
    offered = layers(propose(instance, horizon=25))
    assert [(layer.start, layer.end, layer.media_origin, layer.fade_in, layer.fade_out)
            for layer in offered] == [(0, 10, 0, 3, 0), (7, 20, 7, 3, 0), (17, 30, 17, 3, 0)]
    assert list(offered) == sorted(offered, key=stack_order)
    assert [layer.start for layer in layers(propose(instance, horizon=8))] == [0, 7]


def test_a_bound_frame_with_no_known_screen_gets_no_photo_and_asks_for_none():
    """Frame box run fb-8: a Frame whose screen size is not known has no Frame output, so no
    shape: its slots take no photo (no layer, so the Player keeps its last still, else the
    fallback) and ask for no measurement or copy; beside it a sized Frame fills. Mutation probe:
    give `fit_target` a 16:9 shape when `output_px` is None and the unsized Frame is filled."""
    instance = runtime(contributions=(
        Contribution(target="frame:left", source_refs=("holiday:1",)),
        Contribution(target="frame:right", source_refs=("holiday:1",))))
    bindings = {"player-one": (binding("left", "hdmi-1"), binding("right", "hdmi-2"))}
    photos = [candidate("wide", width=1920, height=1080), candidate("tall", width=1080, height=1920),
              candidate("later", width=1080, height=1920, prepared=False)]
    result = propose(instance, assets=photos, bindings=bindings, frame_outputs={
        "left": frame_output_of("left", None), "right": frame_output_of("right", (1080, 1920))})
    left = [entry for entry in result.plan if entry.frame_id == "left"]
    assert left
    assert all(entry.state == "unfilled" and entry.asset_id is None for entry in left)
    assert {layer.frame_id for layer in layers(result)} == {"right"}
    assert ("no_eligible_candidates", "frame:left") in {(d.code, d.target) for d in result.diagnostics}
    right = {entry.assignment_id for entry in result.plan if entry.frame_id == "right"}
    assert all(set(request.assignment_ids) <= right for request in result.acquisitions)
