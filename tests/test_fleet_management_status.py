"""Operator fleet status keeps Central records distinct from OS carrier claims."""

import json
from uuid import UUID

from test_fleet_attempt_reports import (
    ATTEMPT,
    AUDIENCE,
    BOOT,
    COMMAND,
    DEVICE,
    DRAIN,
    FALLBACK,
    OFFER,
    SESSION,
    TARGET,
    _principal,
    _report,
    _seed,
)
from test_fleet_attempts import DEVICE_ID
from test_fleet_attempts import _principal as command_principal
from test_fleet_command_lifecycle import _ready, _setup

from central.fleet.attempt_reports import AttemptReportStore
from central.fleet.management_status import _command_doc
from central.fleet.service import FleetService
from contracts.os_recovery_report import OsRecoveryReport

LEASE = UUID(int=801)


def _management(registry):
    devices = FleetService(registry.db, registry.clock).status()["devices"]
    return next(device["management"] for device in devices
                if device["device_id"] == DEVICE)


def test_t0_device_has_no_invented_command_or_os_session(registry):
    with registry.db.transaction() as conn:
        conn.execute(
            "INSERT INTO devices(device_id,serial,first_seen,last_seen) "
            "VALUES(%s,'abcdef1234567890',900,900)", (DEVICE,),
        )
    view = FleetService(registry.db, registry.clock).status()
    device = next(item for item in view["devices"] if item["device_id"] == DEVICE)
    assert view["assurance"] == "t0_observational"
    assert device["management"]["os_session"]["state"] == "none"
    assert device["management"]["attempt"]["state"] == "none"
    assert device["management"]["latest_attempt_report"]["state"] == "none"
    assert device["management"]["recovery"]["state"] == "none"
    assert device["update_now"]["available"] is False


def test_status_separates_attempt_session_and_carrier_report(registry):
    _seed(registry)
    before = _management(registry)
    assert before["attempt"]["state"] == "stop_committed"
    assert before["attempt"]["target_digest"] == TARGET
    assert before["attempt"]["fallback_digest"] == FALLBACK
    assert before["attempt"]["root_retention"] == "retention_obligation"
    assert before["attempt"]["byte_availability"] == "unknown"
    assert before["os_session"]["state"] == "recorded_unexpired"
    assert before["os_session"]["physical_connectivity"] == "unknown"
    assert before["latest_attempt_report"]["state"] == "none"
    assert before["recovery"]["state"] == "none"

    AttemptReportStore(registry.db, registry.clock).record(
        _principal(), _report(active_sha256=TARGET, fault_code="renderer_fault"))
    after = _management(registry)
    claim = after["latest_attempt_report"]
    assert claim["state"] == "reported"
    assert claim["source"] == "authenticated_os_carrier_claim"
    assert claim["assurance"] == "t1_t2_carrier_claim"
    assert claim["executor_state"] == "committed"
    assert claim["active_digest"] == TARGET
    assert claim["fault_code"] == "renderer_fault"
    assert claim["physical_output"] == "unknown"
    assert claim["carrier_session_relation"] == "recorded_current"
    rendered = json.dumps(after)
    assert "report_json" not in rendered
    assert "signature" not in rendered

    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_app_attempts SET revoked_at=1000 "
                     "WHERE attempt_id=%s", (ATTEMPT,))
    revoked = _management(registry)
    assert revoked["attempt"]["state"] == "stop_committed"
    assert revoked["attempt"]["attempt_revoked_at"] == 1000
    assert revoked["attempt"]["root_retention"] == "retention_obligation"

    registry.clock.advance(101)
    stale = _management(registry)
    assert stale["os_session"]["state"] == "expired"
    assert stale["latest_attempt_report"]["carrier_session_relation"] == \
        "historical_or_unavailable"


