import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from central.runtime import (
    OPERATOR_HISTORY_SECONDS,
    AdmissionPolicy,
    Child,
    Contribution,
    Program,
    RecordingActuator,
    Runtime,
    RuntimeConflict,
    RuntimeView,
    RunView,
    Scene,
)


def media(target="frame:left", source="holiday:v1", **kwargs):
    return Contribution(target=target, source_refs=(source,), **kwargs)


def lamp(start=0, end=1):
    return Contribution(target="actuator:lamp", kind="actuator", ramp_from=start, ramp_to=end)


def get_run(view, run_id):
    return next(run for run in view.runs if run.run_id == run_id)


def test_drain_policy_rejects_manual_force_restart_and_explicit_queue_before_admission():
    scene = Scene(scene_id="nested", loop=True, children=(Child(scene=Scene(
        scene_id="child", contributions=(media("frame:drained"),),
    )),))
    runtime = Runtime()
    runtime.set_scene(scene)
    existing = runtime.activate("nested", "existing", 0)
    policy = AdmissionPolicy(frozenset({"frame:drained"}))
    guarded = Runtime.restore(runtime.export_state(), admission_policy=policy)

    for identity, options in (
        ("manual", {}),
        ("forced-restart", {"repeat": "restart", "force": True}),
        ("explicit-queue", {"repeat": "queue", "expires_at": 20}),
    ):
        refusal = guarded.activate("nested", identity, 0, **options)
        assert (refusal.status, refusal.reason) == ("rejected", "equipment_draining")
    assert [run.run_id for run in guarded.project(0).runs
            if run.ended_at is None and run.parent_id is None] == [
        existing.run_id
    ]
    assert guarded.export_state()["queue"] == []
    assert guarded.project(0).now == 0
    assert guarded._copy().admission_policy == policy

    guarded.set_scene(Scene(scene_id="unrelated", contributions=(media("frame:open"),)))
    assert guarded.activate("unrelated", "unrelated", 0).status == "admitted"


def test_preexisting_queued_activation_waits_behind_drain_until_expiry_without_spin():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="queued", loop=True, cycle_seconds=100,
                            contributions=(media("frame:open"),)))
    existing = runtime.activate("queued", "existing", 0)
    runtime.set_scene(Scene(scene_id="queued", revision=2, loop=True, cycle_seconds=100,
                            contributions=(media("frame:drained"),)))
    assert runtime.activate("queued", "pending", 0, repeat="queue", expires_at=10).status == (
        "queued"
    )
    guarded = Runtime.restore(
        runtime.export_state(), admission_policy=AdmissionPolicy(frozenset({"frame:drained"}))
    )
    guarded.cancel(existing.run_id, 0)

    before = guarded.export_state()
    assert guarded.project(5, max_events=2).now == 5
    assert guarded.export_state() == before
    assert guarded.advance(5, max_events=2).now == 5
    assert guarded.export_state()["admissions"]["pending"]["status"] == "queued"
    assert guarded.advance(10, max_events=2).now == 10
    assert guarded.export_state()["admissions"]["pending"]["status"] == "expired"
    assert guarded.export_state()["queue"] == []


def test_frame_references_include_nested_outro_and_upcoming_program_context():
    runtime = Runtime()
    runtime.set_scene(Scene(
        scene_id="parent", children=(Child(scene=Scene(
            scene_id="child", outro_seconds=5,
            outro_contributions=(media("frame:portrait"),),
        )),),
    ))
    runtime.set_program(Program(
        program_id="upcoming", scene_id="parent", starts_at=1100, ends_at=1200,
    ))
    runtime.set_program(Program(
        program_id="past", scene_id="parent", starts_at=900, ends_at=950,
    ))
    refs = runtime.frame_references("portrait", now=1000)
    assert refs == {
        "scene_ids": ("parent",), "program_ids": ("upcoming",),
        "queued_activation_ids": (), "run_ids": (),
    }


