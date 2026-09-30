"""Read-only classification of a managed app attempt's currently available evidence.

This projector cannot authorize promotion, a command, or drain release. Current
schema-one local proof names a Player epoch but does not bind a specific applied
control delivery to the OS-observed process. A future causal proof and
base-observed Output handoff contract must be added before an acceptance writer
can consume these classifications.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from contracts.app_process_proof import ProcessIdentity, app_proof_message
from contracts.os_attempt_report import OsAttemptReport

PackageState = Literal["unknown", "committed_target", "rolled_back", "recovery_required",
                       "conflicting"]
ControlState = Literal["unavailable", "applied_unlinked", "latest_rejected",
                       "historical_applied_latest_rejected", "incoherent"]
OutputState = Literal["unavailable", "incomplete", "matching_base_claims"]


@dataclass(frozen=True, slots=True)
class AttemptIdentity:
    attempt_id: UUID
    device_id: str
    device_generation: int
    installation_audience: str
    kernel_boot_id: UUID
    offer_id: UUID
    command_session_id: UUID | None
    command_id: UUID | None
    drain_id: UUID | None
    target_sha256: str
    fallback_sha256: str
    phase: str


@dataclass(frozen=True, slots=True)
class CurrentAuthority:
    """A query adapter's current, locked lifecycle and OS-session snapshot."""

    device_generation: int
    kernel_boot_id: UUID
    command_session_id: UUID | None
    trust_mode: Literal["t1", "t2"] | None
    lifecycle_active: bool
    session_active: bool


@dataclass(frozen=True, slots=True)
class StoredReport:
    """A report already admitted by AttemptReportStore under a verified carrier."""

    report: OsAttemptReport
    carrier_session_id: UUID
    carrier_trust_mode: Literal["t1", "t2"]
    received_at: float


@dataclass(frozen=True, slots=True)
class AppControl:
    """Current Registry row; an older applied ACK is retained as history."""

    player_id: str
    public_key: str
    authority_epoch: int
    schema_version: int
    status: str
    applied_sequence: int
    applied_delivery_id: str | None
    applied_digest: str | None
    applied_at: float | None
    last_result: str | None
    last_result_sequence: int | None
    last_delivery_id: str | None
    last_result_digest: str | None
    last_result_at: float | None


@dataclass(frozen=True, slots=True)
class ExpectedOutput:
    output_id: str
    mode: str
    binding_generation: int | None
    configuration_revision: int | None
    calibration_revision: int | None
    synthetic_frame: bool


@dataclass(frozen=True, slots=True)
class OutputHandoff:
    """Future base display-host observation, never an app-only Observation."""

    output_id: str
    mode: str
    binding_generation: int | None
    configuration_revision: int | None
    calibration_revision: int | None
    synthetic_frame: bool
    process: ProcessIdentity
    authority_epoch: int
    observed_boottime_ms: int
    source: Literal["base_display_host"]


@dataclass(frozen=True, slots=True)
class AcceptanceEvidence:
    attempt: AttemptIdentity
    authority: CurrentAuthority
    reports: tuple[StoredReport, ...]
    control: AppControl | None
    expected_outputs: tuple[ExpectedOutput, ...]
    output_handoffs: tuple[OutputHandoff, ...]
    output_inventory_matches: bool
    target_rooted: bool
    minimum_stable_ms: int


@dataclass(frozen=True, slots=True)
class AcceptanceAssessment:
    package_state: PackageState
    control_state: ControlState
    output_state: OutputState
    proof_matched: bool
    process_stable: bool
    latest_report_sequence: int | None
    latest_report_received_at: float | None
    historical_applied_at: float | None
    latest_control_result: str | None
    refusals: tuple[str, ...]
    promotion_allowed: Literal[False] = False


