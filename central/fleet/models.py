"""Small, bounded wire and domain values for the observational fleet seam."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from central.content_catalog.catalog import sanitize_serial
from contracts.release import MAX_ROOTFS_BYTES

Digest = str


class FleetError(Exception):
    def __init__(self, code: str, status: int = 409) -> None:
        super().__init__(code)
        self.code, self.status = code, status


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Artifact(WireModel):
    tag: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(gt=0, le=MAX_ROOTFS_BYTES)


class OfferRequest(WireModel):
    schema_version: Literal[1] = Field(alias="schema")
    kind: Literal["pi"]
    serial: str = Field(min_length=1, max_length=128)
    kernel_boot_id: UUID
    boot_nonce: str = Field(pattern=r"^[0-9a-f]{16,128}$")

    def validated_serial(self) -> str:
        serial = sanitize_serial(self.serial)
        if serial is None:
            raise FleetError("invalid_serial", 422)
        return serial


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


class PolicyWrite(WireModel):
    expected_revision: int = Field(ge=0)
    target: Artifact | None


class OverrideWrite(WireModel):
    expected_revision: int = Field(ge=0)
    target: Artifact


class RevisionWrite(WireModel):
    expected_revision: int = Field(ge=0)


class BaselineWrite(WireModel):
    expected_revision: int = Field(ge=0)
    tag: str = Field(min_length=1, max_length=128)
