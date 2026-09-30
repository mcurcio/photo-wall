"""Managed-update evidence remains diagnostic until its causal joins exist."""

import base64
from dataclasses import replace
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from central.fleet.acceptance_evidence import (
    AcceptanceEvidence,
    AppControl,
    AttemptIdentity,
    CurrentAuthority,
    ExpectedOutput,
    OutputHandoff,
    StoredReport,
    assess_acceptance,
)
from contracts.app_process_proof import (
    AppProofChallenge,
    AppProofChallengeV2,
    AppProofResponse,
    LocalAppProof,
    LocalAppProofV2,
    ProcessIdentity,
    app_proof_message,
    app_proof_message_v2,
)
from contracts.os_attempt_report import (
    MAX_ATTEMPT_REPORT_BYTES,
    OsAttemptReport,
    parse_os_attempt_report,
)
from contracts.player_control import ControlAppliedReceipt

DEVICE = "device-" + "a" * 64
PLAYER = "p-" + "b" * 32
TARGET = "c" * 64
FALLBACK = "d" * 64
BOOT = UUID(int=1)
OFFER = UUID(int=2)
SESSION = UUID(int=3)
ATTEMPT = UUID(int=4)
COMMAND = UUID(int=5)
DRAIN = UUID(int=6)
PROCESS = ProcessIdentity(pid=123, start_ticks=456, invocation_id="e" * 32,
                          cgroup_unit="photo-wall-player.service")
KEY = Ed25519PrivateKey.generate()
PUBLIC_KEY = KEY.public_key().public_bytes(
    encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw).hex()
ATTEMPT_REF = AttemptIdentity(ATTEMPT, DEVICE, 2, "installation-one", BOOT, OFFER,
                              SESSION, COMMAND, DRAIN, TARGET, FALLBACK, "starting")
AUTHORITY = CurrentAuthority(2, BOOT, SESSION, "t1", True, True)
CONTROL = AppControl(PLAYER, PUBLIC_KEY, 7, 2, "negotiated", 2, "delivery-two",
                     "f" * 64, 100.0, "applied", 2, "delivery-two", "f" * 64, 100.0)
CONTROL_V2 = replace(CONTROL, applied_delivery_id="a" * 32,
                     last_delivery_id="a" * 32, issued_sequence=2,
                     applied_ack_nonce="b" * 64)
RECEIPT = ControlAppliedReceipt(
    authority_epoch=7, delivery_id="a" * 32, delivery_sequence=2,
    state_digest="f" * 64, ack_nonce="b" * 64)
EXPECTED = ExpectedOutput("HDMI-A-1", "1920x1080@60", None, None, None, True)
HANDOFF = OutputHandoff("HDMI-A-1", "1920x1080@60", None, None, None, True,
                        PROCESS, 7, 40000, "base_display_host")


def proof(sequence: int, sampled_ms: int, *, process=PROCESS, key=KEY,
          claimed_epoch=7, command_session_id=SESSION):
    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw).hex()
    challenge = AppProofChallenge(
        nonce=f"{sequence:064x}", installation_audience="installation-one",
        device_id=DEVICE, device_generation=2, kernel_boot_id=BOOT, offer_id=OFFER,
        command_session_id=command_session_id, attempt_id=ATTEMPT, command_id=COMMAND,
        trust_mode="t1", claimed_player_id=PLAYER, claimed_authority_epoch=claimed_epoch,
        process=process)
    response = AppProofResponse(
        nonce=challenge.nonce, public_key=public_key,
        signature=base64.b64encode(key.sign(app_proof_message(challenge))).decode())
    return LocalAppProof(challenge=challenge, response=response,
                         verified_boottime_ms=sampled_ms - 1)


def row(sequence: int, sampled_ms: int, *, state="committed", digest=TARGET,
        process=PROCESS, fault=None, key=KEY, claimed_epoch=7):
    report = OsAttemptReport(
        device_id=DEVICE, device_generation=2, installation_audience="installation-one",
        kernel_boot_id=BOOT, offer_id=OFFER, command_session_id=SESSION,
        attempt_id=ATTEMPT, command_id=COMMAND, drain_id=DRAIN,
        report_sequence=sequence, sampled_boottime_ms=sampled_ms, executor_state=state,
        active_sha256=digest, running_sha256=digest, running_process=process,
        app_proof=proof(sequence, sampled_ms, process=process, key=key,
                        claimed_epoch=claimed_epoch), fault_code=fault)
    return StoredReport(report, SESSION, "t1", 100.0 + sequence)


