"""Data-only ready receipt and bounded stop permit for the loader-OS channel.

These values are not credentials. A future T1/T2 transport authenticates the
request and Central response; the base-owned executor must recheck local roots,
process identity, capacity and deadline before stopping a healthy Player.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.app_process_proof import ProcessIdentity
from contracts.os_command import (
    ActivateAppCommand,
    LocalCommandContext,
    require_current_command,
)
from contracts.strict_json import loads_object

MAX_READY_BYTES = 4096
MAX_STOP_PERMIT_BYTES = 4096
MAX_STOP_PERMIT_SECONDS = 30


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True,
                              allow_inf_nan=False)


class ReadyToStop(_StrictModel):
    """Authenticated OS carrier's local staging claim, never pixel evidence."""

    schema_version: Literal[1] = Field(default=1, alias="schema")
    kind: Literal["ready_to_stop"] = "ready_to_stop"
    attempt_id: UUID
    command_id: UUID
    drain_id: UUID
    command_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ready_nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_size: int = Field(gt=0, le=268435456)
    fallback_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fallback_size: int = Field(gt=0, le=268435456)
    base_abi: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    staged_target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    staged_fallback_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    process: ProcessIdentity
    capacity_checked: Literal[True]
    sampled_boottime_ms: int = Field(ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def roots_match(self) -> ReadyToStop:
        if (self.target_sha256 == self.fallback_sha256
                or self.staged_target_sha256 != self.target_sha256
                or self.staged_fallback_sha256 != self.fallback_sha256
                or self.selected_sha256 != self.fallback_sha256):
            raise ValueError("ready_to_stop_roots_mismatch")
        return self


class StopPermit(_StrictModel):
    """One Central commitment, valid only for admission before its deadline."""

    schema_version: Literal[1] = Field(default=1, alias="schema")
    kind: Literal["stop_app_for_attempt"] = "stop_app_for_attempt"
    installation_audience: str = Field(pattern=r"^[A-Za-z0-9:/._-]{1,256}$")
    device_id: str = Field(pattern=r"^device-[0-9a-f]{64}$")
    device_generation: int = Field(ge=1, le=2**63 - 1)
    kernel_boot_id: UUID
    offer_id: UUID
    command_session_id: UUID
    attempt_id: UUID
    command_id: UUID
    drain_id: UUID
    permit_id: UUID
    command_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ready_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gate_generation: int = Field(ge=1, le=2**63 - 1)
    issued_at: float
    expires_at: float

    @model_validator(mode="after")
    def bounded(self) -> StopPermit:
        if (self.installation_audience == "photo-wall-central-t0"
                or not self.issued_at < self.expires_at
                or self.expires_at > self.issued_at + MAX_STOP_PERMIT_SECONDS):
            raise ValueError("stop_permit_invalid")
        return self


def canonical_bytes(value: ReadyToStop | StopPermit) -> bytes:
    """Use one stable representation for durable identity and retry replay."""
    if type(value) not in (ReadyToStop, StopPermit):
        raise ValueError("os_stop_document_invalid")
    encoded = json.dumps(value.model_dump(mode="json", by_alias=True),
                         sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    limit = MAX_READY_BYTES if type(value) is ReadyToStop else MAX_STOP_PERMIT_BYTES
    if len(encoded) > limit:
        raise ValueError("os_stop_document_too_large")
    return encoded


def parse_ready(raw: bytes) -> ReadyToStop:
    value = loads_object(raw, max_bytes=MAX_READY_BYTES)
    if value is None:
        raise ValueError("ready_to_stop_invalid")
    return ReadyToStop.model_validate_json(raw)


def parse_stop_permit(raw: bytes) -> StopPermit:
    value = loads_object(raw, max_bytes=MAX_STOP_PERMIT_BYTES)
    if value is None:
        raise ValueError("stop_permit_invalid")
    return StopPermit.model_validate_json(raw)


def require_current_stop_permit(permit: StopPermit, command: ActivateAppCommand,
                                context: LocalCommandContext, ready: ReadyToStop,
                                *, command_bytes: bytes, now_utc: float) -> None:
    """Pure local correlation; transport and executor preflight remain separate."""
    if (type(permit) is not StopPermit or type(command) is not ActivateAppCommand
            or type(context) is not LocalCommandContext
            or type(ready) is not ReadyToStop or type(command_bytes) is not bytes
            or permit.installation_audience != context.installation_audience
            or permit.device_id != context.device_id
            or permit.device_generation != context.device_generation
            or permit.kernel_boot_id != context.kernel_boot_id
            or permit.offer_id != context.offer_id
            or permit.command_session_id != context.command_session_id
            or permit.attempt_id != command.attempt_id
            or permit.command_id != command.command_id
            or permit.drain_id != command.drain_id
            or permit.command_sha256 != hashlib.sha256(command_bytes).hexdigest()
            or permit.ready_sha256 != hashlib.sha256(canonical_bytes(ready)).hexdigest()
            or ready.command_id != command.command_id
            or ready.attempt_id != command.attempt_id
            or ready.drain_id != command.drain_id
            or ready.command_sha256 != permit.command_sha256
            or type(now_utc) not in (int, float)
            or not permit.issued_at <= now_utc < permit.expires_at
            or permit.expires_at > command.expires_at
            or permit.expires_at > context.session_expires_at):
        raise ValueError("stop_permit_context_mismatch")
    if (ready.target_sha256 != command.target.sha256
            or ready.target_size != command.target.size
            or ready.fallback_sha256 != command.fallback.sha256
            or ready.fallback_size != command.fallback.size
            or ready.base_abi != context.base_abi
            or ready.base_abi != command.target.base_abi):
        raise ValueError("stop_permit_context_mismatch")
    require_current_command(command, replace(context, now_utc=now_utc))
