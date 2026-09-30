"""Bounded loader-OS recovery claim, separate from same-boot attempt evidence.

The carrier can report repair observations after an OS reboot. This wire value
is never an artifact acceptance, stop permission, or proof of visible output.
"""

from __future__ import annotations

import json
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.strict_json import loads_object

MAX_RECOVERY_REPORT_BYTES = 4096
MAX_RECOVERY_CLAIM_BYTES = 1024


class RecoveryLeaseClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = Field(default=1, alias="schema")
    lease_id: UUID
    expected_lease_id: UUID | None


class OsRecoveryReport(BaseModel):
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
    lease_id: UUID
    report_sequence: int = Field(ge=1, le=2**63 - 1)
    sampled_boottime_ms: int = Field(ge=0, le=2**63 - 1)
    executor_state: Literal["committed", "rolled_back", "recovery_required"]
    active_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    fault_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.installation_audience == "photo-wall-central-t0":
            raise ValueError("recovery_report_t0_audience")
        if self.executor_state in ("committed", "rolled_back") and self.active_sha256 is None:
            raise ValueError("recovery_report_active_digest_required")
        return self


def parse_os_recovery_report(raw: bytes) -> OsRecoveryReport:
    """Reject oversized objects and duplicate JSON keys at every depth."""
    value = loads_object(raw, max_bytes=MAX_RECOVERY_REPORT_BYTES)
    if value is None:
        raise ValueError("recovery_report_invalid_json")
    return OsRecoveryReport.model_validate_json(json.dumps(value).encode("utf-8"))


def parse_recovery_lease_claim(raw: bytes) -> RecoveryLeaseClaim:
    """Bound and strictly parse one idempotent CAS request."""
    value = loads_object(raw, max_bytes=MAX_RECOVERY_CLAIM_BYTES)
    if value is None:
        raise ValueError("recovery_lease_claim_invalid_json")
    return RecoveryLeaseClaim.model_validate_json(json.dumps(value).encode("utf-8"))
