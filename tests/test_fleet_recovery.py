"""Real PostgreSQL gates for cross-boot, data-only recovery leases."""

from hashlib import sha256
from uuid import UUID

import pytest
from psycopg.errors import CheckViolation

from central.content_catalog.catalog import device_id_for_serial
from central.fleet.bytes import OfferByteReader
from central.fleet.models import FleetError
from central.fleet.principal import PrincipalError, VerifiedOsPrincipal
from central.fleet.recovery import RecoveryService
from contracts.os_recovery_report import OsRecoveryReport, parse_os_recovery_report

SERIAL = "abcdef1234567890"
DEVICE = device_id_for_serial(SERIAL)
assert DEVICE is not None
OLD_BOOT, NEW_BOOT = UUID(int=801), UUID(int=802)
OLD_OFFER, NEW_OFFER = UUID(int=803), UUID(int=804)
OLD_SESSION, NEW_SESSION = UUID(int=805), UUID(int=806)
ATTEMPT, COMMAND, DRAIN = UUID(int=807), UUID(int=808), UUID(int=809)
LEASE1, LEASE2, LEASE3 = UUID(int=810), UUID(int=811), UUID(int=812)
OLD_AUDIENCE, NEW_AUDIENCE = "old-installation", "new-installation"
TARGET, FALLBACK = "d" * 64, "e" * 64


def _principal() -> VerifiedOsPrincipal:
    return VerifiedOsPrincipal(
        device_id=DEVICE, device_generation=1, kernel_boot_id=NEW_BOOT,
        offer_id=NEW_OFFER, installation_audience=NEW_AUDIENCE, trust_mode="t2",
        command_session_id=NEW_SESSION, agent_key_sha256="f" * 64, expires_at=1300,
    )


def _old_principal() -> VerifiedOsPrincipal:
    return VerifiedOsPrincipal(
        device_id=DEVICE, device_generation=1, kernel_boot_id=OLD_BOOT,
        offer_id=OLD_OFFER, installation_audience=OLD_AUDIENCE, trust_mode="t1",
        command_session_id=OLD_SESSION, agent_key_sha256="c" * 64, expires_at=1300,
    )


