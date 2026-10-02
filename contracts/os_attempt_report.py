"""A bounded base-owned app-attempt evidence value, without transport authority.

A separate data-only route can store this value only after a T1/T2 verifier
provides the current session principal. Production does not mount that route
until the verifier exists; its stored contents remain claims.
"""

from __future__ import annotations

import json
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.app_process_proof import LocalAppProof, LocalAppProofV2, ProcessIdentity
from contracts.strict_json import loads_object

MAX_ATTEMPT_REPORT_BYTES = 4096


class OsAttemptReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    schema_version: Literal[1] = Field(default=1, alias="schema")
    device_id: str = Field(pattern=r"^device-[0-9a-f]{64}$")
    device_generation: int = Field(ge=1, le=2**63 - 1)
    installation_audience: str = Field(pattern=r"^[A-Za-z0-9:/._-]{1,256}$")
    kernel_boot_id: UUID
    offer_id: UUID
    command_session_id: UUID
    attempt_id: UUID
    command_id: UUID
    drain_id: UUID
    report_sequence: int = Field(ge=1, le=2**63 - 1)
    sampled_boottime_ms: int = Field(ge=0, le=2**63 - 1)
    executor_state: Literal["committed", "rolled_back", "recovery_required"]
    active_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    running_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    running_process: ProcessIdentity | None = None
    # V1 remains a valid historical observation. A V2 proof additionally
    # signs the applied-control receipt and base-owned active root.
    app_proof: LocalAppProofV2 | LocalAppProof | None = None
    fault_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.installation_audience == "photo-wall-central-t0":
            raise ValueError("attempt_report_t0_audience")
        if self.executor_state in ("committed", "rolled_back") and self.active_sha256 is None:
            raise ValueError("attempt_report_active_digest_required")
        if self.running_process is not None and self.active_sha256 is None:
            raise ValueError("attempt_report_running_without_active_digest")
        if (self.running_process is None) != (self.running_sha256 is None):
            raise ValueError("attempt_report_running_incoherent")
        if self.running_sha256 is not None and self.running_sha256 != self.active_sha256:
            raise ValueError("attempt_report_running_digest_mismatch")
        if self.app_proof is not None and (
            self.running_process is None
            or self.app_proof.challenge.installation_audience != self.installation_audience
            or self.app_proof.challenge.device_id != self.device_id
            or self.app_proof.challenge.device_generation != self.device_generation
            or self.app_proof.challenge.kernel_boot_id != self.kernel_boot_id
            or self.app_proof.challenge.offer_id != self.offer_id
            or self.app_proof.challenge.command_session_id != self.command_session_id
            or self.app_proof.challenge.attempt_id != self.attempt_id
            or self.app_proof.challenge.command_id != self.command_id
            or self.app_proof.challenge.process != self.running_process
            or self.app_proof.verified_boottime_ms > self.sampled_boottime_ms
        ):
            raise ValueError("attempt_report_proof_mismatch")
        if type(self.app_proof) is LocalAppProofV2 and (
            self.app_proof.challenge.active_sha256 != self.active_sha256
            or self.app_proof.challenge.active_sha256 != self.running_sha256
            or self.sampled_boottime_ms - self.app_proof.verified_boottime_ms > 15_000
        ):
            # A V2 receipt must describe the root actually sampled by this
            # report. Old local proofs cannot be replayed as fresh samples.
            raise ValueError("attempt_report_v2_proof_stale_or_root_mismatch")
        return self


def parse_os_attempt_report(raw: bytes) -> OsAttemptReport:
    """Bound a future wire body and reject duplicate keys at every depth."""
    value = loads_object(raw, max_bytes=MAX_ATTEMPT_REPORT_BYTES)
    if value is None:
        raise ValueError("attempt_report_invalid_json")
    return OsAttemptReport.model_validate_json(json.dumps(value).encode("utf-8"))
