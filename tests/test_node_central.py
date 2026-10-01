"""Durable V2 node admission/evidence, with real PostgreSQL and inert effects."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_fleet_attempts import BOOT_ID, DEVICE_ID, OFFER_ID, SERIAL, _seed
from test_fleet_rollout_gate import _gate
from test_registry import ADMIN

from central.app import create_app
from central.fleet.node_commands import NodeCommands, OperatorReboot
from central.fleet.node_ingest import NodeIngest
from central.fleet.node_observations import NodeObservations
from central.fleet.node_sessions import NodeControlConfig, NodeControlError, NodeSessions
from central.fleet.rollout_gate import RolloutEffectGate, RolloutGateError
from contracts.node_commands import NodeSessionClaim, encode_session_claim, parse_session_grant
from contracts.node_observation import HostMetricV2, HostObservationV2, encode_host_observation
from contracts.node_protocol import (
    AppProcessFact,
    NodeCommandResponseV2,
    NodeEventV2,
    NodeProcessIdentity,
    NodeSnapshotV2,
    RebootFact,
    encode_node_message,
)


def setup(registry, owner="host_core"):
    _seed(registry)
    sessions = NodeSessions(registry.db, registry.clock, NodeControlConfig("node-test"))
    claim = NodeSessionClaim(SERIAL, OFFER_ID, BOOT_ID, owner, uuid4(), uuid4(),
                             uuid4().hex + uuid4().hex, 1000)
    return sessions, claim, sessions.enroll(claim)


def test_session_exact_retry_concurrent_and_scope_separation(registry):
    sessions, claim, grant = setup(registry)
    with ThreadPoolExecutor(4) as pool:
        assert all(item == grant for item in pool.map(sessions.enroll, [claim] * 4))
    broker_claim = replace(claim, owner="app_effect_broker", session_id=uuid4(),
                           credential=uuid4().hex + uuid4().hex)
    broker = sessions.enroll(broker_claim)
    assert broker.scope == "app_effect" and grant.scope == "operator_reboot"
    with registry.db.transaction() as conn:
        with pytest.raises(NodeControlError, match="session_unavailable"):
            sessions.authenticate_in(conn, broker.session_id, claim.credential)
    with pytest.raises(NodeControlError, match="credential_reuse"):
        sessions.enroll(replace(claim, session_id=uuid4(), expected_session_id=grant.session_id))


def test_replaced_producer_cannot_regress_current_projection(registry):
    sessions, claim, grant = setup(registry)
    ingest = NodeIngest(sessions)
    event = NodeEventV2(grant.producer, uuid4(), 1, 1000, (RebootFact("unknown", "base_observer"),))
    first = ingest.ingest(grant.session_id, claim.credential, encode_node_message(event))
    registry.clock.advance(10)
    assert ingest.ingest(grant.session_id, claim.credential, encode_node_message(event))["received_at"] == first["received_at"]
    next_claim = replace(claim, session_id=uuid4(), incarnation_id=uuid4(),
                         credential=uuid4().hex + uuid4().hex, expected_session_id=grant.session_id)
    sessions.enroll(next_claim)
    later = replace(event, event_id=uuid4(), sequence=2)
    assert ingest.ingest(grant.session_id, claim.credential, encode_node_message(later))["disposition"] == "historical"
    with registry.db.transaction() as conn:
        with pytest.raises(NodeControlError, match="superseded"):
            sessions.authenticate_in(conn, grant.session_id, claim.credential)


def test_snapshot_before_event_atomic_dedupe_and_restart(registry):
    sessions, claim, grant = setup(registry, "app_effect_broker")
    first = AppProcessFact(NodeProcessIdentity(100, 1, uuid4()), 1, "a" * 64, "running")
    second = AppProcessFact(NodeProcessIdentity(101, 2, uuid4()), 2, "b" * 64, "running")
    snap = NodeSnapshotV2(grant.producer, uuid4(), 10, 1000, (replace(first, state="exited"),))
    event = NodeEventV2(grant.producer, uuid4(), 9, 999, (first, second))
    ingest = NodeIngest(sessions)
    ingest.ingest(grant.session_id, claim.credential, encode_node_message(snap))
    with ThreadPoolExecutor(4) as pool:
        outcomes = list(pool.map(lambda _: NodeIngest(sessions).ingest(
            grant.session_id, claim.credential, encode_node_message(event)), range(4)))
    assert sorted(outcome["disposition"] for outcome in outcomes) == ["applied", "duplicate", "duplicate", "duplicate"]
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM node_evidence").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM node_reconciliation_work").fetchone()["n"] == 2
        projection = conn.execute("SELECT projection FROM node_producers").fetchone()["projection"]
        assert sorted(item["sequence"] for item in projection) == [9, 10]
    with pytest.raises(NodeControlError, match="sequence_conflict"):
        ingest.ingest(grant.session_id, claim.credential, encode_node_message(replace(event, event_id=uuid4())))
    with pytest.raises(NodeControlError, match="identity_conflict"):
        ingest.ingest(grant.session_id, claim.credential, encode_node_message(replace(event, sequence=11)))


def test_observation_replay_keeps_sample_and_receipt_age(registry):
    sessions, claim, grant = setup(registry)
    service = NodeObservations(sessions)
    sample = HostObservationV2(grant.producer, 1, 1000, (HostMetricV2("uptime", 1, "seconds"),))
    first = service.record(grant.session_id, claim.credential, encode_host_observation(sample))
    registry.clock.advance(20)
    duplicate = service.record(grant.session_id, claim.credential, encode_host_observation(sample))
    assert duplicate["received_at"] == first["received_at"]
    assert service.status(DEVICE_ID)["sessions"][0]["host_observation"]["receipt_age_seconds"] == 20
    with pytest.raises(NodeControlError, match="identity_conflict"):
        service.record(grant.session_id, claim.credential,
                       encode_host_observation(replace(sample, sampled_boottime_ms=2000)))


def test_reboot_audit_response_and_initiation_are_separate(registry):
    from test_node_boot import claim_for, cold_setup

    from contracts.node_boot import NodeBootRequestV2
    service, sessions, _ = cold_setup(registry)
    claim = claim_for(service.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64)))
    grant = sessions.enroll(claim)
    gate, _ = _gate(registry)
    generation = gate.open(expected_revision=0).generation
    commands = NodeCommands(sessions, gate)
    request = OperatorReboot(uuid4(), grant.session_id, 1, "operator:fixture", generation, 31000)
    issued = commands.request_reboot(DEVICE_ID, request)
    assert not issued["effect_established"]
    assert commands.request_reboot(DEVICE_ID, request)["duplicate"]
    assert commands.poll(grant.session_id, claim.credential)["commands"] == [issued["command"]]
    response = NodeCommandResponseV2(grant.producer, request.command_id,
        issued["command"]["command_sha256"], grant.session_id, "operator_reboot", "accepted", "reboot_admitted")
    ingest = NodeIngest(sessions)
    ingest.ingest(grant.session_id, claim.credential, encode_node_message(response))
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM node_evidence").fetchone()["n"] == 0
    event = NodeEventV2(grant.producer, uuid4(), 1, 1500,
                        (RebootFact("operator_command", "requester"),), request.command_id)
    ingest.ingest(grant.session_id, claim.credential, encode_node_message(event))
    status = NodeObservations(sessions).status(DEVICE_ID)
    audit = status["reboot_commands"][0]
    assert audit["responses"][0]["message"]["message"]["decision"] == "accepted"
    assert audit["effects"][0]["event_id"] == str(event.event_id)
    assert audit["effects"][0]["physical_completion"] == "unknown"
    assert status["boot_claims"][0]["kernel_boot_id"] == str(BOOT_ID)
    assert status["boot_claims"][0]["selectable"]
    registry.clock.advance(31)
    assert commands.poll(grant.session_id, claim.credential)["commands"] == []
    with pytest.raises(NodeControlError, match="expired"):
        commands.request_reboot(DEVICE_ID, request)
    second = service.offer(NodeBootRequestV2(SERIAL, uuid4(), "b" * 64))
    status = NodeObservations(sessions).status(DEVICE_ID)
    assert any(item["kernel_boot_id"] == str(second.kernel_boot_id) for item in status["boot_claims"])
    assert not any(item["producer"]["kernel_boot_id"] == str(second.kernel_boot_id) for item in status["sessions"])


def test_default_gate_cannot_mint_reboot(registry):
    sessions, _, grant = setup(registry)
    service = NodeCommands(sessions, RolloutEffectGate(registry.db))
    with pytest.raises(RolloutGateError):
        service.request_reboot(DEVICE_ID, OperatorReboot(uuid4(), grant.session_id, 1,
                                                        "operator:fixture", 1, 31000))
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM node_reboot_commands").fetchone()["n"] == 0


def test_mounted_routes_disabled_by_default_and_real_when_configured(registry):
    _seed(registry)
    claim = NodeSessionClaim(SERIAL, OFFER_ID, BOOT_ID, "host_core", uuid4(), uuid4(), "d" * 64, 1000)
    app = create_app(registry.db, registry.clock, ADMIN)
    with TestClient(app) as client:
        response = client.post("/v2/node/sessions", content=encode_session_claim(claim))
        assert response.status_code == 503
        assert response.headers["cache-control"] == "private, no-store"
    app = create_app(registry.db, registry.clock, ADMIN, node_control=NodeControlConfig("node-test"))
    with TestClient(app) as client:
        response = client.post("/v2/node/sessions", content=encode_session_claim(claim))
        assert response.status_code == 200, response.text
        grant = parse_session_grant(response.content)
        headers = {"Authorization": "Bearer " + claim.credential, "X-Node-Session": str(grant.session_id)}
        assert not grant.command_eligible
        assert client.get("/v2/node/commands", headers=headers).json() == {"error": "legacy_observation_adoption"}
        assert client.post("/v2/node/evidence", content=b"{}", headers=headers).status_code == 422
        assert client.post("/v2/node/observations", content=b"x" * 20000, headers=headers).status_code == 413


def test_opt_in_factory_requires_audience_and_keeps_effect_gate_closed(registry, monkeypatch):
    from central.node_app import create_app as create_node_app
    monkeypatch.delenv("PHOTO_WALL_NODE_AUDIENCE", raising=False)
    with pytest.raises(ValueError, match="AUDIENCE required"):
        create_node_app(db=registry.db, clock=registry.clock, admin_token=ADMIN)
    monkeypatch.setenv("PHOTO_WALL_NODE_AUDIENCE", "node-factory-test")
    app = create_node_app(db=registry.db, clock=registry.clock, admin_token=ADMIN)
    assert app.state.node_sessions.config.installation_audience == "node-factory-test"
    assert RolloutEffectGate(registry.db).status()["state"] == "closed"