def _seed(registry, *, drain_link: UUID | None = DRAIN,
          drain_phase: str = "stop_committed", roots: bool = True,
          attempt_phase: str = "stop_committed", with_permit: bool = True) -> None:
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,"
                     "discovered_at,updated_at) VALUES('v1.0.0',1,0,0,FALSE,900,900)")
        conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen) "
                     "VALUES(%s,%s,900,900)", (DEVICE, SERIAL))
        conn.execute("INSERT INTO players(id,public_key,token_hash,registered_at,last_seen,"
                     "device_id) VALUES('player-1',%s,%s,900,900,%s)",
                     ("0" * 64, "1" * 64, DEVICE))
        for offer, boot, audience, nonce in (
            (OLD_OFFER, OLD_BOOT, OLD_AUDIENCE, "1" * 32),
            (NEW_OFFER, NEW_BOOT, NEW_AUDIENCE, "2" * 32),
        ):
            conn.execute("INSERT INTO fleet_boot_offers(offer_id,installation_audience,"
                         "device_id,serial,kernel_boot_id,boot_nonce,base_policy_source,"
                         "base_policy_revision,app_policy_source,app_policy_revision,base_tag,"
                         "base_content_key,base_sha256,base_size,app_status,"
                         "compatibility_basis,offer_schema,created_at,expires_at) "
                         "VALUES(%s,%s,%s,%s,%s,%s,'operator_baseline',1,'explicit',1,"
                         "'v1.0.0',%s,%s,1024,'unconfigured','none',2,900,2000)",
                         (offer, audience, DEVICE, SERIAL, boot, nonce, "a" * 64,
                          "b" * 64))
        for session, boot, offer, audience, trust, key, revoked in (
            (OLD_SESSION, OLD_BOOT, OLD_OFFER, OLD_AUDIENCE, "t1", "c" * 64, 950),
            (NEW_SESSION, NEW_BOOT, NEW_OFFER, NEW_AUDIENCE, "t2", "f" * 64, None),
        ):
            conn.execute("INSERT INTO fleet_os_command_sessions(command_session_id,"
                         "device_id,device_generation,kernel_boot_id,offer_id,"
                         "installation_audience,trust_mode,agent_key_sha256,verifier_ref,"
                         "issued_at,expires_at,revoked_at) "
                         "VALUES(%s,%s,1,%s,%s,%s,%s,%s,'test-verifier',900,1300,%s)",
                         (session, DEVICE, boot, offer, audience, trust, key, revoked))
        conn.execute(
            "INSERT INTO fleet_app_attempts(attempt_id,device_id,offer_id,"
            "desired_revision,target_sha256,fallback_sha256,phase,command_id,drain_id,"
            "created_at,updated_at,device_generation,command_session_id,attempt_schema,"
            "installation_audience,kernel_boot_id,policy_source,base_sha256,base_abi,"
            "base_abi_source_manifest,target_tag,target_size,target_format,"
            "target_base_abi,target_source_manifest,fallback_size,fallback_base_abi,"
            "fallback_trust_mode,fallback_evidence_ref) "
            "VALUES(%s,%s,%s,1,%s,%s,%s,%s,%s,900,900,1,%s,1,%s,%s,'explicit',%s,%s,"
            "'manifest.v2.json','v1.0.1',123,'pw-player-data-v1',%s,"
            "'manifest.v2.json',80,%s,'t1','qualified-output')",
            (ATTEMPT, DEVICE, OLD_OFFER, TARGET, FALLBACK, "prepared", COMMAND,
             DRAIN, OLD_SESSION, OLD_AUDIENCE, OLD_BOOT, "b" * 64,
             "sha256:" + "c" * 64, "sha256:" + "c" * 64,
             "sha256:" + "c" * 64),
        )
        if roots:
            for digest, size, name in ((TARGET, 123, "target"),
                                       (FALLBACK, 80, "fallback")):
                conn.execute("INSERT INTO assets(kind,identity,created_at) "
                             "VALUES('player-payload',%s,900)", (digest,))
                conn.execute("INSERT INTO asset_references(kind,identity,owner,locator_url,"
                             "locator_sha256,locator_size,expected_sha256,expected_size,"
                             "added_at) VALUES('player-payload',%s,%s,%s,%s,%s,%s,%s,900)",
                             (digest, f"fleet-attempt:{ATTEMPT}",
                              f"https://example.invalid/{name}.tar.gz", digest, size,
                              digest, size))
        conn.execute("INSERT INTO equipment_drains(player_id,attempt_id,boot_id,"
                     "authority_epoch,phase,prepared_at,authorization_expires_at,"
                     "stop_committed_at,snapshot,fleet_drain_id) "
                     "VALUES('player-1',%s,%s,1,'prepared',900,990,NULL,%s,NULL)",
                     (str(ATTEMPT), str(OLD_BOOT), '{"admission_scope":"unbound_canary"}'))
        conn.execute(
            "INSERT INTO fleet_maintenance_requests(request_id,device_id,"
            "device_generation,status,policy_source,policy_revision,target_tag,"
            "target_sha256,target_size,target_format,target_base_abi,"
            "target_source_manifest,ttl_seconds,requested_at,expires_at,"
            "changed_at,reason) VALUES(%s,%s,1,'dispatched','explicit',1,'v1.0.1',"
            "%s,123,'pw-player-data-v1',%s,'manifest.v2.json',300,900,1200,900,'test')",
            (UUID(int=813), DEVICE, TARGET, "sha256:" + "c" * 64),
        )
        command_bytes = b"frozen-test-command"
        conn.execute(
            "INSERT INTO fleet_app_commands(command_id,request_id,attempt_id,drain_id,"
            "player_id,authority_epoch,device_id,device_generation,command_session_id,"
            "policy_source,desired_revision,target_sha256,target_size,base_abi,"
            "command_bytes,command_sha256,gate_generation,gate_scope_sha256,"
            "issued_at,expires_at) VALUES(%s,%s,%s,%s,'player-1',1,%s,1,%s,"
            "'explicit',1,%s,123,%s,%s,%s,1,%s,900,980)",
            (COMMAND, UUID(int=813), ATTEMPT, DRAIN, DEVICE, OLD_SESSION,
             TARGET, "sha256:" + "c" * 64, command_bytes,
             sha256(command_bytes).hexdigest(), "1" * 64),
        )
        conn.execute("UPDATE fleet_app_attempts SET phase=%s WHERE attempt_id=%s",
                     (attempt_phase, ATTEMPT))
        if drain_phase == "stop_committed":
            conn.execute("UPDATE equipment_drains SET phase='stop_committed',"
                         "stop_committed_at=950,fleet_drain_id=%s WHERE player_id='player-1'",
                         (drain_link,))
        if (with_permit and drain_phase == "stop_committed" and drain_link == DRAIN
                and attempt_phase == "stop_committed"):
            ready_bytes, permit_bytes = b"ready-test", b"permit-test"
            conn.execute(
                "INSERT INTO fleet_app_stop_permits(permit_id,command_id,attempt_id,"
                "drain_id,ready_nonce,ready_bytes,ready_sha256,permit_bytes,"
                "permit_sha256,gate_generation,gate_scope_sha256,issued_at,expires_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,950,970)",
                (UUID(int=814), COMMAND, ATTEMPT, DRAIN, "2" * 64, ready_bytes,
                 sha256(ready_bytes).hexdigest(), permit_bytes,
                 sha256(permit_bytes).hexdigest(), "1" * 64),
            )


