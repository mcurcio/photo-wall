"""Real PostgreSQL HTTP/WebSocket contract checks with disposable test authority."""

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_coordination import setup_players
from test_registry import ADMIN, enroll

from central.app import create_app
from central.media_ports import SourceConfigurationReceipt
from contracts.models import PlayerConfiguration, Readiness


def test_operator_source_scene_program_workflow_and_player_originated_session(registry):
    player = setup_players(registry, count=1)[0]
    app = create_app(registry.db, registry.clock, ADMIN)
    operator = {"Authorization": "Bearer " + ADMIN}
    appliance = {"Authorization": "Bearer " + player["token"]}
    with TestClient(app) as client:
        source = {"schema": 1, "source_ref": "source:1", "connection_ref": "library", "favorites": True}
        configured = client.put(
            "/v1/operator/sources/source:1", json=source, headers=operator,
        ).json()
        assert SourceConfigurationReceipt.model_validate(configured, strict=True).model_dump() == {
            "source_ref": "source:1", "created": True,
        }
        assert client.put("/v1/operator/sources/source:1", json={**source, "favorites": False}, headers=operator).status_code == 409
        scene = {"scene_id": "night", "loop": True, "cycle_seconds": 30,
                 "contributions": [{"target": "frame:frame-0", "kind": "black"}]}
        assert client.put("/v1/operator/scenes/night", json=scene, headers=appliance).status_code == 401
        assert client.put("/v1/operator/scenes/night", json=scene, headers=operator).status_code == 200
        program = {"program_id": "evening", "scene_id": "night", "starts_at": 1010, "ends_at": 1200}
        assert client.put("/v1/operator/programs/evening", json=program, headers=operator).status_code == 200
        app.state.coordinator.advance()
        registry.clock.advance(6)
        with client.websocket_connect("/v1/player/session", headers=appliance) as connection:
            state = connection.receive_json()
            assert state["type"] == "state" and state["plan"] and not state["commits"]
            configuration = PlayerConfiguration.model_validate(state["configuration"])
            assert configuration.player_id == player["player_id"]
            assert "source_ref" not in json.dumps(state) and "library" not in json.dumps(state)
            due = tuple(layer["assignment_id"] for layer in state["plan"]["layers"] if layer["start"] == 1010)
            readiness = Readiness(plan_id=state["plan"]["plan_id"], revision=state["plan"]["revision"],
                authority_epoch=1, sequence=1, secured=due, prepared=due, capacity_ok=True,
                observed_at=1006, clock_uncertainty=.01)
            connection.send_json({"type": "readiness", "payload": readiness.model_dump(mode="json")})
            granted = connection.receive_json()
            assert granted["commits"][0]["readiness_sequence"] == 1
            assert set(granted["commits"][0]["assignment_ids"]) == set(due)
        assert client.delete("/v1/operator/programs/evening", headers=operator).status_code == 200


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
    app = create_app(registry.db, registry.clock, ADMIN)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as unauthorized:
            with client.websocket_connect("/v1/player/session"):
                pass
        assert unauthorized.value.code == 1008
        with client.websocket_connect("/v1/player/session", headers={"Authorization": "Bearer " + player["token"]}) as connection:
            assert connection.receive_json()["configuration"]["authority_epoch"] == 1
            registry.retire(player["player_id"])
            with pytest.raises(WebSocketDisconnect) as retired:
                connection.receive_json()
            assert retired.value.code == 1008
