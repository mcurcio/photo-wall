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
                             uuid4().hex + uuid4().hex)
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
        sessions.enroll(replace(claim, session_id=uuid4()))


def test_replaced_producer_cannot_regress_current_projection(registry):
    sessions, claim, grant = setup(registry)
    ingest = NodeIngest(sessions)
    event = NodeEventV2(grant.producer, uuid4(), 1, 1000, (RebootFact("unknown", "base_observer"),))
    first = ingest.ingest(grant.session_id, claim.credential, encode_node_message(event))
    registry.clock.advance(10)
    assert ingest.ingest(grant.session_id, claim.credential, encode_node_message(event))["received_at"] == first["received_at"]
    next_claim = replace(claim, session_id=uuid4(), incarnation_id=uuid4(),
                         credential=uuid4().hex + uuid4().hex)
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
    request = OperatorReboot(uuid4(), grant.session_id, 1, "operator:fixture", generation)
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
    registry.clock.advance(31)
    assert commands.poll(grant.session_id, claim.credential)["commands"] == []
    with pytest.raises(NodeControlError, match="expired"):
        commands.request_reboot(DEVICE_ID, request)
    second = service.offer(NodeBootRequestV2(SERIAL, uuid4(), "b" * 64))
    status = NodeObservations(sessions).status(DEVICE_ID)
    assert any(item["kernel_boot_id"] == str(second.kernel_boot_id) for item in status["boot_claims"])
    assert not any(item["producer"]["kernel_boot_id"] == str(second.kernel_boot_id) for item in status["sessions"])


def test_the_device_read_serves_a_deprecated_boot_only_when_it_is_the_newest_boot_record(registry):
    """G5: the newer of the V1 offer and the netboot-base serve, served only while newer than
    every node boot offer for the box; Central compares its own clock readings."""
    from test_node_boot import cold_setup

    from central.content_catalog.catalog import device_id_for_serial
    from contracts.node_boot import NodeBootRequestV2
    service, sessions, _ = cold_setup(registry)  # seeds a V1 offer at 900, no node offer
    read = NodeObservations(sessions)
    assert read.status(DEVICE_ID)["deprecated_boot"] == {"path": "offer", "recorded_at": 900}
    service.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))  # node offer at 1000
    assert read.status(DEVICE_ID)["deprecated_boot"] is None
    registry.clock.advance(5)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE devices SET last_served_tag='v1.0.0', last_served_at=%s WHERE device_id=%s",
                     (registry.clock.utc(), DEVICE_ID))
    assert read.status(DEVICE_ID)["deprecated_boot"] == {"path": "base_without_offer", "recorded_at": 1005}
    registry.clock.advance(5)
    service.offer(NodeBootRequestV2(SERIAL, uuid4(), "b" * 64))
    assert read.status(DEVICE_ID)["deprecated_boot"] is None
    # A box with node boot records and no V1 record at all.
    other_serial = "10000000c0ffee93"
    service.offer(NodeBootRequestV2(other_serial, uuid4(), "c" * 64))
    assert read.status(device_id_for_serial(other_serial))["deprecated_boot"] is None