def _service(registry) -> RecoveryService:
    return RecoveryService(registry.db, registry.clock, OfferByteReader(None))


def _report(lease_id: UUID, sequence: int = 1, **changes) -> OsRecoveryReport:
    values = dict(
        device_id=DEVICE, device_generation=1, installation_audience=NEW_AUDIENCE,
        kernel_boot_id=NEW_BOOT, offer_id=NEW_OFFER, command_session_id=NEW_SESSION,
        attempt_id=ATTEMPT, command_id=COMMAND, drain_id=DRAIN, lease_id=lease_id,
        report_sequence=sequence, sampled_boottime_ms=100,
        executor_state="rolled_back", active_sha256=FALLBACK,
    )
    values.update(changes)
    return OsRecoveryReport(**values)


def test_new_boot_can_claim_exact_retained_bytes_and_replay_claim(registry) -> None:
    _seed(registry)
    service = _service(registry)
    lease = service.claim(_principal(), ATTEMPT, lease_id=LEASE1, expected_lease_id=None)
    assert lease.lease_sequence == 1 and lease.attempt.command_session_id == OLD_SESSION
    assert lease.carrier_session_id == NEW_SESSION and lease.expires_at == 1120
    assert service.claim(_principal(), ATTEMPT, lease_id=LEASE1,
                         expected_lease_id=None) == lease
    assert service.resolve(_principal(), ATTEMPT, LEASE1, "target").sha256 == TARGET
    assert service.resolve(_principal(), ATTEMPT, LEASE1, "fallback").sha256 == FALLBACK
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fleet_accepted_artifacts").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM fleet_generation_acceptances")\
            .fetchone()["n"] == 0


def test_cas_replacement_revokes_old_lease_and_preserves_issuer(registry) -> None:
    _seed(registry)
    service = _service(registry)
    service.claim(_principal(), ATTEMPT, lease_id=LEASE1, expected_lease_id=None)
    with pytest.raises(FleetError, match="recovery_lease_conflict"):
        service.claim(_principal(), ATTEMPT, lease_id=LEASE2, expected_lease_id=None)
    replacement = service.claim(_principal(), ATTEMPT, lease_id=LEASE2,
                                expected_lease_id=LEASE1)
    assert replacement.lease_sequence == 2
    with pytest.raises(FleetError, match="recovery_lease_unavailable"):
        service.resolve(_principal(), ATTEMPT, LEASE1, "target")
    with pytest.raises(FleetError, match="recovery_lease_conflict"):
        service.claim(_principal(), ATTEMPT, lease_id=LEASE3,
                      expected_lease_id=LEASE1)
    with registry.db.transaction() as conn:
        rows = conn.execute("SELECT issuing_session_id,carrier_session_id,superseded_at "
                            "FROM fleet_recovery_leases ORDER BY lease_sequence").fetchall()
        assert [row["issuing_session_id"] for row in rows] == [OLD_SESSION, OLD_SESSION]
        assert rows[0]["superseded_at"] is not None