def test_frame_references_keep_queued_snapshot_but_ignore_terminal_run_history():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="show", loop=True, cycle_seconds=100,
                            contributions=(media("frame:elsewhere"),)))
    admission = runtime.activate("show", "active", 1000)
    runtime.set_scene(Scene(scene_id="show", revision=2, loop=True, cycle_seconds=100,
                            contributions=(media("frame:portrait"),)))
    runtime.activate("show", "queued", 1000, repeat="queue", expires_at=1050)
    runtime.set_scene(Scene(scene_id="show", revision=3, loop=True, cycle_seconds=100,
                            contributions=(media("frame:elsewhere"),)))
    assert runtime.frame_references("portrait", now=1000) == {
        "scene_ids": (), "program_ids": (),
        "queued_activation_ids": ("queued",), "run_ids": (),
    }

    runtime.cancel(admission.run_id, 1000)
    runtime.set_scene(Scene(scene_id="show", revision=4))
    assert runtime.frame_references("elsewhere", now=1000) == {
        "scene_ids": (), "program_ids": (),
        "queued_activation_ids": (), "run_ids": (),
    }


def test_frame_references_project_expired_queue_without_mutating_runtime():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="show", loop=True,
                            contributions=(media("frame:portrait"),)))
    active_run_id = runtime.activate("show", "active", 1000).run_id
    runtime.set_scene(Scene(scene_id="show", revision=2,
                            contributions=(media("frame:elsewhere"),)))
    runtime.activate("show", "queued", 1000, repeat="queue", expires_at=1010)
    before = runtime.export_state()

    refs = runtime.frame_references("portrait", now=1011)
    assert refs == {
        "scene_ids": (), "program_ids": (),
        "queued_activation_ids": (),
        "run_ids": (active_run_id,),
    }
    assert runtime.export_state() == before


def test_frame_references_project_logically_ended_run_without_mutating_runtime():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="show", duration_seconds=5,
                            contributions=(media("frame:portrait"),)))
    runtime.activate("show", "active", 1000)
    runtime.set_scene(Scene(scene_id="show", revision=2,
                            contributions=(media("frame:elsewhere"),)))
    before = runtime.export_state()

    assert runtime.frame_references("portrait", now=1031) == {
        "scene_ids": (), "program_ids": (),
        "queued_activation_ids": (), "run_ids": (),
    }
    assert runtime.export_state() == before


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
    initial = runtime.advance(midnight - 10)
    december = initial.for_target("frame:left").run_id
    adapter = RecordingActuator()
    adapter.apply(initial, now=midnight - 10, authorized_run_ids={december})
    overlay = runtime.activate("overlay", "manual-overlay", midnight - 10, priority=10).run_id
    before = runtime.export_state()
    before_records = tuple(adapter.records)
    projected = runtime.project(midnight + 25)
    assert runtime.export_state() == before
    assert tuple(adapter.records) == before_records
    with pytest.raises(ValueError, match="current instant"):
        adapter.apply(projected, now=midnight - 10,
                      authorized_run_ids={run.run_id for run in projected.runs})
    assert tuple(adapter.records) == before_records
    assert projected.for_target("frame:left").scene_id == "january"
    assert projected.for_target("frame:left").cycle_position == 25
    assert projected.for_target("actuator:lamp").actuator_value == pytest.approx(25 / 60)
    still_covered = runtime.advance(midnight + 10)
    assert get_run(still_covered, december).phase == "body"  # outgoing cycle ends at +30
    assert get_run(still_covered, overlay).phase == "body"
    assert still_covered.for_target("frame:left").role == "left-speaker"
    assert still_covered.for_target("frame:left").asset_refs == ("left-portrait",)
    assert still_covered.for_target("frame:right").role == "right-speaker"
    assert still_covered.for_target("frame:right").asset_refs == ("right-portrait",)
    assert still_covered.for_target("frame:surround").kind == "black"
    assert still_covered.for_target("actuator:lamp").actuator_value == 0.8
    adapter.apply(still_covered, now=midnight + 10,
                  authorized_run_ids={run.run_id for run in still_covered.runs})
    assert adapter.records[-1].run_id == overlay
    assert adapter.records[-1].value == 0.8
    reveal = runtime.advance(midnight + 25)
    assert reveal == projected
    january = reveal.for_target("frame:left").run_id
    adapter.apply(reveal, now=midnight + 25,
                  authorized_run_ids={run.run_id for run in reveal.runs})
    assert [record.run_id for record in adapter.records] == [december, overlay, january]
    assert [record.at - midnight for record in adapter.records] == [-10, 10, 25]
    assert [record.value for record in adapter.records] == pytest.approx([0, 0.8, 25 / 60])
    assert {record.target for record in adapter.records} == {"actuator:lamp"}
    assert adapter.apply(reveal, now=midnight + 25,
                         authorized_run_ids={run.run_id for run in reveal.runs}) == ()
    later = runtime.advance(midnight + 30)
    assert later.for_target("frame:left").scene_id == "january"
    assert get_run(later, december).phase == "completed"
    adapter.apply(later, now=midnight + 30,
                  authorized_run_ids={run.run_id for run in later.runs})
    assert [record.run_id for record in adapter.records] == [december, overlay, january, january]
    assert [record.at - midnight for record in adapter.records] == [-10, 10, 25, 30]
    assert [record.value for record in adapter.records] == pytest.approx([0, 0.8, 25 / 60, 0.5])


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


