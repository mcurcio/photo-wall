import hashlib

import pytest

from central.catalog import Candidate, CatalogSnapshot
from central.planner import PlannerLimits, PlanningBudgetExceeded, PlanningError, eligible, project
from central.runtime import Child, Contribution, RecordingActuator, Runtime, Scene
from contracts.models import FrameProfile, OutputBinding, Plan, Variant


def candidate(asset="a", *, width=1200, height=1800, kind="image", prepared=True, captured=0,
              variant_width=None, variant_height=None):
    variant = Variant(
        sha256=hashlib.sha256(asset.encode()).hexdigest(), size=10,
        media_type="video/mp4" if kind == "video" else "image/jpeg",
        width=variant_width or width, height=variant_height or height,
        duration=10 if kind == "video" else None,
    ) if prepared else None
    return Candidate(asset_id=asset, kind=kind, original_width=width, original_height=height,
                     captured_at=captured, variant=variant)


def binding(frame="left", output="hdmi-1", *, width=1080, height=1920, diagonal=40,
            generation=1, video=True):
    return OutputBinding(output_id=output, frame_id=frame, generation=generation,
                         profile=FrameProfile(width_px=width, height_px=height,
                                              diagonal_inches=diagonal, video=video))


def runtime(*, cycle=10, contributions=None, children=()):
    instance = Runtime()
    instance.set_scene(Scene(
        scene_id="scene", cycle_seconds=cycle, loop=True, children=children,
        contributions=contributions if contributions is not None else (
            Contribution(target="frame:left", source_refs=("holiday:1",)),
        ),
    ))
    instance.activate("scene", "run", 0)
    return instance


def propose(instance, *, now=0, horizon=25, assets=None, snapshots=None, bindings=None,
            authored=None, locks=None, limits=None):
    options = dict(
        bindings_by_player=bindings or {"player-one": (binding(),)},
        catalog_snapshots=snapshots if snapshots is not None else {
            "holiday:1": CatalogSnapshot(source_ref="holiday:1", refreshed_at=now,
                                         candidates=tuple(assets or [candidate()])),
        },
        authored_candidates=authored or {}, locked_assignments=locks or {},
        horizon_seconds=horizon,
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
    empty = propose(instance, now=5, snapshots={
        "holiday:1": CatalogSnapshot(source_ref="holiday:1", refreshed_at=5, candidates=()),
    }, locks={locked.assignment_id: locked})
    assert layers(empty) == (locked,)
    assert {d.code for d in empty.diagnostics} >= {"source_empty", "no_eligible_candidates"}


@pytest.mark.parametrize("kind,width,height,diagonal,video,expected", [
    ("image", 2000, 1000, 20, True, False),
    ("image", 1000, 2000, 50, True, True),
    ("image", 1000, 1000, 50, True, True),
    ("video", 1280, 720, 40, True, False),
    ("video", 1280, 720, 39, True, True),
    ("video", 1920, 1080, 40, True, True),
    ("video", 1920, 1080, 20, False, False),
])
def test_original_eligibility_precedes_derivative_dimensions(kind, width, height, diagonal,
                                                           video, expected):
    asset = candidate(kind=kind, width=width, height=height,
                      variant_width=3840, variant_height=2160)
    profile = binding(diagonal=diagonal, video=video).profile
    assert eligible(asset, profile) is expected


def test_upscaled_720p_is_never_sent_to_large_frame_and_authored_alternative_stays_on_role():
    low = candidate("low", kind="video", width=1280, height=720,
                    variant_width=3840, variant_height=2160)
    proper = candidate("proper", kind="video", width=1920, height=1080)
    instance = runtime(contributions=(Contribution(
        target="frame:left", asset_refs=("low", "proper"), role="left-speaker",
    ),))
    result = propose(instance, authored={"low": low, "proper": proper},
                     bindings={"player-one": (binding(), binding("right", "hdmi-2"))})
    assert {layer.frame_id for layer in layers(result)} == {"left"}
    assert all(layer.variant == proper.variant for layer in layers(result))
    assert all(selection.asset_id == "proper" for selection in result.selections)
    missing = propose(instance, authored={"low": low})
    assert layers(missing) == ()
    assert "authored_no_eligible_alternative" in {d.code for d in missing.diagnostics}


@pytest.mark.parametrize("status", ["unavailable", "permission", "incompatible"])
def test_failed_sources_do_not_use_stale_candidates_or_masquerade_as_empty(status):
    instance = runtime()
    result = propose(instance, snapshots={
        "holiday:1": CatalogSnapshot(source_ref="holiday:1", refreshed_at=0,
                                     status=status, candidates=(candidate(),)),
    })
    assert layers(result) == ()
    codes = {diagnostic.code for diagnostic in result.diagnostics}
    assert f"source_{status}" in codes
    assert "source_empty" not in codes
    first = layers(propose(instance))[0]
    locked = propose(instance, snapshots={
        "holiday:1": CatalogSnapshot(source_ref="holiday:1", refreshed_at=0, status=status),
    }, locks={first.assignment_id: first})
    assert layers(locked) == (first,)


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
    assert {d.code for d in result.diagnostics} == {"preparation_pending"}
    missing = propose(instance, snapshots={})
    assert "source_missing" in {d.code for d in missing.diagnostics}


def test_horizon_preserves_current_full_cycle_origins_and_fade_under_locks():
    instance = runtime(contributions=(Contribution(
        target="frame:left", source_refs=("holiday:1",), opacity=0.8,
        fade_in_seconds=2, fade_out_seconds=3,
    ),))
    at_seven = propose(instance, now=7, horizon=10)
    first, second = layers(at_seven)
    assert (first.start, first.end, first.media_origin) == (0, 10, 0)
    assert (first.opacity, first.fade_in, first.fade_out) == (0.8, 2, 3)
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
        Contribution(target="frame:left", kind="black", fade_out_seconds=2),
    )))
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