def test_recovery_lease_and_observation_are_repair_status_only(registry):
    _seed(registry)
    report = OsRecoveryReport(
        device_id=DEVICE, device_generation=1,
        installation_audience=AUDIENCE, kernel_boot_id=BOOT, offer_id=OFFER,
        command_session_id=SESSION, attempt_id=ATTEMPT, command_id=COMMAND,
        drain_id=DRAIN, lease_id=LEASE, report_sequence=1,
        sampled_boottime_ms=2500, executor_state="rolled_back",
        active_sha256=FALLBACK)
    with registry.db.transaction() as conn:
        conn.execute(
            "INSERT INTO fleet_recovery_leases(lease_id,attempt_id,lease_sequence,"
            "device_id,device_generation,issuing_session_id,carrier_session_id,"
            "carrier_boot_id,carrier_offer_id,carrier_audience,carrier_trust_mode,"
            "issued_at,expires_at) VALUES(%s,%s,1,%s,1,%s,%s,%s,%s,%s,'t1',900,1100)",
            (LEASE, ATTEMPT, DEVICE, SESSION, SESSION, BOOT, OFFER, AUDIENCE),
        )
        conn.execute(
            "INSERT INTO fleet_recovery_observations(attempt_id,carrier_session_id,"
            "report_sequence,lease_id,report_json,received_at) "
            "VALUES(%s,%s,1,%s,%s,1000)",
            (ATTEMPT, SESSION, LEASE, report.model_dump_json(by_alias=True)),
        )
    status = _management(registry)
    assert status["recovery"]["state"] == "recorded_unexpired"
    assert status["recovery"]["assurance"] == "repair_authority_record_only"
    claim = status["recovery"]["latest_claim"]
    assert claim["state"] == "reported"
    assert claim["executor_state"] == "rolled_back"
    assert claim["active_digest"] == FALLBACK
    assert status["command"]["state"] == "attempt_correlation_only"

    registry.clock.advance(101)
    expired = _management(registry)
    assert expired["recovery"]["state"] == "expired"
    assert expired["recovery"]["latest_claim"]["state"] == "reported"


def test_command_and_permit_status_never_claim_delivery_or_stop():
    attempt = {"command_id": COMMAND, "drain_id": DRAIN}
    command = {"attempt_id": ATTEMPT, "command_id": COMMAND,
               "request_id": UUID(int=802), "drain_id": DRAIN,
               "command_issued_at": 900.0, "command_expires_at": 1100.0,
               "permit_id": None, "permit_issued_at": None,
               "permit_expires_at": None, "command_bytes": b"must-not-leak"}
    issued = _command_doc(command, attempt, 1000.0)
    assert issued["state"] == "issued_delivery_unknown"
    assert issued["os_delivery"] == "unknown"
    assert issued["stop_execution"] == "unknown"
    assert "command_bytes" not in issued
    assert _command_doc(command, attempt, 1101.0)["state"] == \
        "expired_delivery_unknown"

    permitted = command | {"permit_id": UUID(int=803),
                           "permit_issued_at": 950.0,
                           "permit_expires_at": 970.0,
                           "permit_bytes": b"must-not-leak"}
    assert _command_doc(permitted, attempt, 960.0)["state"] == \
        "permit_issued_stop_unknown"
    expired = _command_doc(permitted, attempt, 1000.0)
    assert expired["state"] == "permit_expired_stop_unknown"
    assert expired["stop_execution"] == "unknown"
    assert "permit_bytes" not in expired


def test_status_reads_issued_command_and_permit_without_claiming_execution(registry):
    lifecycle, request_id, player, generation = _setup(registry)
    principal = command_principal()
    issued = lifecycle.dispatch_unbound(
        principal, request_id=request_id, player_id=player["player_id"],
        expected_gate_generation=generation)

    def status():
        device = next(item for item in FleetService(
            registry.db, registry.clock).status()["devices"]
            if item["device_id"] == DEVICE_ID)
        return device["management"]["command"]

    command = status()
    assert command["state"] == "issued_delivery_unknown"
    assert command["command_id"] == str(issued.command_id)
    assert command["request_id"] == str(request_id)
    assert command["os_delivery"] == "unknown"
    assert command["stop_execution"] == "unknown"

    permit = lifecycle.authorize_stop_unbound(
        principal, _ready(issued), expected_gate_generation=generation)
    after = status()
    assert after["state"] == "permit_issued_stop_unknown"
    assert after["permit_id"] == str(permit.permit_id)
    assert after["stop_execution"] == "unknown"
    assert "command_bytes" not in json.dumps(after)
    assert "permit_bytes" not in json.dumps(after)
    assert "ready_nonce" not in json.dumps(after)

    registry.clock.advance(31)
    expired = status()
    assert expired["state"] == "permit_expired_stop_unknown"
    assert expired["stop_execution"] == "unknown"
