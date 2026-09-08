"""Bounded public release-observation contract shared by fixture and VM host.

This contract observes central decisions; it cannot select or consume a trial.
Session fields describe the device's current session, including when observing
an older boot attempt. Only hashes of boot capabilities cross this boundary.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_RELEASE_RESULT_BYTES = 4096
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
DeviceId = Annotated[str, Field(pattern=r"^device-[a-f0-9]{64}$")]
BootId = Annotated[str, Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")]
PlayerId = Annotated[str, Field(pattern=r"^p-[a-f0-9]{32}$")]
FAILURE_STAGES = {
    "release_probe_input_invalid": "input",
    "release_probe_query_failed": "query",
    "release_probe_evidence_invalid": "result",
    "release_probe_stage_failed": "stage",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CentralBootEvidence(StrictModel):
    device_id: DeviceId
    boot_id: BootId
    ticket_sha256: Digest
    release_id: Digest
    trial: bool
    status: Literal["booting", "healthy", "superseded", "failed"]
    accepted_release_id: Digest
    candidate_release_id: Digest | None
    current: bool
    current_player_id: PlayerId | None
    current_authority_epoch: Annotated[int, Field(ge=1)] | None
    # The consumed row for this attempt's device and release, not the current
    # candidate pointer or a row joined only by ticket. Absence stays explicit.
    trial_ticket_sha256: Digest | None

    @model_validator(mode="after")
    def consistent_observation(self) -> Self:
        if (self.current_player_id is None) != (self.current_authority_epoch is None):
            raise ValueError("release_session_pair")
        if self.trial and self.trial_ticket_sha256 != self.ticket_sha256:
            raise ValueError("release_trial_consumption_mismatch")
        if not self.trial and self.trial_ticket_sha256 == self.ticket_sha256:
            raise ValueError("release_nontrial_consumption")
        if self.current and self.status in ("failed", "superseded"):
            raise ValueError("release_current_status")
        if self.status == "failed" and not self.trial:
            raise ValueError("release_failed_nontrial")
        if self.current and self.status == "healthy" and self.accepted_release_id != self.release_id:
            raise ValueError("release_healthy_not_accepted")
        return self


class ReleaseEvidenceResult(StrictModel):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    kind: Literal["release-evidence"]
    evidence: CentralBootEvidence | None


class ReleaseStageResult(StrictModel):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    kind: Literal["release-staged"]
    staged: bool
    release_id: Digest

    @model_validator(mode="after")
    def successful_stage(self) -> Self:
        if not self.staged:
            raise ValueError("release_not_staged")
        return self


class ReleaseFailureResult(StrictModel):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    kind: Literal["release-failure"]
    role: Literal["observer"]
    action: Literal["evidence", "stage"]
    stage: Literal["input", "query", "result", "stage"]
    code: Literal[tuple(FAILURE_STAGES)]

    @model_validator(mode="after")
    def matching_failure(self) -> Self:
        if FAILURE_STAGES[self.code] != self.stage:
            raise ValueError("release_failure_stage")
        if ((self.stage in ("query", "result") and self.action != "evidence")
                or (self.stage == "stage" and self.action != "stage")):
            raise ValueError("release_failure_action")
        return self


def release_helper_identity(args: list[str]) -> tuple[str, str] | None:
    """Recognize only the pinned helper's complete observer exec shape."""
    if (len(args) not in (11, 15) or args[:2] != ["docker", "exec"]
            or not re.fullmatch(r"pw-boot-[a-f0-9]{16}-observer", args[2])
            or args[3:6] != ["python", "-m", "scripts.vm_release_probe"]
            or args[7] != "--device-id" or not re.fullmatch(r"device-[a-f0-9]{64}", args[8])
            or args[9] != "--boot-id"
            or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", args[10])):
        return None
    if len(args) == 11 and args[6] == "evidence":
        return args[2], args[6]
    if (len(args) == 15 and args[6] == "stage" and args[11] == "--manifest"
            and args[13] == "--signature" and len(args[12]) <= 16384 and len(args[14]) <= 128):
        return args[2], args[6]
    return None


class ReleaseProbeFailure(ValueError):
    """Safe nonzero-command cause; private output is never attached."""

    def __init__(self, failure: ReleaseFailureResult, args: list[str]):
        identity = release_helper_identity(args)
        if identity is None or identity[1] != failure.action:
            raise ValueError("release_failure_source")
        super().__init__(failure.code)
        self.failure, self.command = failure, tuple(args)


def trusted_release_failure(args: list[str], raw: bytes) -> ReleaseProbeFailure | None:
    identity = release_helper_identity(args)
    if identity is None:
        return None
    try:
        failure = ReleaseFailureResult.model_validate(_bounded_json(raw))
        return ReleaseProbeFailure(failure, args)
    except ValueError:
        return None


def decode_release_result(action: str, raw: bytes) -> ReleaseEvidenceResult | ReleaseStageResult:
    """Reject malformed, oversized, or action-confused helper responses."""
    value = _bounded_json(raw)
    if action == "evidence":
        return ReleaseEvidenceResult.model_validate(value)
    if action == "stage":
        return ReleaseStageResult.model_validate(value)
    raise ValueError("release_result_action")


def _bounded_json(raw: bytes) -> object:
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RELEASE_RESULT_BYTES:
        raise ValueError("release_result_size")
    return json.loads(raw, object_pairs_hook=_unique_object)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("release_result_duplicate_key")
        result[key] = value
    return result