def _report_matches(row: StoredReport, attempt: AttemptIdentity,
                    authority: CurrentAuthority) -> bool:
    report = row.report
    return (
        report.attempt_id == attempt.attempt_id
        and report.device_id == attempt.device_id
        and report.device_generation == attempt.device_generation
        and report.installation_audience == attempt.installation_audience
        and report.kernel_boot_id == attempt.kernel_boot_id
        and report.kernel_boot_id == authority.kernel_boot_id
        and report.offer_id == attempt.offer_id
        and report.command_session_id == attempt.command_session_id
        and report.command_id == attempt.command_id
        and report.drain_id == attempt.drain_id
        and row.carrier_session_id == attempt.command_session_id
        and row.carrier_session_id == authority.command_session_id
        and row.carrier_trust_mode == authority.trust_mode
    )


def _proof_valid(report: OsAttemptReport, control: AppControl | None,
                 authority: CurrentAuthority) -> bool:
    proof = report.app_proof
    if proof is None or report.running_process is None:
        return False
    challenge, response = proof.challenge, proof.response
    if (challenge.process != report.running_process
            or challenge.command_session_id != report.command_session_id
            or challenge.trust_mode != authority.trust_mode
            or challenge.nonce != response.nonce):
        return False
    if control is None or (challenge.claimed_player_id != control.player_id
                           or challenge.claimed_authority_epoch != control.authority_epoch
                           or response.public_key != control.public_key):
        return False
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(response.public_key))
        key.verify(base64.b64decode(response.signature, validate=True),
                   app_proof_message(challenge))
    except (ValueError, binascii.Error, InvalidSignature):
        return False
    return True


def _control_state(control: AppControl | None) -> ControlState:
    if control is None:
        return "unavailable"
    if control.last_result != "applied":
        if control.last_result is None:
            return "unavailable"
        return ("historical_applied_latest_rejected" if control.applied_at is not None
                else "latest_rejected")
    if (control.status != "negotiated" or control.schema_version != 2
            or control.applied_at is None or control.applied_sequence < 1
            or control.last_result_sequence != control.applied_sequence
            or control.last_delivery_id != control.applied_delivery_id
            or control.last_result_digest != control.applied_digest
            or control.applied_delivery_id is None or control.applied_digest is None):
        return "incoherent"
    # Schema-one local proof has no signed delivery ID/digest. This is an app
    # acknowledgment, but it cannot yet be attributed to the sampled process.
    return "applied_unlinked"


def _matching_handoffs(evidence: AcceptanceEvidence,
                       process: ProcessIdentity | None, epoch: int | None,
                       earliest_ms: int | None, latest_ms: int | None) -> OutputState:
    if not evidence.expected_outputs or not evidence.output_inventory_matches:
        return "unavailable"
    if process is None or epoch is None or earliest_ms is None or latest_ms is None:
        return "incomplete"
    expected = {output.output_id: output for output in evidence.expected_outputs}
    if len(expected) != len(evidence.expected_outputs):
        return "incomplete"
    by_output: dict[str, list[OutputHandoff]] = {}
    for handoff in evidence.output_handoffs:
        by_output.setdefault(handoff.output_id, []).append(handoff)
    if set(by_output) != set(expected):
        return "incomplete"
    for output_id, output in expected.items():
        handoffs = by_output[output_id]
        if not any(
            (handoff.mode, handoff.binding_generation,
             handoff.configuration_revision, handoff.calibration_revision,
             handoff.synthetic_frame) ==
            (output.mode, output.binding_generation,
             output.configuration_revision, output.calibration_revision,
             output.synthetic_frame)
            and handoff.process == process and handoff.authority_epoch == epoch
            and handoff.source == "base_display_host"
            and earliest_ms <= handoff.observed_boottime_ms <= latest_ms
            for handoff in handoffs
        ):
            return "incomplete"
    return "matching_base_claims"


