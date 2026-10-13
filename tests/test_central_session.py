"""Real PostgreSQL HTTP/WebSocket contract checks with disposable test authority."""

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from support.house_time import UTC_HOUSE, set_house
from test_coordination import setup_players
from test_registry import ADMIN, enroll, hello

from central.app import create_app
from contracts.models import PlayerConfiguration, Readiness


def test_operator_source_scene_program_workflow_and_player_originated_session(registry):
    player = setup_players(registry, count=1)[0]
    hello(registry, player)
    app = create_app(registry.db, registry.clock, ADMIN)
    operator = {"Authorization": "Bearer " + ADMIN}
    appliance = {"Authorization": "Bearer " + player["token"]}
    with TestClient(app) as client:
        connection = client.post("/v1/operator/library/connections", headers=operator, json={
            "name": "library", "url": "http://immich.invalid", "key": "PUBLIC-FIXTURE-KEY-1234",
        }).json()["connection_ref"]
        draft = client.post("/v1/operator/photo-drafts", headers=operator, json={
            "query": {"connection_ref": connection, "favourites_only": True}}).json()["draft_ref"]
        saved = client.post(f"/v1/operator/photo-drafts/{draft}/save", headers=operator,
                            json={"name": "source"})
        assert (saved.status_code, saved.json()) == (200, {"source_ref": "source:1"})
        scene = {"scene_id": "night", "loop": True, "cycle_seconds": 30,
                 "contributions": [{"target": "frame:frame-0", "kind": "black"}]}
        assert client.put("/v1/operator/scenes/night", json=scene, headers=appliance).status_code == 401
        assert client.put("/v1/operator/scenes/night", json=scene, headers=operator).status_code == 200
        set_house(registry.db, UTC_HOUSE)
        registry.clock.advance(10)          # 1010: the window starts at 00:17 (1020)
        program = {"name": "Evening", "scene_id": "night", "when": WHEN_0017_0020}
        created = client.post("/v1/operator/programs", json=program, headers=operator)
        assert created.status_code == 201
        app.state.coordinator.advance()
        registry.clock.advance(6)
        with client.websocket_connect("/v1/player/session", headers=appliance) as connection:
            state = connection.receive_json()
            assert state["type"] == "state" and state["plan"] and not state["commits"]
            configuration = PlayerConfiguration.model_validate(state["configuration"])
            assert configuration.player_id == player["player_id"]
            assert "source_ref" not in json.dumps(state) and "library" not in json.dumps(state)
            due = tuple(layer["assignment_id"] for layer in state["plan"]["layers"] if layer["start"] == 1020)
            readiness = Readiness(plan_id=state["plan"]["plan_id"], revision=state["plan"]["revision"],
                authority_epoch=1, sequence=1, secured=due, prepared=due, capacity_ok=True,
                observed_at=1016, clock_uncertainty=.01, lost_grants=())
            connection.send_json({"type": "readiness", "payload": readiness.model_dump(mode="json")})
            granted = connection.receive_json()
            assert granted["commits"][0]["readiness_sequence"] == 1
            assert set(granted["commits"][0]["assignment_ids"]) == set(due)
        program_id = created.json()["program_id"]
        assert client.delete(f"/v1/operator/programs/{program_id}?revision=1", headers=operator).status_code == 204


