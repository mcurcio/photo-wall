import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from central.runtime import Child, Contribution, Program, RecordingActuator, Runtime, Scene


def media(target="frame:left", source="holiday:v1", **kwargs):
    return Contribution(target=target, source_refs=(source,), **kwargs)


def lamp(start=0, end=1):
    return Contribution(target="actuator:lamp", kind="actuator", ramp_from=start, ramp_to=end)


def get_run(view, run_id):
    return next(run for run in view.runs if run.run_id == run_id)


def test_calendar_boundary_projection_overlay_and_current_reveal():
    midnight = datetime(2027, 1, 1, tzinfo=UTC).timestamp()
    runtime = Runtime()
    runtime.set_scene(Scene(
        scene_id="december", loop=True, cycle_seconds=40,
        contributions=(media(), lamp()),
    ))
    runtime.set_scene(Scene(
        scene_id="january", loop=True, cycle_seconds=60,
        contributions=(media(source="january:v1"), lamp()),
    ))
    portraits = Scene(
        scene_id="portraits", cycle_seconds=30,
        contributions=(
            Contribution(target="frame:left", asset_refs=("left-portrait",), role="left-speaker"),
            Contribution(target="frame:right", asset_refs=("right-portrait",), role="right-speaker"),
        ),
    )
    runtime.set_scene(Scene(
        scene_id="overlay", cycle_seconds=30,
        children=(Child(scene=portraits),),
        contributions=(lamp(0.8, 0.8), Contribution(target="frame:surround", kind="black")),
    ))
    runtime.set_program(Program(
        program_id="dec", scene_id="december", starts_at=midnight - 50, ends_at=midnight,
    ))
    runtime.set_program(Program(
        program_id="jan", scene_id="january", starts_at=midnight, ends_at=midnight + 300,
    ))
    december = runtime.advance(midnight - 10).for_target("frame:left").run_id
    overlay = runtime.activate("overlay", "manual-overlay", midnight - 10, priority=10).run_id
    before = runtime.export_state()
    projected = runtime.project(midnight + 25)
    assert runtime.export_state() == before
    assert projected.for_target("frame:left").scene_id == "january"
    assert projected.for_target("frame:left").cycle_position == 25
    assert projected.for_target("actuator:lamp").actuator_value == pytest.approx(25 / 60)
    still_covered = runtime.advance(midnight + 10)
    assert get_run(still_covered, december).phase == "body"  # outgoing cycle ends at +30
    assert get_run(still_covered, overlay).phase == "body"
    assert still_covered.for_target("frame:left").role == "left-speaker"
    assert still_covered.for_target("frame:right").asset_refs == ("right-portrait",)
    assert still_covered.for_target("actuator:lamp").actuator_value == 0.8
    reveal = runtime.advance(midnight + 25)
    assert reveal == projected
    assert runtime.advance(midnight + 30).for_target("frame:left").scene_id == "january"
    assert get_run(runtime.advance(midnight + 30), december).phase == "completed"


@pytest.mark.parametrize("cover_lamp", [False, True])
def test_recording_actuator_only_current_winner_without_hidden_replay(cover_lamp):
    runtime = Runtime()
    runtime.set_scene(Scene(
        scene_id="background", cycle_seconds=100, contributions=(media(), lamp()),
    ))
    runtime.set_scene(Scene(
        scene_id="cover", cycle_seconds=20,
        contributions=(Contribution(target="frame:left", kind="black"),)
        + ((lamp(0.8, 0.8),) if cover_lamp else ()),
    ))
    background = runtime.activate("background", "bg", 0).run_id
    adapter = RecordingActuator()
    adapter.apply(runtime.advance(10), now=10, authorized_run_ids={background})
    overlay = runtime.activate("cover", "cover", 10, priority=10).run_id
    view = runtime.advance(20)
    adapter.apply(view, now=20, authorized_run_ids={background, overlay})
    assert adapter.records[-1].value == pytest.approx(0.8 if cover_lamp else 0.2)
    before = list(adapter.records)
    future = runtime.project(40)
    assert adapter.records == before
    with pytest.raises(ValueError, match="current instant"):
        adapter.apply(future, now=20, authorized_run_ids={background})
    adapter.apply(runtime.advance(40), now=40, authorized_run_ids={background})
    assert [r.value for r in adapter.records] == pytest.approx(
        [0.1, 0.8 if cover_lamp else 0.2, 0.4]
    )
    assert adapter.apply(runtime.advance(40), now=40, authorized_run_ids={background}) == ()
    adapter.apply(runtime.advance(50), now=50, authorized_run_ids=set())
    assert adapter.records[-1].at == 40


