"""Small, bounded wire and domain values for the observational fleet seam."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


@dataclass(frozen=True, slots=True)
class OfferAsset:
    """Frozen exact bytes a node boot offer or a node app command names."""

    kind: str
    tag: str
    content_key: str
    sha256: str
    size: int
    format: str | None = None


class FleetError(Exception):
    def __init__(self, code: str, status: int = 409, *, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code, self.status, self.retry_after = code, status, retry_after


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckIn(WireModel):
    schema_version: Literal[1] = Field(alias="schema")
    kind: Literal["pi"]
    serial: str = Field(min_length=1, max_length=128)
    kernel_boot_id: UUID
    boot_nonce: str | None = Field(default=None, pattern=r"^[0-9a-f]{16,128}$")
    offer_id: UUID | None = None
    base_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    agent_incarnation: str = Field(min_length=1, max_length=128,
                                   pattern=r"^[A-Za-z0-9:_.-]+$")
    observation_sequence: int = Field(ge=0, le=2147483647)
    phase: Literal["base_ready", "fetching_app", "verifying_app", "installing_app",
                   "starting_app", "player_unit_started", "retry_wait"]
    fault_code: str | None = Field(default=None, max_length=64,
                                   pattern=r"^[a-z0-9_]+$")
    attempted_app_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    sampled_boottime_ms: int | None = Field(default=None, ge=0)


class RunningAppEvidence(WireModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pid: int = Field(strict=True, gt=0, le=2147483647)
    start_ticks: int = Field(strict=True, gt=0, le=9223372036854775807)
    invocation_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class AppEvidence(WireModel):
    kernel_boot_id: UUID
    installed_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    running: RunningAppEvidence | None
    installed_reason: str | None = Field(min_length=1, max_length=64,
                                         pattern=r"^[a-z0-9_]+$")
    running_reason: str | None = Field(min_length=1, max_length=64,
                                       pattern=r"^[a-z0-9_]+$")

    @model_validator(mode="after")
    def coherent(self) -> AppEvidence:
        if (self.installed_sha256 is None) == (self.installed_reason is None):
            raise ValueError("installed_evidence_incoherent")
        if (self.running is None) == (self.running_reason is None):
            raise ValueError("running_evidence_incoherent")
        if self.running is not None and self.running.sha256 != self.installed_sha256:
            raise ValueError("running_digest_mismatch")
        return self


class CheckInV2(CheckIn):
    schema_version: Literal[2] = Field(alias="schema")
    observation_sequence: int = Field(strict=True, ge=0, le=2147483647)
    sampled_boottime_ms: int | None = Field(default=None, strict=True, ge=0)
    app_evidence: AppEvidence

    @model_validator(mode="after")
    def same_boot_evidence(self) -> CheckInV2:
        if self.app_evidence.kernel_boot_id != self.kernel_boot_id:
            raise ValueError("app_evidence_boot_mismatch")
        return self