def test_a_scene_save_must_move_past_the_stored_revision():
    """A save built from an older copy never silently replaces a newer Scene (slice 3
    §13); an identical retry is idempotent. Mutation probe: drop the guard."""
    runtime = Runtime()
    stored = Scene(scene_id="scene", revision=2, cycle_seconds=10, contributions=(media(),))
    runtime.set_scene(stored)
    for stale in (stored.model_copy(update={"cycle_seconds": 45}),
                  stored.model_copy(update={"revision": 1})):
        with pytest.raises(RuntimeConflict, match="scene_revision_conflict"):
            runtime.set_scene(stale)
        assert runtime.export_state()["scenes"]["scene"]["cycle_seconds"] == 10
    runtime.set_scene(Scene.model_validate(stored.model_dump(mode="json")))  # a retry
    runtime.set_scene(stored.model_copy(update={"revision": 3, "cycle_seconds": 45}))
    assert runtime.export_state()["scenes"]["scene"]["revision"] == 3


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


def test_replace_program_uses_expected_version_and_does_not_advance_runtime():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="first", cycle_seconds=10, loop=True, contributions=(media(),)))
    runtime.set_scene(Scene(scene_id="second", cycle_seconds=10, loop=True,
                           contributions=(media(source="second:v1"),)))
    runtime.set_program(Program(program_id="schedule", scene_id="first", starts_at=20, ends_at=40))
    runtime.advance(5)
    expected = runtime.export_state()
    original = runtime._state.programs["schedule"]
    replacement = Program(program_id="schedule", scene_id="second", starts_at=30, ends_at=50)

    runtime.replace_program(original, replacement, now=10)
    assert runtime._state.programs["schedule"] == replacement
    assert runtime._state.now == 5
    assert runtime._state.runs == Runtime.restore(expected)._state.runs
    assert runtime.replace_program(original, replacement, now=30) is None
    assert runtime.advance(30).for_target("frame:left").scene_id == "second"


@pytest.mark.parametrize("starts_at", [10, 9])
def test_replace_program_refuses_due_or_running_program_without_mutation(starts_at):
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="scene", cycle_seconds=10, loop=True, contributions=(media(),)))
    expected = Program(program_id="schedule", scene_id="scene", starts_at=starts_at, ends_at=20)
    runtime.set_program(expected)
    before = runtime.export_state()
    replacement = expected.model_copy(update={"ends_at": 30})
    with pytest.raises(RuntimeConflict, match="program_started"):
        runtime.replace_program(expected, replacement, now=10)
    assert runtime.export_state() == before