def test_delayed_child_union_does_not_issue_early_dark_or_move_roles():
    child = Scene(scene_id="child", cycle_seconds=5, contributions=(
        Contribution(target="frame:right", asset_refs=("right-only",), role="right-speaker"),
    ))
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="background", cycle_seconds=100, contributions=(
        media("frame:right"),
    )))
    runtime.set_scene(Scene(scene_id="parent", cycle_seconds=20, children=(
        Child(scene=child, delay_seconds=10),
    )))
    runtime.activate("background", "bg", 0)
    parent = runtime.activate("parent", "parent", 0, priority=10).run_id
    view = runtime.advance(5)
    assert "frame:right" in get_run(view, parent).participants
    assert view.for_target("frame:right").scene_id == "background"
    assert not get_run(view, parent).children
    child_intent = runtime.advance(12).for_target("frame:right")
    assert child_intent.scene_id == "child"
    assert child_intent.role == "right-speaker"
    assert child_intent.target == "frame:right"
    assert child_intent.interval_start == 10
    assert child_intent.cycle_position == 2


def test_natural_finish_stops_unstarted_children_and_waits_current_work_then_outro():
    current = Scene(scene_id="current", cycle_seconds=12, loop=True, contributions=(media(),))
    future = Scene(scene_id="future", cycle_seconds=3, contributions=(media("frame:future"),))
    runtime = Runtime()
    runtime.set_scene(Scene(
        scene_id="parent", cycle_seconds=10, loop=True,
        children=(Child(scene=current, delay_seconds=2), Child(scene=future, delay_seconds=8)),
        outro_seconds=3, outro_contributions=(Contribution(target="frame:left", kind="black"),),
    ))
    runtime.set_program(Program(program_id="window", scene_id="parent", starts_at=0, ends_at=5))
    runtime.advance(0)
    baseline = runtime.export_state()
    future_view = runtime.project(1000)
    assert runtime.export_state() == baseline
    assert all(r.phase == "completed" for r in future_view.runs)
    at_expiry = runtime.advance(5)
    root = next(r for r in at_expiry.runs if r.parent_id is None)
    assert root.finish_requested_at == 5
    assert len(root.children) == 1
    assert runtime.advance(11).for_target("frame:left").scene_id == "current"
    assert runtime.advance(14).for_target("frame:left").kind == "black"
    final = runtime.advance(17)
    assert get_run(final, root.run_id).ended_at == 17
    assert final.contributions == ()
    assert all(r.scene_id != "future" for r in final.runs)


def test_cancel_is_downward_and_skips_outro():
    grandchild = Scene(scene_id="grandchild", cycle_seconds=50, contributions=(media(),))
    child = Scene(scene_id="child", cycle_seconds=50, children=(Child(scene=grandchild),))
    runtime = Runtime()
    runtime.set_scene(Scene(
        scene_id="parent", cycle_seconds=100, children=(Child(scene=child),),
        outro_seconds=10, outro_contributions=(Contribution(target="frame:left", kind="black"),),
    ))
    root = runtime.activate("parent", "parent", 0).run_id
    child_run = get_run(runtime.advance(2), root).children[0]
    view = runtime.cancel(child_run, 2)
    assert get_run(view, root).phase == "body"
    assert get_run(view, root).children == ()
    assert len(view.runs) == 1
    final = runtime.cancel(root, 3)
    assert get_run(final, root).phase == "cancelled"
    assert final.contributions == ()


