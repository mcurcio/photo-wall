"""Operator fleet status keeps Central records distinct from OS carrier claims."""

import json
from uuid import UUID

from central.content_catalog.catalog import device_id_for_serial
from central.fleet.service import FleetService
from contracts.os_attempt_report import OsAttemptReport

SERIAL = "abcdef1234567890"
DEVICE = device_id_for_serial(SERIAL)
assert DEVICE is not None
BOOT = UUID(int=701)
OFFER = UUID(int=702)
SESSION = UUID(int=703)
ATTEMPT = UUID(int=704)
COMMAND = UUID(int=705)
DRAIN = UUID(int=706)
AUDIENCE = "installation-one"
TARGET = "d" * 64
FALLBACK = "e" * 64


def _report(sequence: int = 1, **changes) -> OsAttemptReport:
    values = dict(
        device_id=DEVICE, device_generation=1, installation_audience=AUDIENCE,
        kernel_boot_id=BOOT, offer_id=OFFER, command_session_id=SESSION,
        attempt_id=ATTEMPT, command_id=COMMAND, drain_id=DRAIN,
        report_sequence=sequence, sampled_boottime_ms=1234,
        executor_state="committed", active_sha256=TARGET,
    )
    values.update(changes)
    return OsAttemptReport(**values)


def _seed(registry, *, phase: str = "stop_committed", schema: int = 1,
          revoked_at: float | None = None,
          attempt_session_id: UUID | None = SESSION) -> None:
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,"
                     "discovered_at,updated_at) VALUES('v1.0.0',1,0,0,FALSE,900,900)")
        conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen) "
                     "VALUES(%s,%s,900,900)", (DEVICE, SERIAL))
        conn.execute("INSERT INTO fleet_boot_offers(offer_id,installation_audience,device_id,"
                     "serial,kernel_boot_id,boot_nonce,base_policy_source,base_policy_revision,"
                     "app_policy_source,app_policy_revision,base_tag,base_content_key,"
                     "base_sha256,base_size,app_status,compatibility_basis,offer_schema,"
                     "created_at,expires_at) "
                     "VALUES(%s,%s,%s,%s,%s,%s,'operator_baseline',1,'explicit',1,"
                     "'v1.0.0',%s,%s,1024,'unconfigured','none',2,900,2000)",
                     (OFFER, AUDIENCE, DEVICE, SERIAL, BOOT, "1" * 32,
                      "a" * 64, "b" * 64))
        conn.execute("INSERT INTO fleet_os_command_sessions(command_session_id,device_id,"
                     "device_generation,kernel_boot_id,offer_id,installation_audience,"
                     "trust_mode,agent_key_sha256,verifier_ref,issued_at,expires_at) "
                     "VALUES(%s,%s,1,%s,%s,%s,'t1',%s,'test-gateway',900,1100)",
                     (SESSION, DEVICE, BOOT, OFFER, AUDIENCE, "f" * 64))
        if attempt_session_id is not None and attempt_session_id != SESSION:
            conn.execute("INSERT INTO fleet_os_command_sessions(command_session_id,device_id,"
                         "device_generation,kernel_boot_id,offer_id,installation_audience,"
                         "trust_mode,agent_key_sha256,verifier_ref,issued_at,expires_at,"
                         "revoked_at) VALUES(%s,%s,1,%s,%s,%s,'t1',%s,"
                         "'former-gateway',800,900,900)",
                         (attempt_session_id, DEVICE, BOOT, OFFER, AUDIENCE, "e" * 64))
        if schema == 1:
            conn.execute(
                "INSERT INTO fleet_app_attempts(attempt_id,device_id,offer_id,"
                "desired_revision,target_sha256,fallback_sha256,phase,command_id,drain_id,"
                "created_at,updated_at,device_generation,command_session_id,"
                "revoked_at,attempt_schema,"
                "installation_audience,kernel_boot_id,policy_source,base_sha256,base_abi,"
                "base_abi_source_manifest,target_tag,target_size,target_format,"
                "target_base_abi,target_source_manifest,fallback_size,fallback_base_abi,"
                "fallback_trust_mode,fallback_evidence_ref) "
                "VALUES(%s,%s,%s,1,%s,%s,%s,%s,%s,900,900,1,%s,%s,1,%s,%s,'explicit',%s,%s,"
                "'manifest.v2.json','v1.0.1',123,'pw-player-data-v1',%s,"
                "'manifest.v2.json',80,%s,'t1','test-qualified-output')",
                (ATTEMPT, DEVICE, OFFER, TARGET, FALLBACK, phase, COMMAND, DRAIN,
                 attempt_session_id, revoked_at, AUDIENCE, BOOT, "b" * 64,
                 "sha256:" + "c" * 64,
                 "sha256:" + "c" * 64, "sha256:" + "c" * 64),
            )
        else:
            conn.execute("INSERT INTO fleet_app_attempts(attempt_id,device_id,offer_id,"
                         "desired_revision,target_sha256,fallback_sha256,phase,command_id,"
                         "drain_id,created_at,updated_at,device_generation) "
                         "VALUES(%s,%s,%s,1,%s,%s,%s,%s,%s,900,900,1)",
                         (ATTEMPT, DEVICE, OFFER, TARGET, FALLBACK, phase, COMMAND, DRAIN))


def _store_report(registry, report: OsAttemptReport) -> None:
    """Fixture bytes as the retired T1/T2 carrier route stored them."""
    with registry.db.transaction() as conn:
        conn.execute(
            "INSERT INTO fleet_os_attempt_reports(attempt_id,report_sequence,"
            "command_session_id,carrier_trust_mode,report_json,received_at) "
            "VALUES(%s,%s,%s,'t1',%s,%s)",
            (report.attempt_id, report.report_sequence, SESSION,
             report.model_dump_json(by_alias=True), registry.clock.utc()),
        )


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

    _store_report(registry, _report(active_sha256=TARGET, fault_code="renderer_fault"))
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