def test_replace_program_refuses_a_new_window_that_has_started():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="scene", cycle_seconds=10, loop=True, contributions=(media(),)))
    expected = Program(program_id="schedule", scene_id="scene", starts_at=20, ends_at=40)
    runtime.set_program(expected)
    with pytest.raises(RuntimeConflict, match="program_window_started"):
        runtime.replace_program(expected, expected.model_copy(update={"starts_at": 10, "ends_at": 30}), 10)


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


# The operator's read (pass 2 slice 3 §8).


def test_run_views_carry_their_program_and_priority():
    assert RunView.model_fields["program_id"].is_required()
    assert RunView.model_fields["priority"].is_required()
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="evening", loop=True, contributions=(media(),)))
    runtime.set_scene(Scene(scene_id="direct", loop=True, contributions=(media("frame:right"),)))
    runtime.set_program(Program(
        program_id="weekday-evenings", scene_id="evening", starts_at=10, ends_at=100, priority=5))
    direct = runtime.activate("direct", "act", 0, priority=2).run_id
    view = runtime.advance(20)
    scheduled = next(run for run in view.runs if run.scene_id == "evening")
    assert (scheduled.program_id, scheduled.priority) == ("weekday-evenings", 5)
    assert (get_run(view, direct).program_id, get_run(view, direct).priority) == (None, 2)


def test_protected_frames_are_served_only_by_the_operator_projection():
    # Never on the scheduler's path: neither view model carries protection.
    for model in (RunView, RuntimeView):
        assert not [name for name in model.model_fields if "protect" in name]
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="guard", protect_frames=True, loop=True,
                            contributions=(media(), lamp())))
    runtime.set_scene(Scene(scene_id="open", loop=True, contributions=(media("frame:right"),)))
    guard = runtime.activate("guard", "guard-act", 0).run_id
    runtime.activate("open", "open-act", 0)
    projection = runtime.operator_projection(5)
    assert projection.protected_frames == {guard: frozenset({"frame:left"})}
    assert {run.run_id for run in projection.current.runs} >= {guard}


def _outcome(runtime, program_id, now):
    return runtime.operator_projection(now).program_outcomes[program_id]


def test_program_outcomes_distinguish_missed_windows_from_warm_restarts():
    scene = Scene(scene_id="evening", cycle_seconds=30, contributions=(media(),))

    # Saved after its window ended, on a warm Runtime: missed.
    late = Runtime()
    late.set_scene(scene)
    late.advance(500)
    late.set_program(Program(program_id="late", scene_id="evening", starts_at=100, ends_at=200))
    missed = _outcome(late, "late", 600)
    assert (missed.status, missed.reason) == ("expired", "missed_window")

    # Ended before the Runtime's first-ever tick: missed.
    cold = Runtime()
    cold.set_scene(scene)
    cold.set_program(Program(program_id="cold", scene_id="evening", starts_at=100, ends_at=200))
    missed = _outcome(cold, "cold", 300)
    assert (missed.status, missed.reason) == ("expired", "missed_window")

    # Central was down across the whole window after a tick: a warm restart
    # catches up logically and the outcome reads admitted, never missed.
    warm = Runtime()
    warm.set_scene(scene)
    warm.advance(0)
    warm.set_program(Program(program_id="warm", scene_id="evening", starts_at=100, ends_at=200))
    projection = warm.operator_projection(300)
    ran = projection.program_outcomes["warm"]
    assert ran.status == "admitted"
    run = get_run(projection.current, ran.run_id)
    assert (run.phase, run.started_at, run.ended_at) == ("completed", 100, 130)

    # Not yet started: no outcome.
    assert _outcome(warm, "warm", 50) is None