def test_tree_edits_adopt_only_on_next_root_including_delayed_child():
    child_v1 = Scene(scene_id="child", revision=1, cycle_seconds=10, contributions=(media(),))
    root_v1 = Scene(scene_id="root", cycle_seconds=20, children=(Child(
        scene=child_v1, delay_seconds=5,
    ),))
    runtime = Runtime()
    runtime.set_scene(root_v1)
    original = runtime.activate("root", "one", 0).run_id
    child_v2 = child_v1.model_copy(update={"revision": 2, "contributions": (media(source="new:v2"),)})
    root_v2 = root_v1.model_copy(update={"revision": 2, "children": (Child(scene=child_v2),)})
    runtime.set_scene(root_v2)
    assert runtime.advance(7).for_target("frame:left").scene_revision == 1
    runtime.cancel(original, 7)
    runtime.activate("root", "two", 7)
    assert runtime.advance(7).for_target("frame:left").source_refs == ("new:v2",)
    with pytest.raises(ValidationError):
        root_v1.revision = 9


def test_repeat_idempotency_restart_queue_capacity_and_expiry():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="scene", cycle_seconds=10, contributions=(media(),)))
    first = runtime.activate("scene", "first", 0)
    assert runtime.activate("scene", "first", 1) == first
    ignored = runtime.activate("scene", "ignored", 1)
    assert ignored.status == "ignored" and ignored.run_id == first.run_id
    queued = runtime.activate("scene", "queued", 1, repeat="queue", expires_at=30)
    assert queued.status == "queued"
    expired = runtime.activate("scene", "expired", 1, repeat="queue", expires_at=3)
    assert expired.status == "queued"
    for index in range(14):
        assert runtime.activate(
            "scene", f"queue-{index}", 1, repeat="queue", expires_at=2
        ).status == "queued"
    assert runtime.activate("scene", "full", 1, repeat="queue", expires_at=4).reason == "queue_full"
    runtime.advance(10)
    assert runtime.activate("scene", "expired", 10).status == "expired"
    admitted = runtime.activate("scene", "queued", 10)
    assert admitted.status == "admitted"
    assert get_run(runtime.advance(10), admitted.run_id).started_at == 10
    restart = runtime.activate("scene", "restart", 12, repeat="restart")
    view = runtime.advance(12)
    assert get_run(view, admitted.run_id).phase == "cancelled"
    assert get_run(view, restart.run_id).started_at == 12


def test_protection_reserves_delayed_frames_force_explicit_actuators_independent():
    runtime = Runtime()
    child = Scene(scene_id="later", cycle_seconds=5, contributions=(media("frame:right"),))
    runtime.set_scene(Scene(
        scene_id="protected", cycle_seconds=30, protect_frames=True,
        children=(Child(scene=child, delay_seconds=10),), contributions=(lamp(),),
    ))
    runtime.set_scene(Scene(scene_id="incoming", cycle_seconds=10, contributions=(media("frame:right"),)))
    runtime.set_scene(Scene(scene_id="lighting", cycle_seconds=10, contributions=(lamp(1, 1),)))
    runtime.activate("protected", "p", 0)
    assert runtime.activate("incoming", "manual", 1, priority=100).reason == "protected_frames"
    assert runtime.activate("lighting", "light", 1, priority=10).status == "admitted"
    assert runtime.activate("incoming", "forced", 1, priority=100, force=True).status == "admitted"
    assert runtime.advance(1).for_target("frame:right").scene_id == "incoming"


def test_serialization_restart_no_duplicate_and_projection_equals_current():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="long", cycle_seconds=10, loop=True, contributions=(media(),)))
    runtime.set_program(Program(program_id="long", scene_id="long", starts_at=0, ends_at=100))
    current = runtime.advance(5)
    restored = Runtime.restore(json.loads(json.dumps(runtime.export_state())))
    projected = runtime.project(37)
    assert restored.advance(37) == projected
    assert restored.advance(37) == runtime.advance(37)
    assert len(restored.export_state()["admissions"]) == 1
    assert current.for_target("frame:left").run_id == projected.for_target("frame:left").run_id
    assert projected.for_target("frame:left").cycle_position == 7
    assert projected.for_target("frame:left").logical_position == 37