def test_reboot_fence_one_outstanding_per_session_and_served_outstanding(registry):
    from test_node_boot import claim_for, cold_setup

    from contracts.node_boot import NodeBootRequestV2
    service, sessions, _ = cold_setup(registry)
    claim = claim_for(service.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64)))
    grant = sessions.enroll(claim)
    gate, _ = _gate(registry)
    generation = gate.open(expected_revision=0).generation
    commands, ingest, reads = NodeCommands(sessions, gate), NodeIngest(sessions), NodeObservations(sessions)

    def reboot():
        return OperatorReboot(uuid4(), grant.session_id, 1, "operator:fixture", generation)

    def respond(request, issued, decision):
        ingest.ingest(grant.session_id, claim.credential, encode_node_message(NodeCommandResponseV2(
            grant.producer, request.command_id, issued["command"]["command_sha256"], grant.session_id,
            "operator_reboot", decision, "reboot_scope_or_expiry")))

    def served():
        return {item["command_id"]: item["outstanding"] for item in reads.status(DEVICE_ID)["reboot_commands"]}

    def stored():
        with registry.db.transaction() as conn:
            return conn.execute("SELECT count(*) AS n FROM node_reboot_commands").fetchone()["n"]

    first = reboot()
    first_issued = commands.request_reboot(DEVICE_ID, first)
    assert served() == {str(first.command_id): True}
    with pytest.raises(NodeControlError, match="node_reboot_outstanding") as refused:
        commands.request_reboot(DEVICE_ID, reboot())
    assert refused.value.status == 409 and stored() == 1
    assert commands.request_reboot(DEVICE_ID, first)["duplicate"]  # the same id is unaffected
    respond(first, first_issued, "rejected")
    assert served() == {str(first.command_id): False}
    second = reboot()
    second_issued = commands.request_reboot(DEVICE_ID, second)  # accepted after a rejection
    assert served() == {str(first.command_id): False, str(second.command_id): True}
    respond(second, second_issued, "accepted")  # accepted, not initiated: still outstanding
    assert served()[str(second.command_id)] is True
    with pytest.raises(NodeControlError, match="node_reboot_outstanding"):
        commands.request_reboot(DEVICE_ID, reboot())
    registry.clock.advance(31)
    assert served()[str(second.command_id)] is False
    third = reboot()
    commands.request_reboot(DEVICE_ID, third)  # accepted after expiry
    assert served()[str(third.command_id)] is True and stored() == 3
    later = sessions.enroll(claim_for(service.offer(NodeBootRequestV2(SERIAL, uuid4(), "b" * 64))))
    commands.request_reboot(DEVICE_ID, OperatorReboot(uuid4(), later.session_id, 1, "operator:fixture",
                                                      generation))  # an earlier session never blocks
    assert stored() == 4


def test_display_read_serves_the_newest_exchange_per_output_on_the_current_boot(registry):
    # Console DDD C2 (§16 display read): newest per Output across the current admission's
    # Display Host producers; an earlier boot is never served; receipt age on one clock.
    from test_node_boot import claim_for, cold_setup

    from contracts.node_boot import NodeBootRequestV2
    from contracts.node_display import (
        DisplayExchange,
        DisplayReceipt,
        Surface,
        encode_display_exchange,
    )
    from contracts.node_protocol import OutputKey
    service, sessions, _ = cold_setup(registry)
    reads = NodeObservations(sessions)
    process = NodeProcessIdentity(123, 10, uuid4())

    def display_host(offer):
        return sessions.enroll(claim_for(offer, owner="display_host"))

    def record(grant, output_id, sampled, *, connected=True, admitted=False, receipt_at=None, receipt_revision=7):
        output = OutputKey(grant.producer.kernel_boot_id, grant.producer.incarnation_id, output_id, 1, 1)
        surface = Surface(output, process, 42, 3, 7, "lobby")
        received = replace(surface, config_revision=receipt_revision)
        receipt = None if receipt_at is None else DisplayReceipt(received, uuid4(), "buffer-1", "tag", receipt_at)
        exchange = DisplayExchange(grant.producer, uuid4(), sampled, output, connected,
                                   admitted=surface if admitted else None, receipt=receipt)
        with registry.db.transaction() as conn:
            producer = conn.execute("SELECT producer_id FROM node_sessions WHERE session_id=%s",
                                    (grant.session_id,)).fetchone()["producer_id"]
            conn.execute("INSERT INTO node_display_exchanges(producer_id,request_id,session_id,output_id,"
                         "sampled_boottime_ms,request,response,decision_id,received_at) "
                         "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (producer, exchange.request_id, grant.session_id, output_id, sampled,
                          encode_display_exchange(exchange), b"{}", uuid4(), registry.clock.utc()))

    def served():
        return {item["output_id"]: item for item in reads.status(DEVICE_ID)["display_outputs"]}

    first_boot = service.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))
    assert served() == {}
    before = display_host(first_boot)
    record(before, "HDMI-A-1", 1000, connected=False)
    record(before, "HDMI-A-1", 1100, admitted=True, receipt_at=900)
    record(before, "HDMI-A-2", 5000, admitted=True, receipt_at=4000, receipt_revision=6)
    received = registry.clock.utc()
    registry.clock.advance(5)
    record(before, "HDMI-A-2", 4500)  # received later on Central's clock, sampled earlier: loses
    restarted = display_host(first_boot)  # a new Display Host producer within the same boot
    record(restarted, "HDMI-A-1", 2000, admitted=True, receipt_at=1600)
    now = served()
    assert set(now) == {"HDMI-A-1", "HDMI-A-2"}
    assert now["HDMI-A-1"] == {"output_id": "HDMI-A-1", "received_at": received + 5, "connected": True,
        "surface": {"frame_id": "lobby", "binding_generation": 3, "config_revision": 7},
        "receipt": {"matches_surface": True, "age_ms": 400}}  # the restarted producer wins
    assert now["HDMI-A-2"] == {"output_id": "HDMI-A-2", "received_at": received, "connected": True,
        "surface": {"frame_id": "lobby", "binding_generation": 3, "config_revision": 7},
        "receipt": {"matches_surface": False, "age_ms": 1000}}  # a receipt for another revision
    # A later boot supersedes the first; its earlier samples (a new boot clock) still win,
    # because the first boot's exchanges are never served.
    later = display_host(service.offer(NodeBootRequestV2(SERIAL, uuid4(), "b" * 64)))
    assert served() == {}
    record(later, "HDMI-A-1", 10, connected=False)
    assert served() == {"HDMI-A-1": {"output_id": "HDMI-A-1", "received_at": registry.clock.utc(),
                                     "connected": False, "surface": None, "receipt": None}}
    # The read is bounded by the index, not the boot's history: thousands of exchanges per
    # Output leave the served rows unchanged and the plan reads a handful of index entries.
    from central.fleet.node_display import DISPLAY_OUTPUTS_SQL, display_outputs_params
    with registry.db.transaction() as conn:
        producer = conn.execute("SELECT producer_id FROM node_sessions WHERE session_id=%s",
                                (later.session_id,)).fetchone()["producer_id"]
        conn.execute("INSERT INTO node_display_exchanges(producer_id,request_id,session_id,output_id,"
                     "sampled_boottime_ms,request,response,decision_id,received_at) "
                     "SELECT %s,gen_random_uuid(),%s,'HDMI-A-' || (1 + i %% 2),i,'\\x00'::bytea,"
                     "'\\x00'::bytea,gen_random_uuid(),0 FROM generate_series(100,20099) i",
                     (producer, later.session_id))
        conn.execute("ANALYZE node_display_exchanges")
    record(later, "HDMI-A-1", 50000, connected=False)
    record(later, "HDMI-A-2", 50001, admitted=True)
    assert {key: item["connected"] for key, item in served().items()} == {"HDMI-A-1": False, "HDMI-A-2": True}
    with registry.db.transaction() as conn:
        plan = conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + DISPLAY_OUTPUTS_SQL,
                            display_outputs_params(DEVICE_ID, later.producer.device_generation)).fetchone()
    plan = next(iter(plan.values()))[0]["Plan"]
    assert 0 < _exchange_rows(plan) < 50  # never a scan of the 20,002 rows


