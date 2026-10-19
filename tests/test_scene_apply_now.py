"""Save and apply now for Scenes (cross-fade delivery, slice 2; design /Volumes/Dock/tmp/cross-fade
design.md, "SceneWrites"): a plain Save stores a Scene and answers what on the wall still plays an
earlier version of it (R4: "Authored configuration edits default to the next Run"); Save and apply
now replaces each such Run at once, its successor admitted over it before it finishes beneath it
(design rule 3: a refused successor leaves its Run playing), and queued Show now activations take
the new copy. The Runtime tests run without a database; the route test runs Central's real routes
on the test database."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from support.house_time import UTC_HOUSE, set_house, window_program

from central.runtime import (
    Contribution,
    EarlierVersion,
    Runtime,
    Scene,
    SceneOnWall,
    SceneWriteRefused,
)

FRAME = "frame:left"


def _scene(revision=1, *, duration=None, protect=False, frame=FRAME) -> Scene:
    return Scene(scene_id="evening", revision=revision, cycle_seconds=60, loop=True,
                 duration_seconds=duration, protect_frames=protect,
                 contributions=(Contribution(target=frame, source_refs=("holiday:1",)),))


def _playing(runtime: Runtime, now: float) -> tuple[Runtime, object, object]:
    """"evening" revision 1 playing under Show now (an hour, from 30 s) and under a Schedule
    (00:01-01:40; a Schedule's window is admitted whatever else plays its Scene)."""
    runtime.set_scene(_scene(duration=3600))
    runtime.activate("evening", "show", 30)
    runtime.create_program(window_program("evenings", "evening", 60, 6000), 30)
    view = runtime.advance(now)
    schedule = next(run for run in view.runs if run.program_id == "evenings")
    show = next(run for run in view.runs if run.activation_id == "show")
    return runtime, schedule, show


def _root(runtime: Runtime, run_id: str):
    return runtime._state.runs[run_id]


def test_save_lists_what_plays_an_earlier_version_and_apply_now_replaces_it_at_once():
    """The red test (Runtime level): a Scene playing under a Schedule and under Show now. Saving
    revision 2 lists both as playing an earlier version and changes nothing on the wall; Save and
    apply now empties the list, each successor admitted at once on revision 2, drawn over its
    predecessor, which finishes gracefully beneath it. The Schedule's successor keeps its window
    end; Show now's ends at its original start plus the new duration.

    Mutation probes: finish before admitting (the shared step) -> a refusal stops the Scene (the
    refusal test below); `duration_from` dropped -> Show now's successor ends an hour after now."""
    runtime, schedule, show = _playing(Runtime(house=UTC_HOUSE), 300)
    saved = runtime.save_scene(_scene(2, duration=1800), 400)
    assert isinstance(saved, SceneOnWall)
    assert saved.scene.revision == 2
    assert {(ref.run_id, ref.scene_revision, ref.started_by) for ref in saved.earlier_version.playing} == {
        (schedule.run_id, 1, "schedule"), (show.run_id, 1, "show_now")}
    assert saved.earlier_version.waiting == ()
    assert runtime.advance(400).for_target(FRAME).scene_revision == 1          # R4: nothing changed

    applied = runtime.save_scene_and_apply_now(_scene(2, duration=1800), 500)
    assert applied.earlier_version.playing == () and applied.earlier_version.waiting == ()
    view = runtime.advance(500)
    successors = [run for run in view.runs if run.parent_id is None and run.ended_at is None
                  and run.scene_revision == 2]
    assert len(successors) == 2
    for predecessor in (schedule, show):
        old = _root(runtime, predecessor.run_id)
        assert old.finish_requested_at == 500                   # finishes gracefully beneath it
        new = next(_root(runtime, run.run_id) for run in successors
                   if _root(runtime, run.run_id).program_id == old.program_id)
        assert new.started_at == 500 and new.priority == old.priority
        assert new.root_order == old.root_order                 # takes its place in the order
    assert view.for_target(FRAME).run_id in {run.run_id for run in successors}   # drawn over it
    on_schedule = next(_root(runtime, run.run_id) for run in successors if run.program_id == "evenings")
    assert on_schedule.program_ends_at == 6000                  # the window's end
    assert on_schedule.stop_at == 60 + 1800                     # the new duration, from the window
    show_now = next(_root(runtime, run.run_id) for run in successors if run.program_id is None)
    assert show_now.stop_at == 30 + 1800
    assert view.for_target(FRAME).scene_revision == 2


def test_an_identical_retry_changes_nothing_and_identities_are_per_revision():
    """Pressing Save and apply now again (or on an unchanged Scene) replaces only what is earlier:
    nothing. A successor's identity is deterministic and distinct per Scene revision, and a
    Schedule's keeps its window's start last (`Runtime._playing_window` reads it)."""
    runtime, schedule, show = _playing(Runtime(house=UTC_HOUSE), 300)
    runtime.save_scene_and_apply_now(_scene(2), 400)
    before = runtime.export_state()
    again = runtime.save_scene_and_apply_now(_scene(2), 400)
    assert again.earlier_version.playing == ()
    assert runtime.export_state() == before
    second = {run.activation_id for run in runtime._state.runs.values()
              if run.active and run.scene.revision == 2 and run.finish_requested_at is None}
    assert second == {f"{schedule.activation_id.rsplit(':', 1)[0]}:scene-2:60", "show:scene-2"}
    runtime.save_scene_and_apply_now(_scene(3), 450)
    third = {run.activation_id for run in runtime._state.runs.values()
             if run.active and run.scene.revision == 3}
    assert third == {f"{schedule.activation_id.rsplit(':', 1)[0]}:scene-3:60", "show:scene-3"}
    program = runtime._state.programs["evenings"]
    assert runtime.program_windows_between(program, 0, 6000)[0][0].start == 60


def test_a_refused_successor_leaves_its_run_playing_and_listed():
    """Design rule 3: Save and apply now never stops a Scene. Another Run protecting the Frame
    refuses the successor; the Run keeps playing its earlier version and the answer says so."""
    runtime = Runtime(house=UTC_HOUSE)
    runtime.set_scene(_scene())
    runtime.set_scene(Scene(scene_id="guard", cycle_seconds=60, loop=True, protect_frames=True,
                            contributions=(Contribution(target=FRAME, source_refs=("guard:1",)),)))
    show = runtime.activate("evening", "show", 0)
    runtime.activate("guard", "guard", 10, priority=5)
    applied = runtime.save_scene_and_apply_now(_scene(2), 100)
    assert [ref.run_id for ref in applied.earlier_version.playing] == [show.run_id]
    old = _root(runtime, show.run_id)
    assert old.active and old.finish_requested_at is None and old.scheduled_finish_at is None
    assert "show:scene-2" not in runtime._state.admissions


def test_queued_activations_take_the_new_copy():
    """A queued Show now holds the copy it was queued with: a plain Save reports it waiting, Save
    and apply now swaps its copy."""
    runtime = Runtime(house=UTC_HOUSE)
    runtime.set_scene(_scene())
    runtime.activate("evening", "first", 10)
    runtime.activate("evening", "later", 20, repeat="queue", expires_at=10_000)
    saved = runtime.save_scene(_scene(2), 30)
    assert saved.earlier_version.waiting == ("later",)
    assert [ref.started_by for ref in saved.earlier_version.playing] == ["show_now"]
    applied = runtime.save_scene_and_apply_now(_scene(2), 40)
    assert applied.earlier_version == EarlierVersion()
    assert runtime._state.queue[0].scene.revision == 2


def test_the_default_scene_restarts_itself_and_is_never_listed():
    runtime = Runtime(house=UTC_HOUSE)
    runtime.set_scene(_scene())
    runtime.set_default_scene("evening", 0)
    for write, now in ((runtime.save_scene, 30), (runtime.save_scene_and_apply_now, 40)):
        revision = runtime._state.scenes["evening"].revision + 1
        assert write(_scene(revision), now).earlier_version == EarlierVersion()
        defaults = [run for run in runtime._state.runs.values()
                    if run.active and run.finish_requested_at is None]
        assert [run.scene.revision for run in defaults] == [revision]
        # It restarts under the Default Scene's own identity (`_stale_defaults`); Save and apply
        # now admits no successor for it, even one that would finish at once.
        assert defaults[0].activation_id.startswith(f"default:evening:1:{revision}:")
        assert not [run.activation_id for run in runtime._state.runs.values()
                    if ":scene-" in (run.activation_id or "")]


def test_a_stale_revision_is_refused_changed_with_the_stored_scene():
    runtime = Runtime(house=UTC_HOUSE)
    runtime.save_scene(_scene(2), 0)
    for write in (runtime.save_scene, runtime.save_scene_and_apply_now):
        with pytest.raises(SceneWriteRefused) as raised:
            write(_scene(2).model_copy(update={"cycle_seconds": 45}), 10)
        assert (raised.value.reason, raised.value.current) == ("changed", _scene(2))


def test_save_and_apply_now_through_the_routes(registry):
    """The red test (DB and HTTP): through Central's routes and the stored Runtime, a Scene playing
    under a Schedule and under Show now; PUT revision 2 answers both as playing an earlier
    version; POST apply-now answers nothing earlier, each successor admitted at once over its
    predecessor, which finishes beneath it; a retry answers the same and changes nothing; a stale
    revision answers 409 "changed" with the stored Scene."""
    from test_operator_frames import ADMIN, AUTH

    from central.app import create_app
    set_house(registry.db, UTC_HOUSE)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    store = app.state.coordinator.runtime
    body = _scene(duration=3600).model_dump(mode="json")
    with TestClient(app) as client:
        assert client.put("/v1/operator/scenes/evening", headers=AUTH, json=body).json() == {
            "scene": body, "earlier_version": {"playing": [], "waiting": []}}
        assert client.post("/v1/operator/activations", headers=AUTH, json={
            "scene_id": "evening", "activation_id": "show"}).json()["status"] == "admitted"
        store.command_current("create_program", window_program("evenings", "evening", 960, 6000))
        registry.clock.advance(60)
        revised = {**body, "revision": 2, "duration_seconds": 1800}
        saved = client.put("/v1/operator/scenes/evening", headers=AUTH, json=revised)
        assert saved.status_code == 200, saved.text
        playing = saved.json()["earlier_version"]["playing"]
        assert sorted(ref["started_by"] for ref in playing) == ["schedule", "show_now"]
        assert {ref["scene_revision"] for ref in playing} == {1}

        registry.clock.advance(60)
        now = registry.clock.utc()
        applied = client.post("/v1/operator/scenes/evening/apply-now", headers=AUTH, json=revised)
        assert applied.json() == {"scene": revised, "earlier_version": {"playing": [], "waiting": []}}
        state = store.read().export_state()
        for ref in playing:
            assert state["runs"][ref["run_id"]]["finish_requested_at"] == now
        successors = [run for run in state["runs"].values()
                      if run["scene"]["revision"] == 2 and run["phase"] == "body"]
        assert sorted(run["started_at"] for run in successors) == [now, now]
        assert sorted(run["root_order"] for run in successors) == sorted(
            state["runs"][ref["run_id"]]["root_order"] for ref in playing)

        assert client.post("/v1/operator/scenes/evening/apply-now", headers=AUTH,
                           json=revised).json() == applied.json()
        assert store.read().export_state()["runs"] == state["runs"]
        stale = client.post("/v1/operator/scenes/evening/apply-now", headers=AUTH,
                            json={**revised, "cycle_seconds": 45})
        assert (stale.status_code, stale.json()) == (409, {"error": "changed", "current": revised})
        assert client.post("/v1/operator/scenes/evening/apply-now", json=revised).status_code == 401


def test_the_authored_save_and_apply_now_replaces_in_its_own_transaction(registry):
    """The hand-placed Scene's Save and apply now: the photos are authored and the Scene saved and
    applied in one transaction (Coordinator.configure_authored_scene_and_apply_now), answering the
    `SceneOnWall` with the authored refs."""
    from test_authored_media import setup_source
    from test_authored_scene import authored_scene, bound_portrait_frame

    from central.coordination import Coordinator
    bound_portrait_frame(registry)
    _repo, spec, originals = setup_source(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    asset = originals[0].asset_id
    saved = coordinator.configure_authored_scene(authored_scene(asset), spec.source_ref, (asset,))
    assert saved["earlier_version"] == {"playing": [], "waiting": []}
    show = coordinator.runtime.command_current("activate", "authored-scene", "show")
    registry.clock.advance(10)      # inside the Scene's one 30 s photo: the Run still plays
    revised = authored_scene(asset, revision=2)
    applied = coordinator.configure_authored_scene_and_apply_now(revised, spec.source_ref, (asset,))
    assert applied == {"scene": revised.model_dump(mode="json"),
                       "earlier_version": {"playing": [], "waiting": []},
                       "asset_refs": [asset], "created": 0}
    state = coordinator.runtime.read().export_state()
    assert state["runs"][show.run_id]["finish_requested_at"] == registry.clock.utc()
    assert state["admissions"]["show:scene-2"]["status"] == "admitted"


def test_a_replacement_takes_its_predecessors_place_under_a_later_show_now():
    """A Run replaced by Save and apply now takes its predecessor's place in the stacking order:
    a Schedule plays "evening", the operator then shows "party" at the same priority (on top: it
    was admitted later), and Save and apply now of "evening" leaves "party" on top, its successor
    drawn over only the Run it replaces.

    Mutation probe: a fresh root order for the successor (`_replace` without `root_order`) ->
    "evening" covers the operator's Show now; the predecessor's admission order (no place above
    it) -> at the cut the earlier version stays on top of its successor."""
    runtime = Runtime(house=UTC_HOUSE)
    runtime.set_scene(_scene())
    runtime.set_scene(Scene(scene_id="party", cycle_seconds=60, loop=True,
                            contributions=(Contribution(target=FRAME, source_refs=("party:1",)),)))
    runtime.create_program(window_program("evenings", "evening", 60, 6000), 30)
    runtime.advance(100)
    runtime.activate("party", "party", 200, priority=1)
    assert runtime.advance(250).for_target(FRAME).scene_id == "party"
    # At a cut (300): the replaced Run's new turn and its successor's first photo start together.
    runtime.save_scene_and_apply_now(_scene(2), 300)
    view = runtime.advance(310)
    assert view.for_target(FRAME).scene_id == "party"
    evening = [intent for intent in view.contributions if intent.scene_id == "evening"]
    assert max(evening, key=lambda intent: intent.precedence).scene_revision == 2