def test_same_scene_program_successor_and_missed_window_are_explicit():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="bg", cycle_seconds=20, loop=True, contributions=(media(),)))
    runtime.set_program(Program(program_id="z-outgoing", scene_id="bg", starts_at=0, ends_at=15))
    runtime.set_program(Program(program_id="a-successor", scene_id="bg", starts_at=15, ends_at=30))
    first = runtime.advance(10).for_target("frame:left")
    at_boundary = runtime.advance(15)
    successor = at_boundary.for_target("frame:left")
    assert successor.run_id != first.run_id
    assert successor.logical_origin == 15
    assert get_run(at_boundary, first.run_id).phase == "body"
    assert successor.root_order > first.root_order
    cold = Runtime()
    cold.set_scene(Scene(scene_id="bg", cycle_seconds=20, contributions=(media(),)))
    cold.set_program(Program(program_id="missed", scene_id="bg", starts_at=0, ends_at=10))
    assert cold.advance(20).contributions == ()
    assert next(iter(cold.export_state()["admissions"].values()))["status"] == "expired"


def test_opacity_distinguishes_black_from_dissolve_and_keeps_underlying_layers():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="bg", cycle_seconds=100, contributions=(media(),)))
    runtime.set_scene(Scene(scene_id="black", cycle_seconds=20, contributions=(
        Contribution(target="frame:left", kind="black", fade_out_seconds=10),
    )))
    runtime.activate("bg", "bg", 0)
    runtime.activate("black", "black", 0, priority=10)
    assert runtime.advance(5).for_target("frame:left").opacity == 1
    fading = runtime.advance(15)
    assert fading.for_target("frame:left").kind == "black"
    assert fading.for_target("frame:left").opacity == 0.5
    assert len([i for i in fading.contributions if i.target == "frame:left"]) == 2
    assert runtime.advance(20).for_target("frame:left").kind == "media"


def test_long_run_leaf_catchup_has_bounded_state():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="month", cycle_seconds=30, loop=True, contributions=(media(),)))
    runtime.set_program(Program(program_id="month", scene_id="month", starts_at=0, ends_at=3000000))
    intent = runtime.advance(2500001).for_target("frame:left")
    assert intent.cycle_index == 83333
    assert intent.cycle_position == 11
    assert len(runtime.export_state()["runs"]) == 1


@pytest.mark.parametrize("definition", [
    {"target": "untyped"},
    {"target": "frame:left", "kind": "actuator"},
    {"target": "actuator:lamp", "kind": "black"},
    {"target": "frame:left", "kind": "media"},
    {"target": "frame:left", "kind": "black", "opacity": float("nan")},
])
def test_invalid_contributions_rejected(definition):
    with pytest.raises(ValidationError):
        Contribution(**definition)


def test_nonfinite_time_and_backwards_clock_rejected():
    runtime = Runtime()
    runtime.advance(10)
    for invalid in (9, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            runtime.advance(invalid)


def test_explicit_finish_at_exact_boundary_does_not_start_next_cycle():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="bg", cycle_seconds=10, loop=True, contributions=(media(),)))
    root = runtime.activate("bg", "bg", 0).run_id
    final = runtime.finish(root, 10)
    assert get_run(final, root).ended_at == 10
    assert final.contributions == ()


def test_explicit_finish_future_instant_allows_earlier_delayed_child():
    runtime = Runtime()
    runtime.set_scene(Scene(
        scene_id="root", cycle_seconds=10, loop=True,
        children=(Child(
            scene=Scene(scene_id="child", cycle_seconds=20, contributions=(media(),)),
            delay_seconds=3,
        ),),
    ))
    root = runtime.activate("root", "root", 0).run_id
    view = runtime.finish(root, 8)
    assert view.for_target("frame:left").scene_id == "child"
    assert view.for_target("frame:left").interval_start == 3
    assert get_run(runtime.advance(23), root).ended_at == 23