def test_a_protected_refusal_is_a_served_program_outcome():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="guard", protect_frames=True, loop=True,
                            contributions=(media(),)))
    runtime.set_scene(Scene(scene_id="evening", loop=True, contributions=(media(),)))
    guard = runtime.activate("guard", "guard-act", 0, priority=5).run_id
    runtime.set_program(Program(program_id="evening", scene_id="evening", starts_at=10, ends_at=100))
    projection = runtime.operator_projection(20)
    refused = projection.program_outcomes["evening"]
    assert (refused.status, refused.reason) == ("rejected", "protected_frames")
    assert refused.blocking_run_id == guard
    assert projection.protected_frames[guard] == frozenset({"frame:left"})


def test_a_refused_activation_names_the_run_that_refused_it():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="guard", protect_frames=True, loop=True,
                            contributions=(media(),)))
    runtime.set_scene(Scene(scene_id="open", loop=True, contributions=(media(),)))
    runtime.set_scene(Scene(scene_id="shy", protect_frames=True, loop=True,
                            contributions=(media("frame:right"),)))
    runtime.set_scene(Scene(scene_id="cover", loop=True, contributions=(media("frame:right"),)))
    guard = runtime.activate("guard", "guard-act", 0).run_id
    cover = runtime.activate("cover", "cover-act", 0, priority=5).run_id
    refused = runtime.activate("open", "open-act", 1)
    assert (refused.reason, refused.blocking_run_id) == ("protected_frames", guard)
    hidden = runtime.activate("shy", "shy-act", 1)
    assert (hidden.reason, hidden.blocking_run_id) == ("protection_not_visible", cover)
    # Every other outcome names no blocker.
    assert runtime.activate("guard", "again", 1).blocking_run_id is None


def test_an_admission_stored_before_the_blocking_run_existed_restores():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="guard", protect_frames=True, loop=True,
                            contributions=(media(),)))
    runtime.set_scene(Scene(scene_id="open", loop=True, contributions=(media(),)))
    runtime.activate("guard", "guard-act", 0)
    runtime.activate("open", "open-act", 1)
    old = json.loads(json.dumps(runtime.export_state()))
    for admission in old["admissions"].values():
        admission.pop("blocking_run_id", None)
    restored = Runtime.restore(old)
    assert restored.activate("open", "open-act", 2).blocking_run_id is None
    exported = restored.export_state()
    assert "blocking_run_id" not in exported["admissions"]["open-act"]
    assert Runtime.restore(exported).export_state() == exported


# Rollback compatibility: the Admission fields the Central build before
# `blocking_run_id` accepts (its models forbid extra keys). A stored state
# must stay inside this set unless a protection refusal has been recorded.
PREVIOUS_ADMISSION_FIELDS = frozenset({"activation_id", "status", "run_id", "reason"})


def _keys_named(value, key):
    if isinstance(value, dict):
        return (key in value) + sum(_keys_named(v, key) for v in value.values())
    if isinstance(value, list):
        return sum(_keys_named(v, key) for v in value)
    return 0


def test_a_state_without_a_protection_refusal_restores_on_the_previous_build():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="guard", protect_frames=True, loop=True,
                            contributions=(media(),)))
    runtime.set_scene(Scene(scene_id="open", loop=True, contributions=(media("frame:right"),)))
    runtime.activate("guard", "guard-act", 0)
    runtime.activate("guard", "guard-again", 1)
    runtime.set_program(Program(program_id="gone", scene_id="open", starts_at=0, ends_at=1))
    runtime.set_program(Program(program_id="later", scene_id="open", starts_at=5, ends_at=50))
    runtime.advance(10)
    exported = json.loads(json.dumps(runtime.export_state()))
    statuses = {a["status"] for a in exported["admissions"].values()}
    assert {"admitted", "ignored"} <= statuses
    assert _keys_named(exported, "blocking_run_id") == 0
    for admission in exported["admissions"].values():
        assert set(admission) <= PREVIOUS_ADMISSION_FIELDS
    assert Runtime.restore(exported).export_state() == exported