@pytest.mark.parametrize("limits,overrides", [
    (PlannerLimits(max_candidates=1), {"assets": [candidate("a"), candidate("b")]}),
    (PlannerLimits(max_sources=1), {"snapshots": {
        "holiday:1": CatalogSnapshot(source_ref="holiday:1", refreshed_at=0),
        "other:1": CatalogSnapshot(source_ref="other:1", refreshed_at=0),
    }}),
    (PlannerLimits(max_assignments=1), {}),
    (PlannerLimits(max_acquisitions=1), {"assets": [candidate("a", prepared=False),
                                                candidate("b", prepared=False)]}),
    (PlannerLimits(max_horizon_seconds=5), {}),
    (PlannerLimits(max_cycle_seconds=5), {}),
    (PlannerLimits(max_events=1), {}),
    (PlannerLimits(max_diagnostics=1), {"snapshots": {}}),
])
def test_each_planning_budget_fails_without_mutation(limits, overrides):
    instance = runtime()
    before = instance.export_state()
    with pytest.raises(PlanningBudgetExceeded):
        propose(instance, limits=limits, **overrides)
    assert instance.export_state() == before


def test_invalid_input_and_prepared_variant_fail_safely():
    instance = runtime()
    with pytest.raises(PlanningError, match="one current binding"):
        propose(instance, bindings={"one": (binding(),), "two": (binding(),)})
    with pytest.raises(PlanningError, match="immutable source"):
        propose(instance, snapshots={
            "holiday:1": CatalogSnapshot(source_ref="wrong:1", refreshed_at=0),
        })
    for invalid in (0, -1, float("nan"), float("inf"), True):
        with pytest.raises(PlanningError):
            propose(instance, horizon=invalid)
    bad = candidate().model_copy(update={"variant": candidate(kind="video").variant})
    result = propose(instance, assets=[bad])
    assert layers(result) == ()
    assert "variant_incompatible" in {d.code for d in result.diagnostics}


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
    from central.runtime import Program

    instance = runtime()
    instance.set_scene(Scene(scene_id="next", cycle_seconds=10, contributions=(
        Contribution(target="frame:left", source_refs=("holiday:1",)),
    )))
    instance.set_program(Program(program_id="p", scene_id="next", starts_at=10, ends_at=20))
    first = propose(instance, horizon=15)
    lock = next(layer for layer in layers(first) if layer.run_id != layers(first)[0].run_id)
    instance.set_scene(Scene(scene_id="overlay", cycle_seconds=30, contributions=(
        Contribution(target="frame:left", kind="black"),
    )))
    instance.activate("overlay", "overlay", 5)
    actual = propose(instance, now=11, horizon=4, assets=[candidate("different")],
                     locks={lock.assignment_id: lock})
    replacement = next(layer for layer in layers(actual) if layer.assignment_id == lock.assignment_id)
    assert replacement.variant == lock.variant
    assert replacement.root_order > lock.root_order
    assert replacement.model_dump(exclude={"root_order"}) == lock.model_dump(exclude={"root_order"})


def test_authored_reference_input_budget_is_explicit():
    instance = runtime(contributions=(Contribution(target="frame:left", source_refs=("holiday:1",) * 3),))
    with pytest.raises(PlanningBudgetExceeded, match="authored source reference"):
        propose(instance, limits=PlannerLimits(max_sources=2))


def test_source_health_remains_observable_when_entire_projection_is_locked():
    instance = runtime()
    lock = layers(propose(instance, horizon=5))[0]
    result = propose(instance, horizon=5, locks={lock.assignment_id: lock}, snapshots={
        "holiday:1": CatalogSnapshot(source_ref="holiday:1", refreshed_at=0, status="unavailable"),
    })
    assert layers(result) == (lock,)
    assert "source_unavailable" in {diagnostic.code for diagnostic in result.diagnostics}
