"""The V2 app stage command: the app root a Pi imports names the stage it carries
(appliance/apps/import_worker.py, root_import.py). Central's app operations and the broker's effect
reports went with the Pi's old HTTP path (decision 0034, S4c).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from uuid import UUID

from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_commands import producer_from_document
from contracts.node_protocol import (
    NodeProcessIdentity,
    NodeProducerV2,
    counter,
    digest,
    identifier,
)
from contracts.strict_json import loads_object

MAX_LIFECYCLE_BYTES = 16384


def _broker(producer):
    if type(producer) is not NodeProducerV2 or producer.owner != "app_effect_broker":
        raise ValueError("app_effect_producer_required")


def _process(value):
    if type(value) is not NodeProcessIdentity:
        raise ValueError("app_process_identity_required")


@dataclass(frozen=True, slots=True)
class StageCommandV2:
    operation_id: UUID
    command_id: UUID
    command_sha256: str
    producer: NodeProducerV2
    command_session_id: UUID
    offer_id: UUID
    old_process: NodeProcessIdentity
    old_app_epoch: int
    old_environment: AppEnvironmentRefV2
    target: AppEnvironmentRefV2
    fallback: AppEnvironmentRefV2

    def __post_init__(self):
        for value in (self.operation_id, self.command_id, self.command_session_id, self.offer_id):
            identifier(value)
        digest(self.command_sha256)
        _broker(self.producer)
        _process(self.old_process)
        counter(self.old_app_epoch, 1)
        for ref in (self.old_environment, self.target, self.fallback):
            if type(ref) is not AppEnvironmentRefV2 or ref.deb_name != "photo-wall-player":
                raise ValueError("app_environment_required")
        if self.target.environment_sha256 == self.old_environment.environment_sha256:
            raise ValueError("app_target_already_current")
        if self.fallback.environment_sha256 == self.target.environment_sha256:
            raise ValueError("app_fallback_equals_target")
        def abi(ref):
            return ref.base_abi, ref.graphics_abi, ref.plugin_abi, ref.architecture
        if any(abi(ref) != abi(self.old_environment) for ref in (self.target, self.fallback)):
            raise ValueError("app_environment_abi_mismatch")


def _encode(value, kind):
    raw = json.dumps({"schema": 2, "kind": kind, **asdict(value)}, default=str,
                     sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_LIFECYCLE_BYTES:
        raise ValueError("app_lifecycle_too_large")
    return raw


def _parse(raw, kind, cls):
    value = loads_object(raw, max_bytes=MAX_LIFECYCLE_BYTES)
    if (value is None or type(value.get("schema")) is not int or value.pop("schema") != 2
            or value.pop("kind", None) != kind):
        raise ValueError("app_lifecycle_invalid")
    try:
        value["producer"] = producer_from_document(value["producer"])
        for name in ("operation_id", "command_id", "command_session_id", "offer_id"):
            if name in value and value[name] is not None:
                value[name] = UUID(value[name])
        for name in ("old_process",):
            if value.get(name) is not None:
                process = value[name]
                value[name] = NodeProcessIdentity(**{**process, "invocation_id": UUID(process["invocation_id"])})
        for name in ("old_environment", "target", "fallback"):
            if value.get(name) is not None:
                value[name] = AppEnvironmentRefV2(**value[name])
        return cls(**value)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("app_lifecycle_invalid") from exc


def encode_stage_command(value: StageCommandV2) -> bytes:
    return _encode(value, "app_stage")


def parse_stage_command(raw: bytes) -> StageCommandV2:
    value = _parse(raw, "app_stage", StageCommandV2)
    if value.command_sha256 != stage_digest(value):
        raise ValueError("app_stage_digest_mismatch")
    return value


def stage_digest(value: StageCommandV2) -> str:
    return sha256(encode_stage_command(replace(value, command_sha256="0" * 64))).hexdigest()

