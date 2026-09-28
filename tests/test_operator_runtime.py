"""GET /v1/operator/runtime serves the operator projection (pass 2 slice 3 §8)."""

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