@pytest.mark.parametrize("with_child", [False, True])
def test_protected_ownership_releases_before_successor_at_exact_boundary(with_child):
    runtime = Runtime()
    protected_child = Scene(scene_id="child", cycle_seconds=10, contributions=(media(),))
    runtime.set_scene(Scene(
        scene_id="protected", cycle_seconds=10, protect_frames=True,
        contributions=() if with_child else (media(),),
        children=(Child(scene=protected_child),) if with_child else (),
    ))
    runtime.set_scene(Scene(scene_id="successor", cycle_seconds=10, contributions=(media(),)))
    runtime.activate("protected", "protected", 0)
    runtime.set_program(Program(
        program_id="next", scene_id="successor", starts_at=10, ends_at=20,
    ))
    assert runtime.advance(10).for_target("frame:left").scene_id == "successor"


def test_program_definition_updates_next_window_removal_stops_all_owned_roots():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="bg", cycle_seconds=10, loop=True, contributions=(media(),)))
    runtime.set_program(Program(program_id="schedule", scene_id="bg", starts_at=0, ends_at=100))
    original = runtime.advance(5).for_target("frame:left").run_id
    runtime.set_program(Program(program_id="schedule", scene_id="bg", starts_at=0, ends_at=6))
    assert runtime.advance(7).for_target("frame:left").run_id == original
    runtime.set_program(Program(program_id="schedule", scene_id="bg", starts_at=8, ends_at=100))
    newer = runtime.advance(8).for_target("frame:left").run_id
    assert newer != original
    runtime.remove_program("schedule", 9)
    assert get_run(runtime.advance(10), original).ended_at == 10
    assert get_run(runtime.advance(18), newer).ended_at == 18
    assert runtime.advance(18).contributions == ()


def test_backdated_already_missed_program_is_not_replayed():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="scene", cycle_seconds=10, loop=True, contributions=(media(),)))
    runtime.advance(100)
    program = Program(program_id="historical", scene_id="scene", starts_at=0, ends_at=80)
    runtime.set_program(program)
    view = runtime.advance(100)
    assert view.runs == ()
    assert runtime.export_state()["admissions"][program.activation_id]["status"] == "expired"


def test_remove_program_reconciles_intervening_start_but_not_same_time_start():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="bg", cycle_seconds=100, loop=True, contributions=(media(),)))
    runtime.set_program(Program(program_id="p", scene_id="bg", starts_at=10, ends_at=200))
    runtime.advance(0)
    view = runtime.remove_program("p", 15)
    root = next(r for r in view.runs if r.parent_id is None)
    assert root.started_at == 10
    assert root.finish_requested_at == 15
    assert get_run(runtime.advance(110), root.run_id).ended_at == 110
    runtime.set_program(Program(program_id="future", scene_id="bg", starts_at=120, ends_at=200))
    runtime.remove_program("future", 120)
    assert len(runtime.export_state()["admissions"]) == 1


def test_external_activation_ids_cannot_alias_child_or_program_identity():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="parent", cycle_seconds=20, children=(Child(
        scene=Scene(scene_id="child", cycle_seconds=10, contributions=(media(),)),
    ),)))
    runtime.set_scene(Scene(scene_id="other", cycle_seconds=5, contributions=(media("frame:other"),)))
    parent = runtime.activate("parent", "external", 0).run_id
    child = get_run(runtime.advance(0), parent).children[0]
    attempted_alias = runtime.activate("other", f"{parent}/0/0", 1).run_id
    assert attempted_alias != child
    view = runtime.advance(20)
    assert get_run(view, parent).phase == "completed"
    assert get_run(view, parent).children == ()
    with pytest.raises(ValueError, match="reserved"):
        runtime.activate("other", "program:p:30", 20)
    runtime.set_program(Program(program_id="p", scene_id="other", starts_at=30, ends_at=40))
    assert runtime.advance(30).for_target("frame:other").scene_id == "other"