def v2_row(sequence: int, sampled_ms: int, *, receipt=RECEIPT,
           root=TARGET, key=KEY) -> StoredReport:
    challenge = AppProofChallengeV2(
        nonce=f"{sequence:064x}", installation_audience="installation-one",
        device_id=DEVICE, device_generation=2, kernel_boot_id=BOOT, offer_id=OFFER,
        command_session_id=SESSION, attempt_id=ATTEMPT, command_id=COMMAND,
        trust_mode="t1", claimed_player_id=PLAYER, claimed_authority_epoch=7,
        process=PROCESS, active_sha256=root, receipt=receipt)
    response = AppProofResponse(
        nonce=challenge.nonce,
        public_key=key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw).hex(),
        signature=base64.b64encode(key.sign(app_proof_message_v2(challenge))).decode())
    proof = LocalAppProofV2(challenge=challenge, response=response,
                            verified_boottime_ms=sampled_ms - 1)
    report = OsAttemptReport(
        device_id=DEVICE, device_generation=2, installation_audience="installation-one",
        kernel_boot_id=BOOT, offer_id=OFFER, command_session_id=SESSION,
        attempt_id=ATTEMPT, command_id=COMMAND, drain_id=DRAIN,
        report_sequence=sequence, sampled_boottime_ms=sampled_ms,
        executor_state="committed", active_sha256=TARGET, running_sha256=TARGET,
        running_process=PROCESS, app_proof=proof)
    return StoredReport(report, SESSION, "t1", 100.0 + sequence)


def evidence(**changes):
    defaults = dict(
        attempt=ATTEMPT_REF, authority=AUTHORITY,
        reports=(row(1, 10000), row(2, 41000)), control=CONTROL,
        expected_outputs=(EXPECTED,), output_handoffs=(HANDOFF,),
        output_inventory_matches=True, target_rooted=True, minimum_stable_ms=30000)
    return AcceptanceEvidence(**(defaults | changes))


def test_strong_current_v1_facts_still_cannot_promote_without_delivery_bound_proof():
    result = assess_acceptance(evidence())
    assert result.package_state == "committed_target"
    assert result.proof_matched and result.process_stable
    assert result.output_state == "matching_base_claims"
    assert result.control_state == "applied_unlinked"
    assert result.refusals == ("control_delivery_unlinked",)
    assert result.promotion_allowed is False


def test_v2_signed_receipt_links_current_applied_row_but_does_not_promote():
    reports = (v2_row(1, 10000), v2_row(2, 41000))
    result = assess_acceptance(evidence(reports=reports, control=CONTROL_V2))
    assert result.package_state == "committed_target"
    assert result.proof_matched and result.process_stable
    assert result.control_state == "applied_process_linked"
    assert result.output_state == "matching_base_claims"
    assert result.refusals == ("acceptance_writer_unavailable",)
    assert result.promotion_allowed is False
    encoded = reports[-1].report.model_dump_json(by_alias=True).encode()
    assert len(encoded) <= MAX_ATTEMPT_REPORT_BYTES
    assert parse_os_attempt_report(encoded) == reports[-1].report


@pytest.mark.parametrize("control", [
    replace(CONTROL_V2, applied_ack_nonce="c" * 64),
    replace(CONTROL_V2, issued_sequence=3, pending_id="c" * 32),
    replace(CONTROL_V2, applied_delivery_id="c" * 32),
    replace(CONTROL_V2, applied_digest="e" * 64),
    replace(CONTROL_V2, last_result="rejected", last_result_sequence=3),
    replace(CONTROL_V2, authority_epoch=8),
    replace(CONTROL_V2, public_key="d" * 64),
])
def test_v2_receipt_requires_exact_current_registry_row(control):
    result = assess_acceptance(evidence(
        reports=(v2_row(1, 10000), v2_row(2, 41000)), control=control))
    assert result.control_state != "applied_process_linked"
    assert not result.proof_matched
    assert not result.process_stable
    assert result.promotion_allowed is False


def test_v2_process_link_needs_sustained_samples_and_each_output():
    reports = (v2_row(1, 10000), v2_row(2, 20000))
    short = assess_acceptance(evidence(reports=reports, control=CONTROL_V2))
    assert short.control_state == "applied_process_linked"
    assert "sustained_process_evidence_missing" in short.refusals
    missing = assess_acceptance(evidence(
        reports=(v2_row(1, 10000), v2_row(2, 41000)),
        control=CONTROL_V2, output_handoffs=()))
    assert missing.control_state == "applied_process_linked"
    assert "output_handoff_incomplete" in missing.refusals
    assert missing.promotion_allowed is False


def test_v2_linked_stability_cannot_borrow_old_v1_or_other_receipt_samples():
    for first in (
        row(1, 10000),
        v2_row(1, 10000, receipt=RECEIPT.model_copy(
            update={"ack_nonce": "c" * 64})),
    ):
        result = assess_acceptance(evidence(
            reports=(first, v2_row(2, 41000)), control=CONTROL_V2))
        assert result.control_state == "applied_process_linked"
        assert result.proof_matched
        assert not result.process_stable
        assert "sustained_process_evidence_missing" in result.refusals
        assert result.promotion_allowed is False


