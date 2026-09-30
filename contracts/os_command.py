"""Strict data contract for a separately authenticated loader-OS command channel.

Parsing this document grants no authority. A future OS-command transport must first
verify its Central audience and device session, then bind every field below to the
current principal and durable drain before the base executor may mutate an app.
The T0 observation and application-control channels must never parse or return it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from uuid import UUID

from contracts.player_payload import FORMAT, MAX_ARCHIVE_BYTES
from contracts.strict_json import loads_object

MAX_COMMAND_BYTES = 4096
MAX_COMMAND_HORIZON_SECONDS = 600
_DIGEST = re.compile(r"[0-9a-f]{64}")
_DEVICE = re.compile(r"device-[0-9a-f]{64}")
_ABI = re.compile(r"sha256:[0-9a-f]{64}")
_AUDIENCE = re.compile(r"[A-Za-z0-9:/._-]{1,256}")


class OsCommandError(ValueError):
    """The command is malformed; it has no permission to reach the executor."""


@dataclass(frozen=True, slots=True)
class AppArtifactRef:
    sha256: str
    size: int
    base_abi: str


@dataclass(frozen=True, slots=True)
class ActivateAppCommand:
    installation_audience: str
    device_id: str
    device_generation: int
    kernel_boot_id: UUID
    offer_id: UUID
    command_session_id: UUID
    attempt_id: UUID
    command_id: UUID
    desired_revision: int
    drain_id: UUID
    target: AppArtifactRef
    fallback: AppArtifactRef
    expires_at: float


@dataclass(frozen=True, slots=True)
class LocalCommandContext:
    """Facts supplied by the base's authenticated channel and current boot."""

    installation_audience: str
    device_id: str
    device_generation: int
    kernel_boot_id: UUID
    offer_id: UUID
    command_session_id: UUID
    base_abi: str
    now_utc: float
    session_expires_at: float


def require_current_command(command: ActivateAppCommand,
                            context: LocalCommandContext) -> None:
    """Bind an already authenticated command to this installation and boot.

    This is a local admission check, not authentication. The caller must obtain
    ``context`` from protected OS state and a verified command-session transport.
    """
    if (not isinstance(command, ActivateAppCommand)
            or not isinstance(context, LocalCommandContext)
            or type(context.now_utc) not in (int, float)
            or not math.isfinite(context.now_utc)
            or type(context.session_expires_at) not in (int, float)
            or not math.isfinite(context.session_expires_at)
            or command.installation_audience != context.installation_audience
            or command.device_id != context.device_id
            or command.device_generation != context.device_generation
            or command.kernel_boot_id != context.kernel_boot_id
            or command.offer_id != context.offer_id
            or command.command_session_id != context.command_session_id
            or command.target.base_abi != context.base_abi
            or command.fallback.base_abi != context.base_abi
            or command.expires_at <= context.now_utc
            or command.expires_at > context.session_expires_at
            or command.expires_at > context.now_utc + MAX_COMMAND_HORIZON_SECONDS):
        raise OsCommandError("os_command_context_mismatch")


def _uuid(value: object) -> UUID:
    if not isinstance(value, str) or len(value) != 36:
        raise OsCommandError("os_command_identity_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise OsCommandError("os_command_identity_invalid") from exc
    if str(parsed) != value:
        raise OsCommandError("os_command_identity_invalid")
    return parsed


def _artifact(value: object) -> AppArtifactRef:
    if not isinstance(value, dict) or set(value) != {"format", "sha256", "size", "base_abi"}:
        raise OsCommandError("os_command_artifact_invalid")
    digest, size, abi = value["sha256"], value["size"], value["base_abi"]
    if (value["format"] != FORMAT or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None or type(size) is not int
            or not 0 < size <= MAX_ARCHIVE_BYTES or not isinstance(abi, str)
            or _ABI.fullmatch(abi) is None):
        raise OsCommandError("os_command_artifact_invalid")
    return AppArtifactRef(digest, size, abi)


def parse_activate_app_command(raw: bytes) -> ActivateAppCommand:
    """Parse a bounded immutable action; transport authentication is a separate step."""
    value = loads_object(raw, max_bytes=MAX_COMMAND_BYTES)
    if value is None or set(value) != {
        "schema", "kind", "installation_audience", "device_id", "device_generation",
        "kernel_boot_id", "offer_id", "command_session_id", "attempt_id", "command_id",
        "desired_revision", "drain_id", "target", "fallback", "expires_at",
    } or type(value["schema"]) is not int or value["schema"] != 1 or (
        value["kind"] != "activate_app"
    ):
        raise OsCommandError("os_command_schema_invalid")
    audience, device_id = value["installation_audience"], value["device_id"]
    generation, revision, expires_at = (value["device_generation"],
                                        value["desired_revision"], value["expires_at"])
    if (not isinstance(audience, str) or _AUDIENCE.fullmatch(audience) is None
            or not isinstance(device_id, str) or _DEVICE.fullmatch(device_id) is None
            or type(generation) is not int or not 1 <= generation <= 2**63 - 1
            or type(revision) is not int or not 0 <= revision <= 2**63 - 1
            or type(expires_at) not in (int, float) or not math.isfinite(expires_at)):
        raise OsCommandError("os_command_identity_invalid")
    target, fallback = _artifact(value["target"]), _artifact(value["fallback"])
    if target.sha256 == fallback.sha256 or target.base_abi != fallback.base_abi:
        raise OsCommandError("os_command_artifact_conflict")
    return ActivateAppCommand(
        audience, device_id, generation, _uuid(value["kernel_boot_id"]),
        _uuid(value["offer_id"]), _uuid(value["command_session_id"]),
        _uuid(value["attempt_id"]), _uuid(value["command_id"]), revision,
        _uuid(value["drain_id"]), target, fallback, float(expires_at),
    )
