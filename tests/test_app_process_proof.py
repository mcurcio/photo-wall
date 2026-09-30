"""The local app key proof stays distinct from OS command authority."""

from dataclasses import replace
from uuid import UUID

import pytest
from pydantic import ValidationError

from appliance.app_process_proof import (
    CurrentAttemptContext,
    LocalAppProofVerifier,
    LocalProofError,
    PeerCredentials,
)
from contracts.app_process_proof import (
    AppProofChallenge,
    AppProofResponse,
    ProcessIdentity,
    app_proof_message,
    parse_app_proof_challenge,
    parse_app_proof_response,
)
from contracts.os_attempt_report import OsAttemptReport, parse_os_attempt_report
from player.identity import load_identity

BOOT = UUID("10000000-0000-0000-0000-000000000001")
OFFER = UUID("20000000-0000-0000-0000-000000000002")
ATTEMPT = UUID("30000000-0000-0000-0000-000000000003")
COMMAND = UUID("40000000-0000-0000-0000-000000000004")
SESSION = UUID("50000000-0000-0000-0000-000000000005")
DRAIN = UUID("60000000-0000-0000-0000-000000000006")
PLAYER = "p-" + "a" * 32
DEVICE = "device-" + "b" * 64
PROCESS = ProcessIdentity(pid=123, start_ticks=456, invocation_id="c" * 32,
                          cgroup_unit="photo-wall-player.service")
CONTEXT = CurrentAttemptContext("installation-one", DEVICE, 2, BOOT, OFFER, ATTEMPT, COMMAND)


def verifier_state():
    state = {"peer": PeerCredentials(pid=123, uid=10001, gid=10001),
             "main": PROCESS, "process": PROCESS, "context": CONTEXT, "now": 10.0}
    verifier = LocalAppProofVerifier(
        expected_app_uid=10001,
        peer_sampler=lambda _: state["peer"],
        main_process_sampler=lambda: state["main"],
        peer_process_sampler=lambda _: state["process"],
        current_attempt=lambda: state["context"],
        boottime=lambda: state["now"],
        nonce_bytes=lambda _: bytes.fromhex("d" * 64),
    )
    return verifier, state


def test_existing_process_key_signs_domain_separated_attempt_bound_challenge():
    verifier, _ = verifier_state()
    challenge = verifier.begin(object(), PLAYER, 7)
    assert b"photo-wall-local-app-proof-v1" in app_proof_message(challenge)
    assert challenge.attempt_id == ATTEMPT and challenge.command_id == COMMAND
    assert challenge.device_id == DEVICE and challenge.kernel_boot_id == BOOT
    assert b"os_command_session" not in app_proof_message(challenge)
    identity = load_identity()
    proof = verifier.verify(object(), identity.sign_app_proof(challenge))
    assert proof.response.public_key == identity.public_key
    assert proof.verified_boottime_ms == 10000
    with pytest.raises(LocalProofError, match="app_proof_expired_or_used"):
        verifier.verify(object(), proof.response)


def test_proof_rejects_wrong_peer_and_process_identity():
    for changed in (
        {"peer": PeerCredentials(pid=123, uid=10002, gid=10001)},
        {"main": PROCESS.model_copy(update={"invocation_id": "e" * 32})},
        {"process": PROCESS.model_copy(update={"start_ticks": 457})},
        {"main": PROCESS.model_copy(update={"cgroup_unit": "other.service"}),
         "process": PROCESS.model_copy(update={"cgroup_unit": "other.service"})},
        {"process": PROCESS.model_copy(update={"start_ticks": 457}),
         "main": PROCESS.model_copy(update={"start_ticks": 457})},
    ):
        verifier, state = verifier_state()
        challenge = verifier.begin(object(), PLAYER, 7)
        state.update(changed)
        with pytest.raises(LocalProofError):
            verifier.verify(object(), load_identity().sign_app_proof(challenge))


def test_proof_rejects_attempt_switch_expiry_and_invalid_signature():
    verifier, state = verifier_state()
    challenge = verifier.begin(object(), PLAYER, 7)
    state["context"] = replace(CONTEXT, attempt_id=UUID(int=9))
    with pytest.raises(LocalProofError, match="app_proof_context_changed"):
        verifier.verify(object(), load_identity().sign_app_proof(challenge))

    verifier, state = verifier_state()
    challenge = verifier.begin(object(), PLAYER, 7)
    state["now"] = 25.0
    with pytest.raises(LocalProofError, match="app_proof_expired_or_used"):
        verifier.verify(object(), load_identity().sign_app_proof(challenge))

    verifier, _ = verifier_state()
    challenge = verifier.begin(object(), PLAYER, 7)
    response = load_identity().sign_app_proof(challenge)
    forged = AppProofResponse(nonce=response.nonce, public_key=load_identity().public_key,
                              signature=response.signature)
    with pytest.raises(LocalProofError, match="app_proof_signature_invalid"):
        verifier.verify(object(), forged)