def test_v2_report_rejects_stale_replayed_proof_and_mismatched_root():
    first = v2_row(1, 10000).report
    with pytest.raises(ValueError, match="attempt_report_v2_proof_stale_or_root_mismatch"):
        OsAttemptReport(**(first.model_dump(by_alias=True) | {
            "report_sequence": 2, "sampled_boottime_ms": 41000}))
    with pytest.raises(ValueError, match="attempt_report_v2_proof_stale_or_root_mismatch"):
        v2_row(1, 10000, root=FALLBACK)


def test_later_rejection_does_not_refresh_historical_applied_control():
    rejected = replace(CONTROL, last_result="rejected", last_result_sequence=3,
                       last_delivery_id="delivery-three", last_result_at=120.0)
    result = assess_acceptance(evidence(control=rejected))
    assert result.historical_applied_at == 100.0
    assert result.latest_control_result == "rejected"
    assert result.control_state == "historical_applied_latest_rejected"
    assert "control_latest_rejected" in result.refusals
    assert result.promotion_allowed is False


def test_rolled_back_fallback_is_not_target_success():
    reports = (row(1, 10000), row(2, 41000, state="rolled_back", digest=FALLBACK))
    result = assess_acceptance(evidence(reports=reports))
    assert result.package_state == "rolled_back"
    assert "target_not_active" in result.refusals
    assert not result.process_stable


def test_old_boot_report_after_new_boot_cannot_supply_current_evidence():
    new_authority = replace(AUTHORITY, kernel_boot_id=UUID(int=99),
                            command_session_id=UUID(int=98))
    result = assess_acceptance(evidence(authority=new_authority))
    assert "current_authority_unavailable" in result.refusals
    assert "os_report_context_conflict" in result.refusals
    assert result.latest_report_sequence is None


def test_new_same_boot_session_cannot_reinterpret_old_attempt_report():
    new_session = UUID(int=80)
    reports = (replace(row(1, 10000), carrier_session_id=new_session), row(2, 41000))
    result = assess_acceptance(evidence(reports=reports))
    assert result.package_state == "unknown"
    assert "os_report_context_conflict" in result.refusals
    assert result.promotion_allowed is False


@pytest.mark.parametrize("changes,expected_reason", [
    ({"output_handoffs": ()}, "output_handoff_incomplete"),
    ({"output_handoffs": (replace(HANDOFF, calibration_revision=5),)},
     "output_handoff_incomplete"),
    ({"output_inventory_matches": False}, "output_inventory_unavailable"),
    ({"target_rooted": False}, "target_root_unavailable"),
    ({"expected_outputs": (EXPECTED,
                           replace(EXPECTED, output_id="HDMI-A-2"))},
     "output_handoff_incomplete"),
])
def test_missing_or_stale_output_and_bytes_keep_acceptance_closed(changes, expected_reason):
    result = assess_acceptance(evidence(**changes))
    assert expected_reason in result.refusals
    assert result.promotion_allowed is False


def test_process_key_epoch_and_signature_must_match_current_registry():
    other_key = Ed25519PrivateKey.generate()
    for reports in (
        (row(1, 10000, key=other_key), row(2, 41000, key=other_key)),
        (row(1, 10000, claimed_epoch=8), row(2, 41000, claimed_epoch=8)),
        (row(1, 10000), StoredReport(
            row(2, 41000).report.model_copy(update={
                "app_proof": row(2, 41000).report.app_proof.model_copy(update={
                    "response": row(2, 41000).report.app_proof.response.model_copy(update={
                        "signature": "A" * 86 + "=="})})}), SESSION, "t1", 102.0)),
    ):
        result = assess_acceptance(evidence(reports=reports))
        assert "app_process_proof_unmatched" in result.refusals
        assert not result.process_stable
        assert not result.promotion_allowed


def test_short_or_faulted_window_is_not_sustained_process_evidence():
    for reports in (
        (row(1, 10000), row(2, 20000)),
        (row(1, 10000, fault="renderer_fault"), row(2, 41000)),
    ):
        result = assess_acceptance(evidence(reports=reports))
        assert "sustained_process_evidence_missing" in result.refusals
        assert not result.process_stable


def test_queued_attempt_and_missing_command_session_cannot_accept_reports():
    queued = replace(ATTEMPT_REF, phase="queued", command_session_id=None,
                     command_id=None, drain_id=None)
    result = assess_acceptance(evidence(attempt=queued))
    assert "attempt_not_stopped" in result.refusals
    assert "attempt_session_unbound" in result.refusals
    assert result.package_state == "unknown"
    assert result.promotion_allowed is False
