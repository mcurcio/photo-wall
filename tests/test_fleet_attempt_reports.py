"""PostgreSQL contract for inert, authenticated-carrier OS attempt reports."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from uuid import UUID

import pytest
from psycopg.errors import CheckViolation
from pydantic import ValidationError

import central.fleet.attempt_reports as reports_module
import central.fleet.principal as principal_module
from central.content_catalog.catalog import device_id_for_serial
from central.fleet.attempt_reports import AttemptReportStore
from central.fleet.locks import FLEET_ASSET_LOCK
from central.fleet.models import FleetError
from central.fleet.principal import PrincipalError, VerifiedOsPrincipal
from contracts.app_process_proof import AppProofChallenge, LocalAppProof, ProcessIdentity
from contracts.os_attempt_report import OsAttemptReport
from player.identity import load_identity

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


def _principal() -> VerifiedOsPrincipal:
    return VerifiedOsPrincipal(
        device_id=DEVICE, device_generation=1, kernel_boot_id=BOOT,
        offer_id=OFFER, installation_audience=AUDIENCE, trust_mode="t1",
        command_session_id=SESSION, agent_key_sha256="f" * 64, expires_at=1100,
    )


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
          revoked_at: float | None = None) -> None:
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
        if schema == 1:
            conn.execute(
                "INSERT INTO fleet_app_attempts(attempt_id,device_id,offer_id,"
                "desired_revision,target_sha256,fallback_sha256,phase,command_id,drain_id,"
                "created_at,updated_at,device_generation,revoked_at,attempt_schema,"
                "installation_audience,kernel_boot_id,policy_source,base_sha256,base_abi,"
                "base_abi_source_manifest,target_tag,target_size,target_format,"
                "target_base_abi,target_source_manifest,fallback_size,fallback_base_abi,"
                "fallback_trust_mode,fallback_evidence_ref) "
                "VALUES(%s,%s,%s,1,%s,%s,%s,%s,%s,900,900,1,%s,1,%s,%s,'explicit',%s,%s,"
                "'manifest.v2.json','v1.0.1',123,'pw-player-data-v1',%s,"
                "'manifest.v2.json',80,%s,'t1','test-qualified-output')",
                (ATTEMPT, DEVICE, OFFER, TARGET, FALLBACK, phase, COMMAND, DRAIN,
                 revoked_at, AUDIENCE, BOOT, "b" * 64, "sha256:" + "c" * 64,
                 "sha256:" + "c" * 64, "sha256:" + "c" * 64),
            )
        else:
            conn.execute("INSERT INTO fleet_app_attempts(attempt_id,device_id,offer_id,"
                         "desired_revision,target_sha256,fallback_sha256,phase,command_id,"
                         "drain_id,created_at,updated_at,device_generation) "
                         "VALUES(%s,%s,%s,1,%s,%s,%s,%s,%s,900,900,1)",
                         (ATTEMPT, DEVICE, OFFER, TARGET, FALLBACK, phase, COMMAND, DRAIN))


def _count(registry) -> int:
    with registry.db.transaction() as conn:
        return conn.execute("SELECT count(*) AS n FROM fleet_os_attempt_reports").fetchone()["n"]


def test_report_is_append_only_carrier_claim_with_exact_replay(registry) -> None:
    _seed(registry)
    store = AttemptReportStore(registry.db, registry.clock)
    assert store.record(_principal(), _report()) == "stored"
    assert store.record(_principal(), _report()) == "replayed"
    with registry.db.transaction() as conn:
        row = conn.execute("SELECT * FROM fleet_os_attempt_reports").fetchone()
        attempt = conn.execute("SELECT phase FROM fleet_app_attempts WHERE attempt_id=%s",
                               (ATTEMPT,)).fetchone()
        assert row["command_session_id"] == SESSION and row["carrier_trust_mode"] == "t1"
        assert json.loads(row["report_json"])["attempt_id"] == str(ATTEMPT)
        assert attempt["phase"] == "stop_committed"
        assert conn.execute("SELECT count(*) AS n FROM fleet_accepted_artifacts").fetchone()["n"] == 0
    assert _count(registry) == 1


def test_sequence_is_monotonic_and_same_sequence_conflict_fails(registry) -> None:
    _seed(registry)
    store = AttemptReportStore(registry.db, registry.clock)
    assert store.record(_principal(), _report(2)) == "stored"
    with pytest.raises(FleetError, match="attempt_report_sequence_stale"):
        store.record(_principal(), _report(1))
    with pytest.raises(FleetError, match="attempt_report_replay_conflict"):
        store.record(_principal(), _report(2, sampled_boottime_ms=1235))
    assert store.record(_principal(), _report(3, executor_state="rolled_back",
                                               active_sha256=FALLBACK)) == "stored"
    assert store.record(_principal(), _report(2)) == "replayed"
    assert _count(registry) == 2


@pytest.mark.parametrize("changed", [
    {"device_id": "device-" + "0" * 64}, {"device_generation": 2},
    {"kernel_boot_id": UUID(int=999)}, {"offer_id": UUID(int=999)},
    {"installation_audience": "installation-two"},
    {"command_session_id": UUID(int=999)}, {"attempt_id": UUID(int=999)},
    {"command_id": UUID(int=999)}, {"drain_id": UUID(int=999)},
])
def test_report_cannot_cross_principal_or_attempt_boundary(registry, changed) -> None:
    _seed(registry)
    with pytest.raises(FleetError, match="attempt_report_.*_mismatch"):
        AttemptReportStore(registry.db, registry.clock).record(_principal(), _report(**changed))
    assert _count(registry) == 0


def test_proof_session_and_carrier_trust_match_report_and_principal(registry) -> None:
    process = ProcessIdentity(pid=123, start_ticks=456, invocation_id="c" * 32,
                              cgroup_unit="photo-wall-player.service")

    def proof(*, session: UUID, trust: str) -> LocalAppProof:
        challenge = AppProofChallenge(
            nonce="a" * 64, installation_audience=AUDIENCE,
            device_id=DEVICE, device_generation=1, kernel_boot_id=BOOT,
            offer_id=OFFER, command_session_id=session, attempt_id=ATTEMPT,
            command_id=COMMAND, trust_mode=trust, claimed_player_id="p-" + "b" * 32,
            claimed_authority_epoch=1, process=process,
        )
        return LocalAppProof(challenge=challenge,
                             response=load_identity().sign_app_proof(challenge),
                             verified_boottime_ms=100)

    with pytest.raises(ValidationError, match="attempt_report_proof_mismatch"):
        _report(running_sha256=TARGET, running_process=process,
                app_proof=proof(session=UUID(int=999), trust="t1"))

    _seed(registry)
    report = _report(running_sha256=TARGET, running_process=process,
                     app_proof=proof(session=SESSION, trust="t2"))
    with pytest.raises(FleetError, match="attempt_report_principal_mismatch"):
        AttemptReportStore(registry.db, registry.clock).record(_principal(), report)
    assert _count(registry) == 0


def test_current_session_required_even_for_replay(registry) -> None:
    _seed(registry)
    store = AttemptReportStore(registry.db, registry.clock)
    store.record(_principal(), _report())
    registry.clock.advance(101)
    with pytest.raises(PrincipalError, match="os_command_session_unavailable"):
        store.record(_principal(), _report())
    assert _count(registry) == 1


@pytest.mark.parametrize("reverse_utc", [False, True])
def test_report_cannot_enter_after_session_expires_during_fleet_lock_wait(
    registry, monkeypatch, reverse_utc,
) -> None:
    _seed(registry)
    waiting = Event()
    lock = principal_module.lock_fleet_assets_in

    def signalled_lock(conn):
        waiting.set()
        lock(conn)

    monkeypatch.setattr(principal_module, "lock_fleet_assets_in", signalled_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with registry.db.transaction() as blocker:
            blocker.execute("SELECT pg_advisory_xact_lock(%s)", (FLEET_ASSET_LOCK,))
            future = pool.submit(
                AttemptReportStore(registry.db, registry.clock).record,
                _principal(), _report(),
            )
            assert waiting.wait(timeout=3)
            assert not future.done()
            registry.clock.advance(101)
            if reverse_utc:
                registry.clock.step_utc(-101)
        with pytest.raises(PrincipalError, match="os_command_session_unavailable"):
            future.result(timeout=5)
    assert _count(registry) == 0


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("utc_steps_back", [False, True])
def test_session_expiry_during_lock_wait_blocks_insert_and_replay(
    registry, monkeypatch, replay, utc_steps_back,
) -> None:
    _seed(registry)
    store = AttemptReportStore(registry.db, registry.clock)
    if replay:
        assert store.record(_principal(), _report()) == "stored"
    guard = reports_module.require_current_principal_in

    def delayed_guard(conn, principal, *, clock):
        admission = guard(conn, principal, clock=clock)
        # Simulate waiting after the guard sampled UTC, while its session row
        # remains locked. A backward wall-clock step cannot undo elapsed time.
        registry.clock.advance(101)
        if utc_steps_back:
            registry.clock.step_utc(-101)
        return admission

    monkeypatch.setattr(reports_module, "require_current_principal_in", delayed_guard)
    with pytest.raises(PrincipalError, match="os_command_session_unavailable"):
        store.record(_principal(), _report())
    assert _count(registry) == int(replay)


def test_received_at_samples_after_authority_lock_wait(registry, monkeypatch) -> None:
    _seed(registry)
    guard = reports_module.require_current_principal_in

    def delayed_guard(conn, principal, *, clock):
        admission = guard(conn, principal, clock=clock)
        registry.clock.advance(5)
        return admission

    monkeypatch.setattr(reports_module, "require_current_principal_in", delayed_guard)
    assert AttemptReportStore(registry.db, registry.clock).record(_principal(), _report()) == \
        "stored"
    with registry.db.transaction() as conn:
        recorded = conn.execute("SELECT received_at FROM fleet_os_attempt_reports").fetchone()
    assert recorded["received_at"] == 1005


def test_revocation_and_legacy_attempt_fences(registry) -> None:
    _seed(registry, phase="queued")
    store = AttemptReportStore(registry.db, registry.clock)
    with pytest.raises(FleetError, match="attempt_report_attempt_mismatch"):
        store.record(_principal(), _report())
    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_app_attempts SET phase='stop_committed',"
                     "revoked_at=1000 WHERE attempt_id=%s", (ATTEMPT,))
    assert store.record(_principal(), _report()) == "stored"

    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_os_command_sessions SET revoked_at=1000 "
                     "WHERE command_session_id=%s", (SESSION,))
    with pytest.raises(PrincipalError, match="os_command_session_unavailable"):
        store.record(_principal(), _report(2))
    assert _count(registry) == 1


def test_schema_zero_attempt_does_not_admit_report(registry) -> None:
    _seed(registry, schema=0)
    with pytest.raises(FleetError, match="attempt_report_attempt_mismatch"):
        AttemptReportStore(registry.db, registry.clock).record(_principal(), _report())
    assert _count(registry) == 0


def test_unvalidated_model_copy_cannot_bypass_report_contract(registry) -> None:
    _seed(registry)
    forged = _report().model_copy(update={"device_generation": "one"})
    with pytest.raises(FleetError, match="attempt_report_invalid"):
        AttemptReportStore(registry.db, registry.clock).record(_principal(), forged)
    assert _count(registry) == 0


def test_concurrent_exact_replay_has_one_row(registry) -> None:
    _seed(registry)
    store = AttemptReportStore(registry.db, registry.clock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.record(_principal(), _report()), range(2)))
    assert sorted(results) == ["replayed", "stored"]
    assert _count(registry) == 1


def test_database_prevents_report_mutation_and_out_of_order_direct_insert(registry) -> None:
    _seed(registry)
    store = AttemptReportStore(registry.db, registry.clock)
    store.record(_principal(), _report(2))
    with pytest.raises(CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute("UPDATE fleet_os_attempt_reports SET report_json='{}' "
                         "WHERE attempt_id=%s", (ATTEMPT,))
    with pytest.raises(CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute("DELETE FROM fleet_os_attempt_reports WHERE attempt_id=%s",
                         (ATTEMPT,))
    with pytest.raises(CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute("INSERT INTO fleet_os_attempt_reports(attempt_id,report_sequence,"
                         "command_session_id,carrier_trust_mode,report_json,received_at) "
                         "VALUES(%s,1,%s,'t1','{}',1000)", (ATTEMPT, SESSION))
    assert _count(registry) == 1


def test_replaced_principal_session_cannot_replay_old_session(registry) -> None:
    _seed(registry)
    store = AttemptReportStore(registry.db, registry.clock)
    store.record(_principal(), _report())
    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_os_command_sessions SET revoked_at=1000 "
                     "WHERE command_session_id=%s", (SESSION,))
        conn.execute("INSERT INTO fleet_os_command_sessions(command_session_id,device_id,"
                     "device_generation,kernel_boot_id,offer_id,installation_audience,"
                     "trust_mode,agent_key_sha256,verifier_ref,issued_at,expires_at) "
                     "VALUES(%s,%s,1,%s,%s,%s,'t1',%s,'replacement',900,1100)",
                     (UUID(int=799), DEVICE, BOOT, OFFER, AUDIENCE, "f" * 64))
    other = replace(_principal(), command_session_id=UUID(int=799))
    with pytest.raises(FleetError, match="attempt_report_principal_mismatch"):
        store.record(other, _report())
    assert _count(registry) == 1