def test_a_protection_refusal_is_stored_and_restores_on_this_build():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="guard", protect_frames=True, loop=True,
                            contributions=(media(),)))
    runtime.set_scene(Scene(scene_id="open", loop=True, contributions=(media(),)))
    guard = runtime.activate("guard", "guard-act", 0).run_id
    runtime.activate("open", "open-act", 1)
    exported = json.loads(json.dumps(runtime.export_state()))
    assert exported["admissions"]["open-act"]["blocking_run_id"] == guard
    assert "blocking_run_id" not in exported["admissions"]["guard-act"]
    restored = Runtime.restore(exported)
    assert restored.activate("open", "open-act", 2).blocking_run_id == guard
    assert restored.export_state()["admissions"] == exported["admissions"]


def test_the_operator_read_serves_a_day_of_history_and_stores_everything():
    day = OPERATOR_HISTORY_SECONDS
    runtime = Runtime()
    for scene_id, target in (("old", "frame:a"), ("recent", "frame:b"), ("live", "frame:c")):
        runtime.set_scene(Scene(scene_id=scene_id, loop=True, contributions=(media(target),)))
    old = runtime.activate("old", "old-act", 0).run_id
    runtime.cancel(old, 10)
    runtime.set_program(Program(program_id="old-window", scene_id="old", starts_at=20, ends_at=60))
    recent = runtime.activate("recent", "recent-act", day).run_id
    runtime.cancel(recent, day + 100)
    live = runtime.activate("live", "live-act", day + 100).run_id
    runtime.set_program(Program(
        program_id="recent-window", scene_id="recent", starts_at=day + 150, ends_at=day + 300))
    runtime.set_program(Program(
        program_id="later-window", scene_id="live", starts_at=day + 900, ends_at=day + 1000))
    before = runtime.export_state()

    now = day + 200
    projection = runtime.operator_projection(now)
    served = {run.run_id for run in projection.current.runs}
    assert {recent, live} <= served  # ended 100 s ago, and live
    assert old not in served  # ended more than a day ago
    assert set(projection.program_outcomes) == {"recent-window", "later-window"}
    assert projection.program_outcomes["later-window"] is None

    # A read filter only: stored state and every other read keep the old Run.
    assert runtime.export_state() == before
    assert old in {run.run_id for run in runtime.project(now).runs}


def test_delete_scene_checks_revision_and_removes_only_an_unused_definition():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="unused", revision=3))
    before = runtime.export_state()
    with pytest.raises(RuntimeConflict, match="scene_revision_conflict"):
        runtime.delete_scene("unused", 2)
    assert runtime.export_state() == before
    runtime.delete_scene("unused", 3)
    assert "unused" not in runtime.export_state()["scenes"]
    with pytest.raises(RuntimeConflict, match="scene_missing"):
        runtime.delete_scene("unused", 3)


@pytest.mark.parametrize("dependency", ["program", "run", "queued", "child"])
def test_delete_scene_refuses_each_live_or_authored_dependency(dependency):
    runtime = Runtime()
    target = Scene(scene_id="target", loop=True, contributions=(media(),))
    runtime.set_scene(target)
    if dependency == "program":
        runtime.set_program(Program(program_id="uses-target", scene_id="target", starts_at=10, ends_at=20))
    elif dependency == "run":
        runtime.activate("target", "active", 0)
    elif dependency == "queued":
        runtime.activate("target", "active", 0)
        runtime.activate("target", "queued", 1, repeat="queue", expires_at=30)
    else:
        runtime.set_scene(Scene(scene_id="parent", children=(Child(scene=target),)))

    before = runtime.export_state()
    with pytest.raises(RuntimeConflict) as raised:
        runtime.delete_scene("target", 1)
    assert raised.value.code == "scene_in_use"
    assert runtime.export_state() == before
    assert ("uses-target" in getattr(raised.value, "program_ids", ())) == (dependency == "program")
    assert bool(getattr(raised.value, "run_ids", ())) == (dependency in {"run", "queued"})
    assert bool(getattr(raised.value, "queued_activation_ids", ())) == (dependency == "queued")
    assert ("parent" in getattr(raised.value, "scene_ids", ())) == (dependency == "child")