def assess_acceptance(evidence: AcceptanceEvidence) -> AcceptanceAssessment:
    """Classify what is known without deriving permission for any state change."""
    if (type(evidence) is not AcceptanceEvidence
            or type(evidence.minimum_stable_ms) is not int
            or evidence.minimum_stable_ms <= 0):
        raise ValueError("acceptance_evidence_invalid")
    attempt, authority = evidence.attempt, evidence.authority
    refusals: list[str] = []

    def refuse(code: str) -> None:
        if code not in refusals:
            refusals.append(code)

    if (attempt.phase not in ("stop_committed", "installing", "starting", "operational",
                              "observed_failed", "expired_unknown", "recovery_required")
            or attempt.command_id is None or attempt.drain_id is None):
        refuse("attempt_not_stopped")
    if attempt.command_session_id is None:
        refuse("attempt_session_unbound")
    if (not authority.lifecycle_active or not authority.session_active
            or authority.device_generation != attempt.device_generation
            or authority.kernel_boot_id != attempt.kernel_boot_id
            or authority.command_session_id != attempt.command_session_id
            or authority.trust_mode not in ("t1", "t2")):
        refuse("current_authority_unavailable")
    if not evidence.target_rooted:
        refuse("target_root_unavailable")

    ordered = sorted(evidence.reports, key=lambda row: row.report.report_sequence)
    if ordered and (any(not _report_matches(row, attempt, authority) for row in ordered)
                    or len({row.report.report_sequence for row in ordered}) != len(ordered)):
        refuse("os_report_context_conflict")
        valid: list[StoredReport] = []
    else:
        valid = ordered
    if not valid:
        refuse("os_report_missing")
    latest = valid[-1] if valid else None
    package: PackageState = "unknown"
    proof_matched = False
    stable = False
    earliest_ms: int | None = None
    latest_ms: int | None = None
    process: ProcessIdentity | None = None
    if latest is not None:
        current = latest.report
        process = current.running_process
        latest_ms = current.sampled_boottime_ms
        if current.executor_state == "recovery_required":
            package = "recovery_required"
            refuse("os_recovery_required")
        elif current.executor_state == "rolled_back":
            package = "rolled_back"
            refuse("target_not_active")
        elif (current.executor_state == "committed"
              and current.active_sha256 == attempt.target_sha256
              and current.running_sha256 == attempt.target_sha256
              and process is not None):
            package = "committed_target"
        else:
            package = "conflicting"
            refuse("target_not_active")
        if any(row.report.fault_code is not None for row in valid):
            refuse("os_fault_reported")
        proof_matched = _proof_valid(current, evidence.control, authority)
        if not proof_matched:
            refuse("app_process_proof_unmatched")
        if package == "committed_target" and proof_matched:
            candidates = [row.report for row in valid if
                          row.report.executor_state == "committed"
                          and row.report.active_sha256 == attempt.target_sha256
                          and row.report.running_sha256 == attempt.target_sha256
                          and row.report.running_process == process
                          and _proof_valid(row.report, evidence.control, authority)]
            if candidates:
                earliest_ms = candidates[0].sampled_boottime_ms
                stable = (
                    latest_ms - earliest_ms >= evidence.minimum_stable_ms
                    and all(valid[index].report.sampled_boottime_ms <=
                            valid[index + 1].report.sampled_boottime_ms
                            for index in range(len(valid) - 1))
                    and len(candidates) == len(valid)
                    and "os_fault_reported" not in refusals
                )
            if not stable:
                refuse("sustained_process_evidence_missing")

    control_state = _control_state(evidence.control)
    if control_state == "unavailable":
        refuse("control_unavailable")
    elif control_state in ("latest_rejected", "historical_applied_latest_rejected"):
        refuse("control_latest_rejected")
    elif control_state == "incoherent":
        refuse("control_incoherent")
    else:
        refuse("control_delivery_unlinked")
    output_state = _matching_handoffs(
        evidence, process, evidence.control.authority_epoch if evidence.control else None,
        earliest_ms, latest_ms)
    if output_state == "unavailable":
        refuse("output_inventory_unavailable")
    elif output_state == "incomplete":
        refuse("output_handoff_incomplete")
    return AcceptanceAssessment(
        package_state=package, control_state=control_state, output_state=output_state,
        proof_matched=proof_matched, process_stable=stable,
        latest_report_sequence=latest.report.report_sequence if latest else None,
        latest_report_received_at=latest.received_at if latest else None,
        historical_applied_at=evidence.control.applied_at if evidence.control else None,
        latest_control_result=evidence.control.last_result if evidence.control else None,
        refusals=tuple(refusals),
    )
