"""Application envelope compatibility and epoch-scoped control evidence."""

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_registry import ADMIN, enroll

from central.app import create_app
from central.player_control_protocol import project_state, state_digest
from contracts.models import FrameProfile, Layer, OutputBinding, Plan, PlayerConfiguration, Variant
from contracts.player_control import (
    LAYER_AFTER_END,
    ControlAck,
    ControlHello,
    ControlSelection,
    select_control,
)
from player.service import PlayerService


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


def test_v2_applied_ack_replays_exact_receipt_until_superseded_and_epoch_fenced(registry):
    from player.service import State

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
        accepted = client.post(
            "/v1/player/control-acks", json=report, headers=headers).json()
        assert accepted["accepted"] is True
        assert accepted["receipt"] == {
            "schema": 1, "authority_epoch": player["authority_epoch"],
            "delivery_id": state["delivery_id"], "delivery_sequence": 1,
            "state_digest": accepted["receipt"]["state_digest"],
            "ack_nonce": accepted["receipt"]["ack_nonce"],
        }
        assert re.fullmatch(r"[0-9a-f]{64}", accepted["receipt"]["state_digest"])
        assert re.fullmatch(r"[0-9a-f]{64}", accepted["receipt"]["ack_nonce"])
        assert "ack_nonce" not in registry.control_fact(player["player_id"])
        with registry.db.transaction() as conn:
            first = conn.execute(
                "SELECT applied_at,applied_sequence FROM player_control_sessions WHERE player_id=%s",
                (player["player_id"],),
            ).fetchone()
        assert first["applied_at"] is not None and first["applied_sequence"] == 1
        registry.clock.advance(10)
        assert client.post("/v1/player/control-acks", json=report,
                           headers=headers).json() == accepted
        with registry.db.transaction() as conn:
            repeated = conn.execute(
                "SELECT applied_at,applied_sequence FROM player_control_sessions WHERE player_id=%s",
                (player["player_id"],),
            ).fetchone()
        assert repeated == first
        assert client.get("/v1/player/state", headers=headers).json()["delivery_id"] != state[
            "delivery_id"]
        assert client.post("/v1/player/control-acks", json=report, headers=headers).json() == {
            "accepted": False}
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
    first_ack = ControlAck(authority_epoch=epoch, delivery_id=first["delivery_id"],
                           result="applied")
    receipt = registry.control_ack_response(player_id, first_ack)
    assert receipt.accepted and receipt.receipt is not None
    assert registry.control_ack_response(player_id, first_ack) == receipt
    second = registry.issue_control_delivery_record(player_id, epoch, "b" * 64)
    assert second["delivery_sequence"] == 2
    assert registry.control_ack_response(player_id, first_ack).model_dump(exclude_none=True) == {
        "accepted": False}
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT applied_ack_nonce FROM player_control_sessions "
                            "WHERE player_id=%s", (player_id,)).fetchone()[
                                "applied_ack_nonce"] is None
    assert not registry.control_ack(player_id, ControlAck(
        authority_epoch=epoch, delivery_id=first["delivery_id"], result="applied"))
    rejected = registry.control_ack_response(player_id, ControlAck(
        authority_epoch=epoch, delivery_id=second["delivery_id"], result="rejected"))
    assert rejected.model_dump(exclude_none=True) == {"accepted": True}
    assert registry.control_ack_response(player_id, first_ack).model_dump(
        exclude_none=True) == {"accepted": False}
    fact = registry.control_fact(player_id)
    assert fact["issued_sequence"] == 2
    assert fact["applied_sequence"] == 1
    assert fact["applied_delivery_id"] == first["delivery_id"]
    assert fact["applied_digest"] == "a" * 64
    assert fact["last_delivery_id"] == second["delivery_id"]
    assert fact["last_result_sequence"] == 2
    assert fact["last_result_digest"] == "b" * 64
    assert fact["last_result"] == "rejected"


def test_older_central_writer_cannot_leave_a_replayable_receipt(registry):
    player, _, _ = enroll(registry)
    player_id, epoch = player["player_id"], player["authority_epoch"]
    registry.control_hello(player_id, ControlHello(
        authority_epoch=epoch, schemas=(1, 2), capabilities=()))
    first = registry.issue_control_delivery_record(player_id, epoch, "a" * 64)
    ack = ControlAck(authority_epoch=epoch, delivery_id=first["delivery_id"],
                     result="applied")
    assert registry.control_ack_response(player_id, ack).receipt is not None
    # Simulate a mixed-version Central pod that issues a newer delivery using
    # the pre-receipt SQL, which does not know about applied_ack_nonce.
    with registry.db.transaction() as conn:
        conn.execute(
            "UPDATE player_control_sessions SET issued_sequence=issued_sequence+1,"
            "pending_id=%s,pending_digest=%s,pending_expires=%s WHERE player_id=%s",
            ("b" * 32, "b" * 64, registry.clock.utc() + 60, player_id),
        )
        nonce = conn.execute("SELECT applied_ack_nonce FROM player_control_sessions "
                             "WHERE player_id=%s", (player_id,)).fetchone()[
                                 "applied_ack_nonce"]
    assert nonce is None
    assert registry.control_ack_response(player_id, ack).model_dump(exclude_none=True) == {
        "accepted": False}


