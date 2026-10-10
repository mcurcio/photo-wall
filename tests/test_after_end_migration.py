"""069 moves stored Scenes, Runs, offered plans and locks from `retain_on_expiry` to `after_end`.

The rows are written as the previous build stored them (the yes/no flag, the console's endings
changing nothing), into temporary tables of the real shape, and the migration's SQL runs on them;
and the runbook's reverse SQL followed by 069 again, with an ending in flight.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_exposed_settings_roundtrip import (
    BLACK,
    _Frame,
    _layers,
    _play_one_cycle,
    _rig,
    _shown,
)
from test_exposed_settings_roundtrip import _scene as _console_scene
from test_player_control_protocol import after_end_plan

from central.runtime import Contribution, Program, Runtime, Scene
from contracts.models import Layer, Plan

MIGRATION = Path(__file__).parents[1] / "central/migrations/069_layer_after_end.sql"


def _previous_build(value):
    """`value` as the previous build stored it: each after-state as the yes/no flag."""
    if isinstance(value, list):
        return [_previous_build(item) for item in value]
    if isinstance(value, dict):
        return {("retain_on_expiry" if key == "after_end" else key):
                (item == "keep_this_photo" if key == "after_end" else _previous_build(item))
                for key, item in value.items()}
    return value


def _scene(scene_id, ending):
    return Scene(scene_id=scene_id, cycle_seconds=20, loop=True, outro_seconds=4, contributions=(
        Contribution(target="frame:left", source_refs=("library:1",), after_end="keep_this_photo"),
    ), outro_contributions=(ending,))


def test_stored_scenes_runs_plans_and_locks_move_to_the_after_state(registry):
    """Mutation probes: map the flag's "no" to "keep_nothing" (a plain body photo would drop the
    kept one); skip the endings step (the ending stays "leave_as_is")."""
    runtime = Runtime()
    black = Contribution(target="frame:left", kind="black")
    fading = Contribution(target="frame:left", source_refs=("library:1",), fade_out_seconds=4)
    runtime.set_scene(_scene("black-end", black))
    runtime.set_scene(_scene("fade-end", fading))
    runtime.set_scene(Scene(scene_id="plain", contributions=(
        Contribution(target="frame:right", source_refs=("library:1",)),)))
    runtime.activate("black-end", "running", 1000)
    runtime.set_program(Program(program_id="later", scene_id="fade-end", starts_at=5000,
                                ends_at=6000))
    snapshot = runtime.export_state()
    _, plan = after_end_plan()
    plan = plan.model_copy(update={"layers": plan.layers[:1] + plan.layers[2:]})
    with registry.db.transaction() as conn:
        for table in ("runtime_state", "plan_offers", "assignment_locks"):
            conn.execute(f"CREATE TEMP TABLE {table} (LIKE {table} INCLUDING DEFAULTS)")
        conn.execute("INSERT INTO runtime_state VALUES(TRUE,1,%s,0)",
                     (Jsonb(_previous_build(snapshot)),))
        conn.execute("INSERT INTO plan_offers VALUES('p',1,1,'plan-1',%s,'{}',0,20)",
                     (Jsonb(_previous_build(plan.model_dump(mode="json"))),))
        conn.execute("INSERT INTO assignment_locks VALUES('p',1,'kept',%s,0,10)",
                     (Jsonb(_previous_build(plan.layers[0].model_dump(mode="json"))),))
        assert "retain_on_expiry" in json.dumps(
            conn.execute("SELECT snapshot FROM runtime_state").fetchone()["snapshot"])
        conn.execute(MIGRATION.read_text())
        migrated = conn.execute("SELECT snapshot FROM runtime_state").fetchone()["snapshot"]
        manifest = conn.execute("SELECT manifest FROM plan_offers").fetchone()["manifest"]
        lock = conn.execute("SELECT layer FROM assignment_locks").fetchone()["layer"]
        functions = conn.execute(
            "SELECT count(*) AS n FROM pg_proc WHERE proname='ending_keeps_nothing'").fetchone()
    state = Runtime.restore(migrated).export_state()
    for scene_id in ("black-end", "fade-end"):
        scene = state["scenes"][scene_id]
        assert [c["after_end"] for c in scene["contributions"]] == ["keep_this_photo"]
        assert [c["after_end"] for c in scene["outro_contributions"]] == ["keep_nothing"]
    assert state["scenes"]["plain"]["contributions"][0]["after_end"] == "leave_as_is"
    (run,) = state["runs"].values()
    assert [c["after_end"] for c in run["scene"]["outro_contributions"]] == ["keep_nothing"]
    assert [layer.after_end for layer in Plan.model_validate(manifest).layers] == [
        "keep_this_photo", "leave_as_is"]
    assert Layer.model_validate(lock).after_end == "keep_this_photo"
    assert functions["n"] == 0


RUNBOOK = Path(__file__).parents[1] / "docs/runbook.md"


def _rollback_sql() -> str:
    """The runbook's reverse of 069, exactly as an operator would copy it."""
    text = RUNBOOK.read_text()
    section = text[text.index("**Rolling Central back past the after-state"):]
    start = section.index("```sql\n") + len("```sql\n")
    return section[start:section.index("\n```", start)]


def test_an_ending_in_flight_across_rollback_and_069_still_keeps_nothing(registry, tmp_path):
    """The black ending is offered and committed when Central is rolled back with the runbook's
    SQL (the previous build's shape) and then forward again: 069 gives the stored ending
    `keep_nothing` but its offered layer `leave_as_is`, and the planner revises it rather than
    dropping it as stale. Mutation probe: make the after-state not revisable (the ending is
    dropped and the kept photo comes back)."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play_one_cycle(client, frame, _console_scene("black-end", ending=("black", 4)))
        assert _shown(frame.advance(9)) == (False, [("photo", 1.0)])  # 19 s: ending committed
        with registry.db.transaction() as conn:
            conn.execute(_rollback_sql())
            stored = json.dumps([conn.execute(f"SELECT {column} FROM {table}").fetchall()
                                 for table, column in (("runtime_state", "snapshot"),
                                                       ("plan_offers", "manifest"),
                                                       ("assignment_locks", "layer"))])
        assert "retain_on_expiry" in stored and "after_end" not in stored
        registry.db.migrate()  # rolling forward: 069 runs again
        assert _shown(frame.advance(2)) == (False, [("black", 1.0)])  # 21 s: the ending
        assert [layer.after_end for layer in _layers(coordinator, player)
                if layer.presentation == "black"] == ["keep_nothing"]
        assert _shown(frame.lose_central(60)) == BLACK