def _exchange_rows(node):
    """Rows the plan read from node_display_exchanges, across every loop."""
    own = (node["Actual Rows"] * node["Actual Loops"]
           if node.get("Relation Name") == "node_display_exchanges" else 0)
    return own + sum(_exchange_rows(child) for child in node.get("Plans", []))


def test_display_read_is_bounded_however_many_producers_the_node_creates(registry):
    # A node picks its incarnation ids, so it picks how many display_host producers one boot
    # has (up to the daily session cap), and any Output id. The read is bounded in SQL by
    # the newest producers and the Output bound, never by that count: removing the producer
    # LIMIT reads every producer's Outputs and fails the row bound below.
    from test_node_boot import claim_for, cold_setup

    from central.fleet.node_display import (
        _MAX_OUTPUTS,
        _MAX_PRODUCERS,
        DISPLAY_OUTPUTS_SQL,
        display_outputs_params,
    )
    from contracts.node_boot import NodeBootRequestV2
    service, sessions, _ = cold_setup(registry)
    offer = service.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))
    producers = 3 * _MAX_PRODUCERS
    grants = []
    for _ in range(producers):
        grants.append(sessions.enroll(claim_for(offer, owner="display_host")))
        registry.clock.advance(1)  # admission order is Central's own clock
    with registry.db.transaction() as conn:
        for index, grant in enumerate(grants):
            producer = conn.execute("SELECT producer_id FROM node_sessions WHERE session_id=%s",
                                    (grant.session_id,)).fetchone()["producer_id"]
            # Each producer floods past the Output bound; the newest producer samples latest.
            conn.execute("INSERT INTO node_display_exchanges(producer_id,request_id,session_id,output_id,"
                         "sampled_boottime_ms,request,response,decision_id,received_at) "
                         "SELECT %s,gen_random_uuid(),%s,'OUT-' || lpad(i::text,3,'0'),%s,"
                         "'\\x00'::bytea,'\\x00'::bytea,gen_random_uuid(),0 "
                         "FROM generate_series(1,%s) i",
                         (producer, grant.session_id, 1000 + index, _MAX_OUTPUTS + 6))
        conn.execute("ANALYZE node_display_exchanges")
        params = display_outputs_params(DEVICE_ID, grants[0].producer.device_generation)
        rows = conn.execute(DISPLAY_OUTPUTS_SQL, params).fetchall()
        plan = conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + DISPLAY_OUTPUTS_SQL, params).fetchone()
    assert len(rows) == _MAX_OUTPUTS
    assert {row["sampled_boottime_ms"] for row in rows} == {1000 + producers - 1}  # newest wins
    assert [row["output_id"] for row in rows] == sorted(row["output_id"] for row in rows)
    plan = next(iter(plan.values()))[0]["Plan"]
    # Skip-scan plus newest probe per (producer, Output), then one request row per winner.
    bound = _MAX_PRODUCERS * (2 * _MAX_OUTPUTS + 2) + _MAX_OUTPUTS
    assert _exchange_rows(plan) <= bound