def test_new_boot_replaces_old_carrier_without_changing_original_attempt(registry) -> None:
    _seed(registry)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_os_command_sessions SET revoked_at=1000 "
                     "WHERE command_session_id=%s", (NEW_SESSION,))
        conn.execute("UPDATE fleet_os_command_sessions SET revoked_at=NULL "
                     "WHERE command_session_id=%s", (OLD_SESSION,))
    service = _service(registry)
    service.claim(_old_principal(), ATTEMPT, lease_id=LEASE1, expected_lease_id=None)
    old_report = _report(
        LEASE1, installation_audience=OLD_AUDIENCE, kernel_boot_id=OLD_BOOT,
        offer_id=OLD_OFFER, command_session_id=OLD_SESSION,
    )
    assert service.record(_old_principal(), old_report) == "stored"
    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_os_command_sessions SET revoked_at=1000 "
                     "WHERE command_session_id=%s", (OLD_SESSION,))
        conn.execute("UPDATE fleet_os_command_sessions SET revoked_at=NULL "
                     "WHERE command_session_id=%s", (NEW_SESSION,))
    with pytest.raises(PrincipalError, match="os_command_session_unavailable"):
        service.resolve(_old_principal(), ATTEMPT, LEASE1, "target")
    next_lease = service.claim(_principal(), ATTEMPT, lease_id=LEASE2,
                               expected_lease_id=LEASE1)
    assert next_lease.carrier_session_id == NEW_SESSION
    assert next_lease.attempt.command_session_id == OLD_SESSION
    assert service.resolve(_principal(), ATTEMPT, LEASE2, "fallback").sha256 == FALLBACK
    assert service.record(_principal(), _report(LEASE2, 1)) == "stored"
    with registry.db.transaction() as conn:
        rows = conn.execute("SELECT carrier_session_id,report_sequence "
                            "FROM fleet_recovery_observations ORDER BY carrier_session_id")\
            .fetchall()
        assert {(row["carrier_session_id"], row["report_sequence"]) for row in rows} == {
            (OLD_SESSION, 1), (NEW_SESSION, 1),
        }


def test_recovery_observations_append_and_sequence_survives_renewal(registry) -> None:
    _seed(registry)
    service = _service(registry)
    service.claim(_principal(), ATTEMPT, lease_id=LEASE1, expected_lease_id=None)
    assert service.record(_principal(), _report(LEASE1, 1)) == "stored"
    assert service.record(_principal(), _report(LEASE1, 1)) == "replayed"
    service.claim(_principal(), ATTEMPT, lease_id=LEASE2, expected_lease_id=LEASE1)
    with pytest.raises(FleetError, match="recovery_lease_unavailable"):
        service.record(_principal(), _report(LEASE1, 2))
    with pytest.raises(FleetError, match="recovery_report_replay_conflict"):
        service.record(_principal(), _report(LEASE2, 1))
    assert service.record(_principal(), _report(LEASE2, 2)) == "stored"
    with pytest.raises(FleetError, match="recovery_report_replay_conflict"):
        service.record(_principal(), _report(LEASE2, 2, sampled_boottime_ms=101))
    with registry.db.transaction() as conn:
        rows = conn.execute("SELECT lease_id,report_sequence FROM fleet_recovery_observations "
                            "ORDER BY report_sequence").fetchall()
        assert [(row["lease_id"], row["report_sequence"]) for row in rows] == [
            (LEASE1, 1), (LEASE2, 2),
        ]


@pytest.mark.parametrize("change,error", [
    ("drain_phase", "recovery_drain_unavailable"),
    ("roots", "attempt_root_unavailable"),
    ("attempt_phase", "recovery_attempt_unavailable"),
])
def test_no_recovery_without_exact_committed_obligation(registry, change, error) -> None:
    values = {"drain_phase": {"drain_phase": "prepared"},
              "roots": {"roots": False},
              "attempt_phase": {"attempt_phase": "queued", "drain_phase": "prepared"}}
    _seed(registry, **values[change])
    with pytest.raises(FleetError, match=error):
        _service(registry).claim(_principal(), ATTEMPT, lease_id=LEASE1,
                                 expected_lease_id=None)