def test_deleting_definition_does_not_rewrite_ended_run_snapshot():
    runtime = Runtime()
    runtime.set_scene(Scene(scene_id="historical", loop=True, contributions=(media(),)))
    run_id = runtime.activate("historical", "historical-activation", 0).run_id
    runtime.cancel(run_id, 1)
    snapshot = runtime.export_state()["runs"][run_id]["scene"]
    runtime.delete_scene("historical", 1)
    assert runtime.export_state()["runs"][run_id]["scene"] == snapshot


KEEP = {"after_end": "keep_this_photo"}


def _kept_scene(**scene):
    return Scene(scene_id="kept", cycle_seconds=10, loop=True, contributions=(
        media(fade_in_seconds=1, fade_out_seconds=2, **KEEP),), **scene)


def _fades(view):
    return [(intent.phase, intent.fade_in_seconds, intent.fade_out_seconds)
            for intent in view.contributions]


def test_a_kept_photo_plans_no_fade_out_in_its_final_cycle_only():
    """The final cycle of a kept photo (the Scene stops at its end and nothing on the Frame
    follows) holds full strength: a Program's end, the Scene's duration and Finish each make a
    cycle final; earlier cycles, an ending on the Frame, and a photo not kept keep the authored
    fade-out. Mutation probe: drop the final-cycle rule (the fade-out stays 2)."""
    runtime = Runtime()
    runtime.set_scene(_kept_scene())
    runtime.set_program(Program(program_id="evening", scene_id="kept", starts_at=0, ends_at=20))
    assert _fades(runtime.advance(5)) == [("body", 1, 2)]
    assert _fades(runtime.advance(15)) == [("body", 1, 0)]

    timed = Runtime()
    timed.set_scene(_kept_scene(duration_seconds=15))
    timed.activate("kept", "timed", 0)
    assert _fades(timed.advance(5)) == [("body", 1, 2)]
    assert _fades(timed.advance(12)) == [("body", 1, 0)]

    finished = Runtime()
    finished.set_scene(_kept_scene())
    run = finished.activate("kept", "finished", 0).run_id
    assert _fades(finished.advance(3)) == [("body", 1, 2)]
    assert _fades(finished.finish(run, 4)) == [("body", 1, 0)]

    ending = Runtime()
    ending.set_scene(_kept_scene(outro_seconds=4, outro_contributions=(
        Contribution(target="frame:left", kind="black"),)))
    run = ending.activate("kept", "ending", 0).run_id
    assert _fades(ending.finish(run, 4)) == [("body", 1, 2)]

    plain = Runtime()
    plain.set_scene(Scene(scene_id="plain", cycle_seconds=10, contributions=(
        media(fade_in_seconds=1, fade_out_seconds=2),)))
    plain.activate("plain", "plain", 0)
    assert _fades(plain.advance(5)) == [("body", 1, 2)]


def test_a_kept_photo_followed_on_its_frame_keeps_its_fade_out():
    """The final-cycle hold is only for a photo nothing follows: a Program starting at its end,
    or a Run playing beneath it, keeps the authored fade-out; a follower that appears later
    revises the hold away. Mutation probe: ignore what follows (the fade-out is 0)."""
    def kept(runtime):
        return [(i.fade_in_seconds, i.fade_out_seconds) for i in runtime.advance(
            runtime._state.now).contributions if i.scene_id == "kept"]

    following = Runtime()
    following.set_scene(_kept_scene())
    following.set_scene(Scene(scene_id="next", cycle_seconds=10, contributions=(media(),)))
    following.set_program(Program(program_id="evening", scene_id="kept", starts_at=0, ends_at=10))
    following.advance(5)
    assert kept(following) == [(1, 0)]
    following.set_program(Program(program_id="night", scene_id="next", starts_at=10, ends_at=20))
    assert kept(following) == [(1, 2)]

    beneath = Runtime()
    beneath.set_scene(_kept_scene())
    beneath.set_scene(Scene(scene_id="under", cycle_seconds=10, loop=True, contributions=(media(),)))
    beneath.activate("under", "under", 0)
    beneath.set_program(Program(program_id="evening", scene_id="kept", starts_at=0, ends_at=10))
    beneath.advance(5)
    assert kept(beneath) == [(1, 2)]