def test_an_undecodable_display_exchange_fails_only_its_own_output(registry):
    # §15: a stored exchange this Central cannot decode (a display contract changed across an
    # upgrade while the node kept its boot) is served as undecodable for that Output alone;
    # the device read, the other Outputs and the reboot records survive.
    from test_node_boot import claim_for, cold_setup

    from contracts.node_boot import NodeBootRequestV2
    from contracts.node_display import DisplayExchange, encode_display_exchange
    from contracts.node_protocol import OutputKey
    service, sessions, _ = cold_setup(registry)
    reads = NodeObservations(sessions)
    grant = sessions.enroll(claim_for(service.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64)),
                                      owner="display_host"))

    def store(output_id, sampled, request):
        with registry.db.transaction() as conn:
            producer = conn.execute("SELECT producer_id FROM node_sessions WHERE session_id=%s",
                                    (grant.session_id,)).fetchone()["producer_id"]
            conn.execute("INSERT INTO node_display_exchanges(producer_id,request_id,session_id,output_id,"
                         "sampled_boottime_ms,request,response,decision_id,received_at) "
                         "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (producer, uuid4(), grant.session_id, output_id, sampled, request, b"{}",
                          uuid4(), registry.clock.utc()))

    output = OutputKey(grant.producer.kernel_boot_id, grant.producer.incarnation_id, "HDMI-A-1", 1, 1)
    store("HDMI-A-1", 100, encode_display_exchange(DisplayExchange(grant.producer, uuid4(), 100, output, True)))
    store("HDMI-A-2", 200, b'{"schema":99,"kind":"display_exchange"}')
    store("HDMI-A-3", 300, b"\x00")
    status = reads.status(DEVICE_ID)
    served = {item["output_id"]: item for item in status["display_outputs"]}
    received = registry.clock.utc()
    assert served == {
        "HDMI-A-1": {"output_id": "HDMI-A-1", "received_at": received, "connected": True,
                     "surface": None, "receipt": None},
        "HDMI-A-2": {"output_id": "HDMI-A-2", "received_at": received, "undecodable": True},
        "HDMI-A-3": {"output_id": "HDMI-A-3", "received_at": received, "undecodable": True},
    }
    assert "reboot_commands" in status and any(item["current"] for item in status["sessions"])


def test_default_gate_cannot_mint_reboot(registry):
    sessions, _, grant = setup(registry)
    service = NodeCommands(sessions, RolloutEffectGate(registry.db))
    with pytest.raises(RolloutGateError):
        service.request_reboot(DEVICE_ID, OperatorReboot(uuid4(), grant.session_id, 1,
                                                        "operator:fixture", 1))
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM node_reboot_commands").fetchone()["n"] == 0


def test_mounted_routes_disabled_by_default_and_real_when_configured(registry):
    _seed(registry)
    claim = NodeSessionClaim(SERIAL, OFFER_ID, BOOT_ID, "host_core", uuid4(), uuid4(), "d" * 64)
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
        assert not grant.command_eligible and grant.command_reason == "legacy_observation_adoption"
        assert client.get("/v2/node/commands", headers=headers).json() == {"commands": []}
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
