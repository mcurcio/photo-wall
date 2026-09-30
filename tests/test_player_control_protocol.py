"""Application envelope compatibility and epoch-scoped control evidence."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_registry import ADMIN, enroll

from central.app import create_app
from central.player_control_protocol import project_state, state_digest
from contracts.player_control import (
    ControlAck,
    ControlHello,
    ControlSelection,
    select_control,
)
from player.service import State


class _WireValue:
    def __init__(self, value):
        self.value = value

    def model_dump(self, *, mode):
        assert mode == "json"
        return self.value


def test_identify_countdown_does_not_churn_control_delivery_identity():
    base = {"configuration": {}, "plan": None, "commits": [], "revocations": [],
            "identify_output": {"request_id": "a", "output_id": "HDMI-A-1",
                                "authority_epoch": 3, "remaining_seconds": 15.0},
            "identify_expires_at": 115.0}
    later = {**base, "identify_output": {**base["identify_output"],
                                          "remaining_seconds": 14.5},
             "identify_expires_at": 114.5}
    changed = {**base, "identify_output": {**base["identify_output"],
                                            "request_id": "b"}}
    assert state_digest(base) == state_digest(later)
    assert state_digest(base) != state_digest(changed)


def test_future_control_schema_offer_selects_highest_known_common():
    offer = ControlHello(authority_epoch=3, schemas=(1, 3, 2),
                         capabilities=("identify_output", "future_capability"))
    assert select_control(offer) == ControlSelection(
        authority_epoch=3, schema=2, capabilities=("identify_output",))
    assert select_control(ControlHello(authority_epoch=3, schemas=(3,),
                                       capabilities=())) is None
    with pytest.raises(ValidationError):
        ControlHello(authority_epoch=3, schemas=(0, 2), capabilities=())
    with pytest.raises(ValidationError):
        ControlHello(authority_epoch=3, schemas=(2, 2), capabilities=())


def test_legacy_projection_is_exact_four_field_published_envelope():
    delivery = {"configuration": _WireValue({"authority_epoch": 1}), "plan": None,
                "commits": (), "revocations": (),
                "identify_output": _WireValue({"output_id": "HDMI-A-1"})}
    assert project_state(delivery, ControlSelection(authority_epoch=1, schema=1)) == {
        "configuration": {"authority_epoch": 1}, "plan": None,
        "commits": [], "revocations": [],
    }


def test_legacy_http_and_websocket_state_and_identify_refusal(registry):
    player, _, _ = enroll(registry)
    app = create_app(registry.db, registry.clock, ADMIN)
    headers = {"Authorization": "Bearer " + player["token"]}
    with TestClient(app) as client:
        state = client.get("/v1/player/state", headers=headers)
        assert state.status_code == 200
        assert set(state.json()) == {"configuration", "plan", "commits", "revocations"}
        with client.websocket_connect("/v1/player/session", headers=headers) as socket:
            message = socket.receive_json()
            assert message.pop("type") == "state"
            assert set(message) == {"configuration", "plan", "commits", "revocations"}
        refused = client.post(
            f"/v1/operator/players/{player['player_id']}/outputs/HDMI-A-2/identify",
            headers={"Authorization": "Bearer " + ADMIN},
        )
        assert refused.status_code == 409
        assert refused.json() == {"error": "identify_unsupported"}


def test_mixed_pod_enrollment_without_control_row_seals_legacy(registry):
    player, _, _ = enroll(registry)
    with registry.db.transaction() as conn:
        conn.execute("DELETE FROM player_control_sessions WHERE player_id=%s",
                     (player["player_id"],))
    app = create_app(registry.db, registry.clock, ADMIN)
    headers = {"Authorization": "Bearer " + player["token"]}
    offer = {"authority_epoch": player["authority_epoch"], "schemas": [1, 2],
             "capabilities": ["identify_output"]}
    with TestClient(app) as client:
        hello = client.post("/v1/player/hello", json=offer, headers=headers)
        assert hello.status_code == 200
        assert hello.json() == {"authority_epoch": player["authority_epoch"],
                                "schema": 1, "capabilities": []}
        assert set(client.get("/v1/player/state", headers=headers).json()) == {
            "configuration", "plan", "commits", "revocations"}
        assert client.post("/v1/player/hello", json=offer, headers=headers).status_code == 409


def test_first_state_seals_hello_and_new_epoch_can_negotiate(registry):
    player, key, request = enroll(registry)
    app = create_app(registry.db, registry.clock, ADMIN)
    offer = {"authority_epoch": player["authority_epoch"], "schemas": [1, 2],
             "capabilities": ["identify_output"]}
    with TestClient(app) as client:
        assert client.get("/v1/player/state", headers={
            "Authorization": "Bearer " + player["token"]}).status_code == 200
        late = client.post("/v1/player/hello", json=offer, headers={
            "Authorization": "Bearer " + player["token"]})
        assert late.status_code == 409
        assert late.json() == {"error": "control_negotiation_closed"}
        newer, _, _ = enroll(registry, key=key, device_id=request.device_id)
        headers = {"Authorization": "Bearer " + newer["token"]}
        offer["authority_epoch"] = newer["authority_epoch"]
        hello = client.post("/v1/player/hello", json=offer, headers=headers)
        assert hello.status_code == 200
        assert hello.json() == {"authority_epoch": newer["authority_epoch"],
                                "schema": 2, "capabilities": ["identify_output"]}
        assert client.post("/v1/player/hello", json=offer, headers=headers).json() == hello.json()
        conflict = client.post("/v1/player/hello", json={**offer, "capabilities": []},
                               headers=headers)
        assert conflict.status_code == 409
        assert conflict.json() == {"error": "control_negotiation_conflict"}


def test_v2_applied_ack_is_once_consumed_and_epoch_fenced(registry):
    player, key, request = enroll(registry)
    app = create_app(registry.db, registry.clock, ADMIN)
    headers = {"Authorization": "Bearer " + player["token"]}
    offer = {"authority_epoch": player["authority_epoch"], "schemas": [1, 2],
             "capabilities": ["identify_output"]}
    with TestClient(app) as client:
        assert client.post("/v1/player/hello", json=offer, headers=headers).status_code == 200
        state = client.get("/v1/player/state", headers=headers).json()
        assert set(state) == {"configuration", "plan", "commits", "revocations",
                              "identify_output", "identify_expires_at", "delivery_id",
                              "delivery_sequence"}
        assert state["delivery_sequence"] == 1
        State.model_validate(state)
        report = {"authority_epoch": player["authority_epoch"],
                  "delivery_id": state["delivery_id"], "result": "applied"}
        assert client.post("/v1/player/control-acks", json=report, headers=headers).json() == {
            "accepted": True}
        with registry.db.transaction() as conn:
            first = conn.execute(
                "SELECT applied_at,applied_sequence FROM player_control_sessions WHERE player_id=%s",
                (player["player_id"],),
            ).fetchone()
        assert first["applied_at"] is not None and first["applied_sequence"] == 1
        registry.clock.advance(10)
        assert client.post("/v1/player/control-acks", json=report, headers=headers).json() == {
            "accepted": False}
        with registry.db.transaction() as conn:
            repeated = conn.execute(
                "SELECT applied_at,applied_sequence FROM player_control_sessions WHERE player_id=%s",
                (player["player_id"],),
            ).fetchone()
        assert repeated == first
        assert client.get("/v1/player/state", headers=headers).json()["delivery_id"] != state[
            "delivery_id"]
        newer, _, _ = enroll(registry, key=key, device_id=request.device_id)
        stale = client.post("/v1/player/control-acks", json=report, headers={
            "Authorization": "Bearer " + newer["token"]})
        assert stale.status_code == 403


def test_delivery_sequence_and_applied_receipt_remain_distinct_from_latest_result(registry):
    player, _, _ = enroll(registry)
    player_id, epoch = player["player_id"], player["authority_epoch"]
    registry.control_hello(player_id, ControlHello(
        authority_epoch=epoch, schemas=(1, 2), capabilities=()))
    first = registry.issue_control_delivery_record(player_id, epoch, "a" * 64)
    assert registry.issue_control_delivery_record(player_id, epoch, "a" * 64) == first
    assert first["delivery_sequence"] == 1
    assert registry.control_ack(player_id, ControlAck(
        authority_epoch=epoch, delivery_id=first["delivery_id"], result="applied"))
    second = registry.issue_control_delivery_record(player_id, epoch, "b" * 64)
    assert second["delivery_sequence"] == 2
    assert not registry.control_ack(player_id, ControlAck(
        authority_epoch=epoch, delivery_id=first["delivery_id"], result="applied"))
    assert registry.control_ack(player_id, ControlAck(
        authority_epoch=epoch, delivery_id=second["delivery_id"], result="rejected"))
    fact = registry.control_fact(player_id)
    assert fact["issued_sequence"] == 2
    assert fact["applied_sequence"] == 1
    assert fact["applied_delivery_id"] == first["delivery_id"]
    assert fact["applied_digest"] == "a" * 64
    assert fact["last_delivery_id"] == second["delivery_id"]
    assert fact["last_result_sequence"] == 2
    assert fact["last_result_digest"] == "b" * 64
    assert fact["last_result"] == "rejected"