def test_looking_at_what_follows_a_kept_photo_spends_the_callers_budget():
    """The projection that decides a final-cycle hold is bounded by the caller's `max_events`:
    the same advance that fits the budget without a kept photo exceeds it with one. Mutation
    probe: give the projection its own budget (the kept advance succeeds)."""
    def advance(after_end):
        runtime = Runtime()
        runtime.set_scene(Scene(scene_id="kept", cycle_seconds=10, loop=True, contributions=(
            media(fade_in_seconds=1, fade_out_seconds=2, after_end=after_end),)))
        runtime.set_program(Program(program_id="evening", scene_id="kept", starts_at=0,
                                    ends_at=10))
        return runtime.advance(5, max_events=2)

    from central.runtime import RuntimeBudgetExceeded

    assert advance("leave_as_is").contributions
    with pytest.raises(RuntimeBudgetExceeded):
        advance("keep_this_photo")


def test_an_ending_keeps_nothing_only_when_nothing_plays_beneath_it():
    """Owner, 2026-10-10: "When the top show ends, it gets out of the way and the show
    underneath takes over." An ending's `keep_nothing` becomes `leave_as_is` when another Run
    plays on its Frame at its end. Mutation probe: keep the authored after-state."""
    def ending(beneath):
        runtime = Runtime()
        runtime.set_scene(Scene(scene_id="top", cycle_seconds=10, outro_seconds=4,
                                contributions=(media(),), outro_contributions=(
                                    Contribution(target="frame:left", kind="black",
                                                 after_end="keep_nothing"),)))
        if beneath:
            runtime.set_scene(Scene(scene_id="under", cycle_seconds=10, loop=True,
                                    contributions=(media(),)))
            runtime.activate("under", "under", 0)
        runtime.activate("top", "top", 0)
        return [i.after_end for i in runtime.advance(11).contributions if i.phase == "outro"]

    assert ending(beneath=False) == ["keep_nothing"]
    assert ending(beneath=True) == ["leave_as_is"]


def test_a_photo_is_kept_only_when_no_run_beneath_it_plays_on():
    """The same principle for Keep the last photo up: a top Scene's photo keeps nothing of its
    own while a Run beneath it plays on its Frame at its cycle's end, so the show underneath
    keeps its photo; a Program that follows it (on top, not beneath) does not change it.
    Mutation probes: keep the top's photo whatever plays beneath; step aside for any
    follower."""
    def after_ends(other):
        runtime = Runtime()
        runtime.set_scene(_kept_scene())
        runtime.set_scene(Scene(scene_id="other", cycle_seconds=10, loop=True,
                                contributions=(media(after_end="keep_this_photo"),)))
        if other == "beneath":
            runtime.activate("other", "other", 0)
        runtime.set_program(Program(program_id="evening", scene_id="kept", starts_at=0,
                                    ends_at=20))
        if other == "after":
            runtime.set_program(Program(program_id="night", scene_id="other", starts_at=20,
                                        ends_at=30))
        return [(i.scene_id, i.after_end) for i in runtime.advance(15).contributions]

    assert after_ends(None) == [("kept", "keep_this_photo")]
    assert after_ends("after") == [("kept", "keep_this_photo")]
    assert after_ends("beneath") == [("other", "keep_this_photo"), ("kept", "leave_as_is")]
