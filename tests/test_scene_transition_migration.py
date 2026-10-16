"""098 moves every Scene's fade from its Contributions to the Scene's Transition.

The snapshot is written as the previous build stored it (each Contribution carrying the fade the
console wrote: half in and half out between photos, a "Fades out" ending's photo half in and the
rest out, a black ending none), into the real runtime_state, and the migration's SQL runs on it:
stored Scenes become a cross-fade at the next revision (owner, 2026-10-15, asked whether saved
Scenes become cross-fade: "Yes"), while the copies Runs and queued activations captured become
fade out and in, whose derived fades are the fields they carried, so their offered layers do not
change; and Central boots on it and offers the same layers it offered before.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from support.house_time import UTC_HOUSE, set_house
from test_exposed_settings_roundtrip import _rig, _save_and_start, _scene
from test_registry import ADMIN

from central.app import create_app
from central.runtime import Child, Contribution, Runtime, Scene, Transition
from central.runtime_store import RuntimeStore

MIGRATION = Path(__file__).parents[1] / "central/migrations/098_scene_transition.sql"
THROUGH_BLACK = Transition(style="fade_out_and_in", seconds=3)


def _previous_scene(scene: dict) -> dict:
    """`scene` (exported by this build, a fade out and in) as the previous build stored it: no
    Transition, and its seconds on each Contribution as the console wrote them."""
    half = scene["transition"]["seconds"] / 2
    outro = scene["outro_seconds"]
    old = {key: value for key, value in scene.items() if key != "transition"}
    old["contributions"] = [{**c, "fade_in_seconds": half, "fade_out_seconds": half}
                            for c in scene["contributions"]]
    old["outro_contributions"] = [
        {**c, "fade_in_seconds": 0, "fade_out_seconds": 0} if c["kind"] == "black"
        else {**c, "fade_in_seconds": half, "fade_out_seconds": outro - half}
        for c in scene["outro_contributions"]]
    old["children"] = [{**child, "scene": _previous_scene(child["scene"])}
                       for child in scene["children"]]
    return old


def _previous_snapshot(state: dict) -> dict:
    return {**state,
            "scenes": {key: _previous_scene(scene) for key, scene in state["scenes"].items()},
            "runs": {key: {**run, "scene": _previous_scene(run["scene"])}
                     for key, run in state["runs"].items()},
            "queue": [{**item, "scene": _previous_scene(item["scene"])} for item in state["queue"]]}


def _media(target="frame:left", **fields):
    return Contribution(target=target, source_refs=("library:1",), **fields)


def _migrate(registry, snapshot: dict):
    """`snapshot` stored as runtime_state at revision 7, 098 run on it; the migrated row and the
    Runtime Central restores from it."""
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO runtime_state(singleton, revision, snapshot, updated_at) "
                     "VALUES (TRUE, 7, %s, 0) ON CONFLICT (singleton) DO UPDATE "
                     "SET revision=7, snapshot=EXCLUDED.snapshot", (Jsonb(snapshot),))
        assert "fade_in_seconds" in json.dumps(
            conn.execute("SELECT snapshot FROM runtime_state").fetchone()["snapshot"])
        conn.execute(MIGRATION.read_text())
        row = conn.execute("SELECT revision, snapshot FROM runtime_state").fetchone()
        functions = conn.execute("SELECT count(*) AS n FROM pg_proc "
                                 "WHERE proname IN ('with_transition', 'without_fades')").fetchone()
        runtime = RuntimeStore.read_in(conn)
    assert functions["n"] == 0
    return row, runtime


def test_stored_scenes_cross_fade_and_captured_copies_keep_their_fades(registry):
    """Stored Scenes (a child Scene and an ending included) become a cross-fade at their seconds
    and the next revision; a playing Run's and a queued activation's copies become fade out and
    in at the same revision, deriving exactly the fades they carried, ending included; the
    Default Scene restarts on the new revision. Mutation probes: rewrite captured copies to a
    cross-fade (the Run's photo would no longer fade out); skip child Scenes (the snapshot no
    longer restores); keep the stored revision (the Default Scene keeps its old copy)."""
    set_house(registry.db, UTC_HOUSE)
    runtime = Runtime(house=UTC_HOUSE)
    child = Scene(scene_id="child", cycle_seconds=10, transition=Transition(
        style="fade_out_and_in", seconds=2), contributions=(_media("frame:right"),))
    runtime.set_scene(Scene(
        scene_id="dissolve", revision=4, cycle_seconds=20, loop=True, transition=THROUGH_BLACK,
        contributions=(_media(after_end="keep_this_photo"),), outro_seconds=4,
        outro_contributions=(_media(after_end="keep_nothing"),),
        children=(Child(scene=child),)))
    runtime.set_scene(Scene(scene_id="black-end", cycle_seconds=20, loop=True,
                            transition=THROUGH_BLACK, contributions=(_media(),), outro_seconds=4,
                            outro_contributions=(Contribution(target="frame:left", kind="black"),)))
    runtime.set_scene(Scene(scene_id="background", cycle_seconds=30, loop=True,
                            transition=THROUGH_BLACK, contributions=(_media("frame:hall"),)))
    runtime.set_default_scene("background", 1000)
    run_id = runtime.activate("dissolve", "showing", 1000).run_id
    runtime.activate("dissolve", "waiting", 1001, repeat="queue", expires_at=9000)
    before = runtime.advance(1005)
    row, migrated = _migrate(registry, _previous_snapshot(runtime.export_state()))

    state = row["snapshot"]
    assert row["revision"] == 8 and "fade_in_seconds" not in json.dumps(state)
    stored = state["scenes"]["dissolve"]
    assert (stored["revision"], stored["transition"]) == (5, {"style": "cross_fade", "seconds": 3})
    assert stored["children"][0]["scene"]["transition"] == {"style": "cross_fade", "seconds": 2}
    assert state["scenes"]["black-end"]["transition"] == {"style": "cross_fade", "seconds": 3}
    run = state["runs"][run_id]["scene"]
    assert (run["revision"], run["transition"]) == (4, {"style": "fade_out_and_in", "seconds": 3})
    assert run["children"][0]["scene"]["transition"] == {"style": "fade_out_and_in", "seconds": 2}
    assert state["queue"][0]["scene"]["transition"] == {"style": "fade_out_and_in", "seconds": 3}

    def fades(view):
        return sorted((i.target, i.phase, i.shows_from, i.fade_in_seconds, i.fade_out_seconds,
                       i.after_end) for i in view.contributions if i.scene_id in ("dissolve", "child"))

    after = migrated.advance(1005)
    assert fades(after) == fades(before) == [
        ("frame:left", "body", 1000, 1.5, 1.5, "keep_this_photo"),
        ("frame:right", "body", 1000, 1, 1, "leave_as_is")]
    runtime.finish(run_id, 1010)
    migrated.finish(run_id, 1010)
    ending, migrated_ending = runtime.advance(1021), migrated.advance(1021)
    assert fades(migrated_ending) == fades(ending) == [
        # The queued activation plays after it, so the ending leaves its Frame as it is.
        ("frame:left", "outro", 1020, 1.5, 2.5, "leave_as_is")]
    defaults = [r for r in migrated_ending.runs
                if r.scene_id == "background" and r.ended_at is None]
    assert {r.scene_revision for r in defaults} == {1, 2}       # the old one finishes beneath
    assert migrated._state.runs[max(defaults, key=lambda r: r.scene_revision).run_id].scene \
        .transition == Transition(style="cross_fade", seconds=3)


def test_central_boots_on_a_migrated_snapshot_and_offers_the_same_layers(registry):
    """A Scene with a 3 s fade and a fading ending plays through Central; its Runtime snapshot is
    put back the way the previous build stored it and 098 runs: a Central started on it offers
    every layer it offered before, field for field, the locked ones kept (none stale), and the
    stored Scene is a cross-fade at its next revision. Mutation probe: give the captured copy a
    cross-fade (the layers' fades and starts change and the locks go stale)."""
    player, app, coordinator = _rig(registry)
    with TestClient(app) as client:
        _save_and_start(client, _scene("faded", fade=3, ending=("fade", 4)))
        registry.clock.advance(12)
        offered = coordinator.advance().for_player(player["player_id"]).layers
    assert [(layer.fade_in, layer.fade_out) for layer in offered][:2] == [(1.5, 1.5)] * 2
    with registry.db.transaction() as conn:
        snapshot = conn.execute("SELECT snapshot FROM runtime_state").fetchone()["snapshot"]
        conn.execute("UPDATE runtime_state SET snapshot=%s",
                     (Jsonb(_previous_snapshot(snapshot)),))
        conn.execute(MIGRATION.read_text())
    booted = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(booted) as client:
        projection = booted.state.coordinator.advance()
        stored = client.get("/v1/operator/runtime", headers={"Authorization": "Bearer " + ADMIN}
                            ).json()["definitions"]["faded"]
    assert projection.for_player(player["player_id"]).layers == offered
    assert not [d for d in projection.diagnostics if d.code == "lock_stale_authority"]
    assert (stored["revision"], stored["transition"]) == (2, {"style": "cross_fade", "seconds": 3})