def test_proof_detects_context_or_process_change_during_exchange():
    verifier, _ = verifier_state()
    calls = 0

    def switching_context():
        nonlocal calls
        calls += 1
        return CONTEXT if calls == 1 else replace(CONTEXT, command_id=UUID(int=9))

    verifier.current_attempt = switching_context
    with pytest.raises(LocalProofError, match="app_proof_context_changed"):
        verifier.begin(object(), PLAYER, 7)

    verifier, _ = verifier_state()
    challenge = verifier.begin(object(), PLAYER, 7)
    samples = 0

    def switching_process(_):
        nonlocal samples
        samples += 1
        return PROCESS if samples == 1 else PROCESS.model_copy(update={"start_ticks": 457})

    verifier.peer_process_sampler = switching_process
    with pytest.raises(LocalProofError, match="app_process_mismatch"):
        verifier.verify(object(), load_identity().sign_app_proof(challenge))


def test_proof_contract_bounds_and_report_cannot_reuse_another_attempt():
    verifier, _ = verifier_state()
    challenge = verifier.begin(object(), PLAYER, 7)
    proof = verifier.verify(object(), load_identity().sign_app_proof(challenge))
    report = OsAttemptReport(
        device_id=DEVICE, device_generation=2, installation_audience="installation-one",
        kernel_boot_id=BOOT, offer_id=OFFER, command_session_id=SESSION,
        attempt_id=ATTEMPT, command_id=COMMAND, drain_id=DRAIN,
        report_sequence=1, sampled_boottime_ms=10001, executor_state="committed",
        active_sha256="f" * 64, running_sha256="f" * 64,
        running_process=PROCESS, app_proof=proof,
    )
    assert report.app_proof == proof
    assert parse_os_attempt_report(report.model_dump_json(by_alias=True).encode()) == report
    with pytest.raises(ValidationError, match="attempt_report_proof_mismatch"):
        OsAttemptReport(**{**report.model_dump(by_alias=True), "attempt_id": UUID(int=9)})
    with pytest.raises(ValidationError, match="attempt_report_t0_audience"):
        OsAttemptReport(**{**report.model_dump(by_alias=True), "installation_audience":
                           "photo-wall-central-t0"})
    with pytest.raises(ValidationError):
        AppProofChallenge(**{**challenge.model_dump(by_alias=True), "nonce": "short"})
    with pytest.raises(ValidationError):
        AppProofChallenge(**{**challenge.model_dump(by_alias=True),
                             "installation_audience": "invalid audience"})


def test_wire_parsers_reject_duplicate_and_oversized_proof_fields():
    verifier, _ = verifier_state()
    challenge = verifier.begin(object(), PLAYER, 7)
    response = load_identity().sign_app_proof(challenge)
    assert parse_app_proof_challenge(challenge.model_dump_json(by_alias=True).encode()) == challenge
    assert parse_app_proof_response(response.model_dump_json().encode()) == response
    with pytest.raises(ValueError, match="app_proof_response_invalid_json"):
        parse_app_proof_response(
            (response.model_dump_json()[:-1] + ',"nonce":"' + response.nonce + '"}').encode(),
        )
    with pytest.raises(ValueError, match="app_proof_challenge_invalid_json"):
        parse_app_proof_challenge(b" " * 2049)
    report = OsAttemptReport(
        device_id=DEVICE, device_generation=2, installation_audience="installation-one",
        kernel_boot_id=BOOT, offer_id=OFFER, command_session_id=SESSION,
        attempt_id=ATTEMPT, command_id=COMMAND, drain_id=DRAIN,
        report_sequence=1, sampled_boottime_ms=10001, executor_state="committed",
        active_sha256="f" * 64,
    )
    encoded = report.model_dump_json(by_alias=True)
    duplicate = encoded[:-1] + ',"report_sequence":2}'
    with pytest.raises(ValueError, match="attempt_report_invalid_json"):
        parse_os_attempt_report(duplicate.encode())
    with pytest.raises(ValidationError, match="attempt_report_running_digest_mismatch"):
        OsAttemptReport(**{**report.model_dump(by_alias=True), "running_sha256": "e" * 64,
                           "running_process": PROCESS})
