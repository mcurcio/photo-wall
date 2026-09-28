"""GET /v1/operator/runtime serves the operator projection (pass 2 slice 3 §8)."""

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient
from test_operator_frames import ADMIN, AUTH, _portrait

from central.app import create_app
from central.runtime import Contribution, Program, Scene


def test_the_runtime_read_serves_protection_and_program_outcomes(registry):
    _portrait(registry)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    runtime = app.state.coordinator.runtime
    now = registry.clock.utc()
    target = (Contribution(target="frame:portrait", asset_refs=("a",)),)
    runtime.command("set_scene", Scene(scene_id="guard", protect_frames=True, loop=True,
                                       contributions=target))
    runtime.command("set_scene", Scene(scene_id="evening", loop=True, contributions=target))
    runtime.command("activate", "guard", "guard-act", now, priority=5)
    runtime.command("set_program", Program(
        program_id="evening", scene_id="evening", starts_at=now + 10, ends_at=now + 100))
    registry.clock.advance(20)
    with TestClient(app) as client:
        body = client.get("/v1/operator/runtime", headers=AUTH).json()
        refused = client.post("/v1/operator/activations", headers=AUTH, json={
            "scene_id": "evening", "activation_id": "evening-act"}).json()
    assert {"definitions", "programs", "current"} <= set(body)
    guard = next(run for run in body["current"]["runs"] if run["scene_id"] == "guard")
    assert (guard["program_id"], guard["priority"]) == (None, 5)
    assert body["protected_frames"] == {guard["run_id"]: ["frame:portrait"]}
    outcome = body["program_outcomes"]["evening"]
    assert (outcome["status"], outcome["reason"]) == ("rejected", "protected_frames")
    assert outcome["blocking_run_id"] == guard["run_id"]
    assert (refused["reason"], refused["blocking_run_id"]) == ("protected_frames", guard["run_id"])


def test_a_stale_scene_put_is_refused_with_409_and_an_identical_retry_is_not(registry):
    """Both Scene routes store through Runtime.set_scene, so both refuse a save that
    does not move past the stored revision (slice 3 §13, Question 4 flipped)."""
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    scene = {"scene_id": "night", "revision": 1, "cycle_seconds": 30, "loop": True,
             "contributions": [{"target": "frame:portrait", "source_refs": ["holiday:1"]}]}
    with TestClient(app) as client:
        def put(body):
            return client.put("/v1/operator/scenes/night", headers=AUTH, json=body)
        assert put(scene).status_code == 200
        assert put(scene).status_code == 200  # the same save, retried
        stale = put({**scene, "cycle_seconds": 45})
        assert (stale.status_code, stale.json()) == (409, {"error": "scene_revision_conflict"})
        assert put({**scene, "revision": 2, "cycle_seconds": 45}).status_code == 200
        stored = client.get("/v1/operator/runtime", headers=AUTH).json()["definitions"]["night"]
    assert (stored["revision"], stored["cycle_seconds"]) == (2, 45)


def _console_literal(name):
    source = (Path(__file__).parents[1] / "central/console/src/authoring.js").read_text()
    pinned = re.search(rf"^export const {name} = (\{{.*?^\}});$", source, re.MULTILINE | re.DOTALL)
    assert pinned is not None, f"authoring.js no longer exports {name} as a JSON literal"
    return json.loads(pinned.group(1))


def _model_defaults(model):
    return {name: json.loads(json.dumps(field.default))
            for name, field in model.model_fields.items() if not field.is_required()}


def test_the_console_scene_default_tables_are_the_runtime_model_defaults():
    # Slice 3 §13: Edit compares a stored Scene with the console's rebuild of it
    # after filling these defaults, so they must be the models' own.
    assert _console_literal("SCENE_DEFAULTS") == _model_defaults(Scene)
    assert _console_literal("CONTRIBUTION_DEFAULTS") == _model_defaults(Contribution)