def test_nested_protection_reserves_its_frames_without_protecting_unrelated_siblings():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="parent", cycle_seconds=20, children=(Child(
        scene=Scene(scene_id="protected-child", cycle_seconds=10, protect_frames=True,
                    contributions=(media("frame:right"),)),
        delay_seconds=5,
    ),), contributions=(media("frame:left"),)))
    runtime.set_scene(Scene(scene_id="right", cycle_seconds=10, contributions=(media("frame:right"),)))
    runtime.set_scene(Scene(scene_id="left", cycle_seconds=10, contributions=(media("frame:left"),)))
    runtime.activate("parent", "parent", 0)
    assert runtime.activate("right", "right", 1, priority=100).reason == "protected_frames"
    assert runtime.activate("left", "left", 1, priority=100).status == "admitted"


def test_timeline_contains_short_child_and_all_loop_boundaries_without_effects():
    from central.runtime import RuntimeBudgetExceeded

    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="root", cycle_seconds=2, loop=True, children=(Child(
        scene=Scene(scene_id="brief", cycle_seconds=0.05, contributions=(media(),)),
        delay_seconds=0.125,
    ),)))
    runtime.activate("root", "root", 0)
    before = runtime.export_state()
    timeline = runtime.timeline(0, 3)
    assert [view.now for view in timeline] == [0, 0.125, 0.175, 2, 2.125, 2.175]
    assert timeline[1].for_target("frame:left").scene_id == "brief"
    assert timeline[2].for_target("frame:left") is None
    assert runtime.export_state() == before
    with pytest.raises(RuntimeBudgetExceeded):
        runtime.timeline(0, 3, max_events=3)
    assert runtime.export_state() == before


def test_timeline_does_not_skip_leaf_cycles_and_retains_authored_fade_origin():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="scene", loop=True, cycle_seconds=10, contributions=(
        media(opacity=0.8, fade_in_seconds=2, fade_out_seconds=3),
    )))
    runtime.activate("scene", "scene", 0)
    timeline = runtime.timeline(5, 31)
    assert [v.now for v in timeline] == [5, 10, 20, 30]
    intent = timeline[0].for_target("frame:left")
    assert intent.interval_start == 0
    assert intent.base_opacity == 0.8
    assert intent.fade_in_seconds == 2
    assert intent.fade_out_seconds == 3
    assert runtime.timeline(5, 10)[-1].now == 5


def test_pathologically_tiny_cycles_fail_bounded_projection():
    from central.runtime import RuntimeBudgetExceeded

    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="tiny", cycle_seconds=1e-12, loop=True, contributions=(media(),)))
    runtime.activate("tiny", "tiny", 0)
    before = runtime.export_state()
    with pytest.raises(RuntimeBudgetExceeded):
        runtime.timeline(0, 300, max_events=20)
    assert runtime.export_state() == before


def test_timeline_budget_counts_simultaneous_program_admissions_and_extreme_cycles():
    from central.runtime import RuntimeBudgetExceeded

    instance = Runtime()
    instance.set_scene(Scene(scene_id="empty", cycle_seconds=10))
    for index in range(20):
        instance.set_program(Program(program_id=f"p{index}", scene_id="empty", starts_at=10, ends_at=20))
    instance.advance(0)
    before = instance.export_state()
    with pytest.raises(RuntimeBudgetExceeded):
        instance.timeline(10, 11, max_events=5)
    assert instance.export_state() == before
    tiny = Runtime()
    tiny.set_scene(Scene(scene_id="tiny", loop=True, cycle_seconds=1e-320))
    tiny.activate("tiny", "tiny", 0)
    with pytest.raises(RuntimeBudgetExceeded):
        tiny.timeline(1, 2)


def test_early_v1_snapshot_without_local_order_restores_child_precedence():
    instance = Runtime()
    instance.set_scene(Scene(scene_id="root", cycle_seconds=20, contributions=(media(),), children=(Child(
        scene=Scene(scene_id="child", cycle_seconds=10, contributions=(
            Contribution(target="frame:left", kind="black"),
        )),
    ),)))
    instance.activate("root", "root", 0)
    legacy = instance.export_state()
    for record in legacy["runs"].values():
        del record["local_order"]
        del record["descendant_sequence"]
    restored = Runtime.restore(legacy)
    assert restored.advance(1).for_target("frame:left").scene_id == "child"
    assert all("local_order" not in record for record in legacy["runs"].values())