WHEN_0017_0020 = {"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                  "start": {"at": "clock", "time": "00:17"}, "end": {"at": "clock", "time": "00:20"}}


def test_program_routes_are_optimistic_and_retryable(registry):
    """The Schedule routes (roadmap 2b): POST creates at revision 1 with Central's id; PUT saves
    against the Program the editor loaded (409 "changed" with the stored one, an identical retry
    answers again); DELETE names the revision; refusals are {"error": code}."""
    app = create_app(registry.db, registry.clock, ADMIN)
    operator = {"Authorization": "Bearer " + ADMIN}
    with TestClient(app) as client:
        for scene_id in ("first", "second"):
            scene = {"scene_id": scene_id, "loop": True, "cycle_seconds": 30,
                     "contributions": [{"target": "frame:frame-0", "kind": "black"}]}
            assert client.put(f"/v1/operator/scenes/{scene_id}", json=scene,
                              headers=operator).status_code == 200
        draft = {"name": "Evenings", "scene_id": "first", "when": WHEN_0017_0020}
        created = client.post("/v1/operator/programs", json=draft, headers=operator)
        assert created.status_code == 201
        original = created.json()
        assert original["program_id"].startswith("p-") and original["revision"] == 1
        assert (original["priority"], original["name"]) == ("normal", "Evenings")
        path = f"/v1/operator/programs/{original['program_id']}"

        first = {"expected": original, "next": {**draft, "priority": "special-event"}}
        saved = client.put(path, json=first, headers=operator)
        assert saved.status_code == 200 and saved.json()["revision"] == 2
        assert client.put(path, json=first, headers=operator).json() == saved.json()   # a retry
        stale = client.put(path, json={"expected": original, "next": {**draft, "scene_id": "second"}},
                           headers=operator)
        assert stale.status_code == 409 and stale.json() == {"error": "changed", "current": saved.json()}

        assert client.delete(f"{path}?revision=1", headers=operator).json() == {
            "error": "changed", "current": saved.json()}
        assert client.delete(f"{path}?revision=2", headers=operator).status_code == 204
        missing = client.delete(f"{path}?revision=2", headers=operator)
        assert (missing.status_code, missing.json()) == (404, {"error": "missing"})

        unknown = client.post("/v1/operator/programs", json={**draft, "scene_id": "nowhere"}, headers=operator)
        assert (unknown.status_code, unknown.json()) == (422, {"error": "unknown-scene"})
        no_days = client.post("/v1/operator/programs", json={**draft, "when": {**WHEN_0017_0020, "days": []}},
                              headers=operator)
        assert (no_days.status_code, no_days.json()) == (422, {"error": "no-days"})
        sunset = {**WHEN_0017_0020, "start": {"at": "sunset", "offset_minutes": -30}}
        needs_place = client.post("/v1/operator/programs", json={**draft, "when": sunset}, headers=operator)
        assert (needs_place.status_code, needs_place.json()) == (422, {"error": "needs-place"})

        refused = client.put("/v1/operator/default-scene", json={"scene_id": "nowhere"}, headers=operator)
        assert (refused.status_code, refused.json()) == (422, {"error": "unknown-scene"})
        assert client.put("/v1/operator/default-scene", json={"scene_id": "second"},
                          headers=operator).status_code == 204
        runtime = client.get("/v1/operator/runtime", headers=operator).json()
        assert runtime["default_scene"] == {"scene_id": "second", "revision": 1}
        assert [run["scene_id"] for run in runtime["current"]["runs"]] == ["second"]
        assert client.put("/v1/operator/default-scene", json={"scene_id": None},
                          headers=operator).status_code == 204


def test_program_save_checks_the_path_identity(registry):
    app = create_app(registry.db, registry.clock, ADMIN)
    expected = {"program_id": "other", "name": "x", "scene_id": "s", "when": WHEN_0017_0020}
    response = TestClient(app).put(
        "/v1/operator/programs/path-id",
        json={"expected": expected, "next": {"name": "x", "scene_id": "s", "when": WHEN_0017_0020}},
        headers={"Authorization": "Bearer " + ADMIN},
    )
    assert response.status_code == 422


def test_operator_unbind_endpoint_reverses_binding_and_updates_pending_queue(registry):
    player, _, _ = enroll(registry)
    app = create_app(registry.db, registry.clock, ADMIN)
    operator = {"Authorization": "Bearer " + ADMIN}
    with TestClient(app) as client:
        frame = {"id": "portrait", "width_mm": 300, "height_mm": 500,
                 "profile": {"width_px": 1080, "height_px": 1920, "diagonal_inches": 24}}
        assert client.post("/v1/operator/frames", json=frame, headers=operator).status_code == 201
        binding = {"player_id": player["player_id"], "output_id": "HDMI-A-1", "expected_generation": 0}
        assert client.put("/v1/operator/frames/portrait/binding", json=binding, headers=operator).status_code == 200
        inventory = client.get("/v1/operator/inventory", headers=operator).json()
        assert next(p for p in inventory["players"] if p["id"] == player["player_id"])["is_bound"] is True
        response = client.request("DELETE", "/v1/operator/frames/portrait/binding",
                                  json={"expected_generation": 1}, headers=operator)
        assert response.status_code == 200 and response.json()["generation"] == 2
        inventory = client.get("/v1/operator/inventory", headers=operator).json()
        assert next(p for p in inventory["players"] if p["id"] == player["player_id"])["is_bound"] is False
        assert inventory["frames"][0]["player_id"] is None
        again = client.request("DELETE", "/v1/operator/frames/portrait/binding",
                               json={"expected_generation": 2}, headers=operator)
        assert again.status_code == 404 and again.json()["error"] == "not_bound"


def test_live_session_rejects_retired_or_wrong_epoch_authority(registry):
    player = setup_players(registry, count=1)[0]
    hello(registry, player)
    app = create_app(registry.db, registry.clock, ADMIN)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as unauthorized:
            with client.websocket_connect("/v1/player/session"):
                pass
        assert unauthorized.value.code == 1008
        with client.websocket_connect("/v1/player/session", headers={"Authorization": "Bearer " + player["token"]}) as connection:
            assert connection.receive_json()["configuration"]["authority_epoch"] == 1
            registry.unbind("frame-0", expected_generation=1)
            registry.retire(player["player_id"])
            with pytest.raises(WebSocketDisconnect) as retired:
                connection.receive_json()
            assert retired.value.code == 1008