@pytest.mark.parametrize("operation", ["ack", "reissue"])
def test_control_expiry_is_sampled_after_waiting_for_player_lock(
        registry, monkeypatch, operation):
    player, _, _ = enroll(registry)
    player_id, epoch = player["player_id"], player["authority_epoch"]
    registry.control_hello(player_id, ControlHello(
        authority_epoch=epoch, schemas=(1, 2), capabilities=()))
    first = registry.issue_control_delivery_record(player_id, epoch, "a" * 64)
    waiting = threading.Event()
    original = registry._control_player

    def mark_lock_wait(conn, locked_player_id, locked_epoch):
        waiting.set()
        original(conn, locked_player_id, locked_epoch)

    monkeypatch.setattr(registry, "_control_player", mark_lock_wait)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with registry.db.transaction() as conn:
            conn.execute("SELECT id FROM players WHERE id=%s FOR UPDATE", (player_id,))
            if operation == "ack":
                ack = ControlAck(authority_epoch=epoch,
                                 delivery_id=first["delivery_id"], result="applied")
                future = executor.submit(registry.control_ack_response, player_id, ack)
            else:
                future = executor.submit(registry.issue_control_delivery_record,
                                         player_id, epoch, "a" * 64)
            assert waiting.wait(timeout=2)
            registry.clock.advance(61)
        outcome = future.result(timeout=5)
    if operation == "ack":
        assert outcome.model_dump(exclude_none=True) == {"accepted": False}
    else:
        assert outcome["delivery_id"] != first["delivery_id"]
        assert outcome["delivery_sequence"] == first["delivery_sequence"] + 1


def after_end_plan() -> tuple[PlayerConfiguration, Plan]:
    """A kept photo, then a black ending that keeps nothing, then a plain overlay."""
    binding = OutputBinding(output_id="HDMI-A-1", frame_id="frame-0", generation=1,
                            profile=FrameProfile(width_px=1920, height_px=1080,
                                                 diagonal_inches=24))
    configuration = PlayerConfiguration(player_id="p-" + "a" * 32, authority_epoch=1,
                                        configuration_revision=1, bindings=(binding,),
                                        enabled_outputs=("HDMI-A-1",))
    common = dict(run_id="run-1", output_id="HDMI-A-1", frame_id="frame-0", binding_generation=1)
    photo = Variant(sha256="b" * 64, size=10, media_type="image/jpeg", width=10, height=10)
    layers = (
        Layer(assignment_id="kept", start=0, end=10, media_origin=0, variant=photo,
              after_end="keep_this_photo", **common),
        Layer(assignment_id="ending", start=10, end=14, media_origin=10, presentation="black",
              after_end="keep_nothing", **common),
        Layer(assignment_id="overlay", start=14, end=20, media_origin=14, variant=photo,
              priority=5, **common),
    )
    return configuration, Plan(plan_id="plan-1", revision=1, player_id=configuration.player_id,
                               authority_epoch=1, issued_at=0, valid_from=0, valid_until=20,
                               bindings=(binding,), layers=layers)


def after_end_state(selection: ControlSelection) -> dict:
    configuration, plan = after_end_plan()
    return project_state({"configuration": configuration, "plan": plan, "commits": (),
                          "revocations": (), "identify_output": None}, selection)


LEGACY = ControlSelection(authority_epoch=1, schema=1)
IDENTIFY_ONLY = ControlSelection(authority_epoch=1, schema=2, capabilities=("identify_output",))
CURRENT = ControlSelection(authority_epoch=1, schema=2,
                           capabilities=("identify_output", LAYER_AFTER_END))


def test_only_a_player_that_offers_it_is_sent_the_after_state():
    """A plan layer's `after_end` reaches only a session that selected `layer_after_end`; any
    other gets the yes/no flag its release parses (unknown fields forbidden), kept photo or
    not. This Player reads either shape as the after-state. Mutation probe: send `after_end`
    to every session (a released Player, whose models forbid unknown fields, would refuse the
    state)."""
    assert select_control(ControlHello(authority_epoch=1, schemas=(1, 2), capabilities=(
        LAYER_AFTER_END, "identify_output"))) == CURRENT
    assert select_control(ControlHello(authority_epoch=1, schemas=(1, 2),
                                       capabilities=("identify_output",))) == IDENTIFY_ONLY
    current = after_end_state(CURRENT)["plan"]["layers"]
    assert [layer["after_end"] for layer in current] == [
        "keep_this_photo", "keep_nothing", "leave_as_is"]
    assert not any("retain_on_expiry" in layer for layer in current)
    for selection in (LEGACY, IDENTIFY_ONLY):
        legacy = after_end_state(selection)["plan"]["layers"]
        assert [layer["retain_on_expiry"] for layer in legacy] == [True, False, False]
        assert not any("after_end" in layer for layer in legacy)
    # This Player, given a selection without the capability by an older Central.
    _, plan = after_end_plan()
    service = SimpleNamespace(_control_selection=IDENTIFY_ONLY)
    state = PlayerService._state(service, after_end_state(IDENTIFY_ONLY))
    assert [layer.after_end for layer in state.plan.layers] == [
        "keep_this_photo", "leave_as_is", "leave_as_is"]
    service = SimpleNamespace(_control_selection=CURRENT)
    assert PlayerService._state(service, after_end_state(CURRENT)).plan == plan
    # An explicit after-state is read as sent, whatever the selection says.
    service = SimpleNamespace(_control_selection=IDENTIFY_ONLY)
    assert PlayerService._state(service, after_end_state(CURRENT)).plan == plan