@pytest.mark.parametrize("missing", ["drain_link", "permit"])
def test_unpermitted_committed_drain_cannot_be_seeded(registry, missing) -> None:
    change = {"drain_link": None} if missing == "drain_link" else {"with_permit": False}
    with pytest.raises(CheckViolation, match="same-transaction fleet permit"):
        _seed(registry, **change)


def test_generation_session_and_lease_expiry_are_fences(registry) -> None:
    _seed(registry)
    service = _service(registry)
    service.claim(_principal(), ATTEMPT, lease_id=LEASE1, expected_lease_id=None)
    registry.clock.advance(121)
    with pytest.raises(FleetError, match="recovery_lease_unavailable"):
        service.resolve(_principal(), ATTEMPT, LEASE1, "fallback")
    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_device_lifecycle SET generation=2 WHERE device_id=%s",
                     (DEVICE,))
    with pytest.raises(PrincipalError, match="os_command_session_unavailable"):
        service.claim(_principal(), ATTEMPT, lease_id=LEASE2, expected_lease_id=LEASE1)


def test_release_and_explicit_lease_revocation_fence_reads_and_reports(registry) -> None:
    _seed(registry)
    service = _service(registry)
    service.claim(_principal(), ATTEMPT, lease_id=LEASE1, expected_lease_id=None)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_recovery_leases SET revoked_at=1000 WHERE lease_id=%s",
                     (LEASE1,))
    with pytest.raises(FleetError, match="recovery_lease_unavailable"):
        service.resolve(_principal(), ATTEMPT, LEASE1, "target")
    with pytest.raises(FleetError, match="recovery_lease_unavailable"):
        service.record(_principal(), _report(LEASE1))
    with pytest.raises(FleetError, match="recovery_lease_revoked"):
        service.claim(_principal(), ATTEMPT, lease_id=LEASE2, expected_lease_id=LEASE1)
    with pytest.raises(CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute(
                "INSERT INTO fleet_recovery_leases(lease_id,attempt_id,lease_sequence,"
                "predecessor_lease_id,device_id,device_generation,issuing_session_id,"
                "carrier_session_id,carrier_boot_id,carrier_offer_id,carrier_audience,"
                "carrier_trust_mode,issued_at,expires_at) "
                "VALUES(%s,%s,2,%s,%s,1,%s,%s,%s,%s,%s,'t2',1000,1100)",
                (LEASE2, ATTEMPT, LEASE1, DEVICE, OLD_SESSION, NEW_SESSION,
                 NEW_BOOT, NEW_OFFER, NEW_AUDIENCE),
            )
    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_app_attempts SET root_released_at=1000 "
                     "WHERE attempt_id=%s", (ATTEMPT,))
    with pytest.raises(FleetError, match="recovery_attempt_unavailable"):
        service.resolve(_principal(), ATTEMPT, LEASE1, "fallback")


def test_database_rejects_lease_mutation_and_observation_reorder(registry) -> None:
    _seed(registry)
    service = _service(registry)
    service.claim(_principal(), ATTEMPT, lease_id=LEASE1, expected_lease_id=None)
    service.record(_principal(), _report(LEASE1, 2))
    with pytest.raises(CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute("UPDATE fleet_recovery_leases SET carrier_session_id=%s "
                         "WHERE lease_id=%s", (OLD_SESSION, LEASE1))
    with pytest.raises(CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute("DELETE FROM fleet_recovery_observations")
    with pytest.raises(CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute("INSERT INTO fleet_recovery_observations(attempt_id,"
                         "carrier_session_id,report_sequence,lease_id,report_json,received_at) "
                         "VALUES(%s,%s,1,%s,'{}',1000)", (ATTEMPT, NEW_SESSION, LEASE1))


def test_wire_rejects_duplicate_keys_and_wrong_carrier(registry) -> None:
    _seed(registry)
    service = _service(registry)
    service.claim(_principal(), ATTEMPT, lease_id=LEASE1, expected_lease_id=None)
    with pytest.raises(ValueError):
        parse_os_recovery_report(b'{"schema":1,"schema":1}')
    with pytest.raises(FleetError, match="recovery_report_principal_mismatch"):
        service.record(_principal(), _report(LEASE1, installation_audience="other"))
    with pytest.raises(FleetError, match="recovery_report_attempt_mismatch"):
        service.record(_principal(), _report(LEASE1, active_sha256="a" * 64))
