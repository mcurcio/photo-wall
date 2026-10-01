"""V2 app staging, stop authorization and independently observed local effects.

A stage command permits preparation only. A stop permit is a separate bounded
capability; a response, intent record, or process spawn is not proof of success.
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
    token,
)
from contracts.strict_json import loads_object

MAX_LIFECYCLE_BYTES = 16384
MAX_STOP_PERMIT_MS = 30000


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
    fallback: AppEnvironmentRefV2 | None
    expires_boottime_ms: int

    def __post_init__(self):
        for value in (self.operation_id, self.command_id, self.command_session_id, self.offer_id):
            identifier(value)
        digest(self.command_sha256)
        _broker(self.producer)
        _process(self.old_process)
        counter(self.old_app_epoch, 1)
        counter(self.expires_boottime_ms, 1)
        if self.old_environment is None or self.target is None:
            raise ValueError("app_environment_required")
        for ref in (self.old_environment, self.target, self.fallback):
            if ref is None:
                continue
            if type(ref) is not AppEnvironmentRefV2 or ref.deb_name != "photo-wall-player":
                raise ValueError("app_environment_required")
        if self.target.environment_sha256 == self.old_environment.environment_sha256:
            raise ValueError("app_target_already_current")
        if self.fallback is not None and self.fallback.environment_sha256 == self.target.environment_sha256:
            raise ValueError("app_fallback_equals_target")
        def abi(ref):
            return ref.base_abi, ref.graphics_abi, ref.plugin_abi, ref.architecture
        if any(abi(ref) != abi(self.old_environment) for ref in (self.target, self.fallback) if ref is not None):
            raise ValueError("app_environment_abi_mismatch")


@dataclass(frozen=True, slots=True)
class StageReadyV2:
    producer: NodeProducerV2
    operation_id: UUID
    command_id: UUID
    command_sha256: str
    command_session_id: UUID
    request_id: UUID
    sequence: int
    sampled_boottime_ms: int
    old_process: NodeProcessIdentity
    old_app_epoch: int
    target_sha256: str
    fallback_sha256: str | None
    roots_verified: bool
    capacity_available: bool

    def __post_init__(self):
        _broker(self.producer)
        for value in (self.operation_id, self.command_id, self.command_session_id, self.request_id):
            identifier(value)
        for value in (self.command_sha256, self.target_sha256):
            digest(value)
        if self.fallback_sha256 is not None:
            digest(self.fallback_sha256)
        _process(self.old_process)
        counter(self.old_app_epoch, 1)
        counter(self.sequence, 1)
        counter(self.sampled_boottime_ms)
        if type(self.roots_verified) is not bool or type(self.capacity_available) is not bool:
            raise ValueError("app_ready_flags_invalid")


@dataclass(frozen=True, slots=True)
class StopPermitV2:
    producer: NodeProducerV2
    operation_id: UUID
    command_id: UUID
    command_sha256: str
    command_session_id: UUID
    permit_id: UUID
    drain_id: UUID
    ready_sha256: str
    old_process: NodeProcessIdentity
    old_app_epoch: int
    issued_boottime_ms: int
    expires_boottime_ms: int

    def __post_init__(self):
        _broker(self.producer)
        for value in (self.operation_id, self.command_id, self.command_session_id, self.permit_id, self.drain_id):
            identifier(value)
        digest(self.command_sha256)
        digest(self.ready_sha256)
        _process(self.old_process)
        counter(self.old_app_epoch, 1)
        counter(self.issued_boottime_ms)
        counter(self.expires_boottime_ms, 1)
        if not 0 < self.expires_boottime_ms - self.issued_boottime_ms <= MAX_STOP_PERMIT_MS:
            raise ValueError("app_stop_permit_duration_invalid")


@dataclass(frozen=True, slots=True)
class AppEffectEventV2:
    producer: NodeProducerV2
    operation_id: UUID
    command_id: UUID
    command_sha256: str
    command_session_id: UUID
    event_id: UUID
    sequence: int
    occurred_boottime_ms: int
    phase: str
    permit_id: UUID | None = None
    process: NodeProcessIdentity | None = None
    app_epoch: int | None = None
    environment_sha256: str | None = None
    fault: str | None = None
    executor_sealed: bool = False
    journal_watermark: int | None = None

    def __post_init__(self):
        _broker(self.producer)
        for value in (self.operation_id, self.command_id, self.command_session_id, self.event_id):
            identifier(value)
        digest(self.command_sha256)
        counter(self.sequence, 1)
        counter(self.occurred_boottime_ms)
        if self.phase not in {"intent_stop", "stopped", "starting_new", "running", "target_failed",
                "fallback_starting", "fallback_running", "effect_unknown", "no_stop_quiescent", "cancelled_before_stop"}:
            raise ValueError("app_effect_phase_invalid")
        if self.permit_id is not None:
            identifier(self.permit_id)
        if self.phase not in {"no_stop_quiescent", "cancelled_before_stop"} and self.permit_id is None:
            raise ValueError("app_effect_permit_required")
        if self.process is not None:
            _process(self.process)
        if self.app_epoch is not None:
            counter(self.app_epoch, 1)
        if self.environment_sha256 is not None:
            digest(self.environment_sha256)
        if self.phase in {"starting_new", "running", "fallback_starting", "fallback_running", "no_stop_quiescent", "cancelled_before_stop"}:
            if self.process is None or self.app_epoch is None or self.environment_sha256 is None:
                raise ValueError("app_effect_process_required")
        if self.fault is not None:
            token(self.fault, 128)
        if type(self.executor_sealed) is not bool:
            raise ValueError("app_effect_seal_invalid")
        if self.phase in {"no_stop_quiescent", "cancelled_before_stop"}:
            if not self.executor_sealed or type(self.journal_watermark) is not int or self.journal_watermark != self.sequence:
                raise ValueError("app_no_effect_contiguous_seal_required")
        elif self.executor_sealed or self.journal_watermark is not None:
            raise ValueError("app_effect_seal_phase_invalid")


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
        for name in ("operation_id", "command_id", "command_session_id", "offer_id", "request_id",
                     "permit_id", "drain_id", "event_id", "revalidation_id", "quiescent_event_id", "carrier_session_id"):
            if name in value and value[name] is not None:
                value[name] = UUID(value[name])
        for name in ("old_process", "process"):
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


def encode_stage_ready(value: StageReadyV2) -> bytes:
    return _encode(value, "app_ready")


def parse_stage_ready(raw: bytes) -> StageReadyV2:
    return _parse(raw, "app_ready", StageReadyV2)


def ready_digest(value: StageReadyV2) -> str:
    return sha256(encode_stage_ready(value)).hexdigest()


def encode_stop_permit(value: StopPermitV2) -> bytes:
    return _encode(value, "app_stop_permit")


def parse_stop_permit(raw: bytes) -> StopPermitV2:
    return _parse(raw, "app_stop_permit", StopPermitV2)


def encode_app_effect_event(value: AppEffectEventV2) -> bytes:
    return _encode(value, "app_effect")


def parse_app_effect_event(raw: bytes) -> AppEffectEventV2:
    return _parse(raw, "app_effect", AppEffectEventV2)


@dataclass(frozen=True, slots=True)
class RevalidationGrantV2:
    producer: NodeProducerV2
    operation_id: UUID
    revalidation_id: UUID
    quiescent_event_id: UUID
    carrier_session_id: UUID
    old_process: NodeProcessIdentity
    old_app_epoch: int
    old_environment_sha256: str
    control_floor: int
    expires_boottime_ms: int

    def __post_init__(self):
        _broker(self.producer)
        for value in (self.operation_id, self.revalidation_id, self.quiescent_event_id, self.carrier_session_id):
            identifier(value)
        _process(self.old_process)
        counter(self.old_app_epoch, 1)
        digest(self.old_environment_sha256)
        counter(self.control_floor)
        counter(self.expires_boottime_ms, 1)


@dataclass(frozen=True, slots=True)
class NoEffectProofV2:
    operation_id: UUID
    revalidation_id: UUID
    quiescent_event_id: UUID
    app_link: object

    def __post_init__(self):
        from contracts.node_app_link import NodeAppLinkV2
        for value in (self.operation_id, self.revalidation_id, self.quiescent_event_id):
            identifier(value)
        if type(self.app_link) is not NodeAppLinkV2:
            raise ValueError("no_effect_app_link_required")


def encode_revalidation_grant(value: RevalidationGrantV2) -> bytes:
    return _encode(value, "app_revalidation")


def parse_revalidation_grant(raw: bytes) -> RevalidationGrantV2:
    return _parse(raw, "app_revalidation", RevalidationGrantV2)


def encode_no_effect_proof(value: NoEffectProofV2) -> bytes:
    from contracts.node_app_link import encode_node_app_link
    document = {"schema": 2, "kind": "app_no_effect", "operation_id": str(value.operation_id),
                "revalidation_id": str(value.revalidation_id), "quiescent_event_id": str(value.quiescent_event_id),
                "app_link": json.loads(encode_node_app_link(value.app_link))}
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > MAX_LIFECYCLE_BYTES:
        raise ValueError("app_lifecycle_too_large")
    return raw


def parse_no_effect_proof(raw: bytes) -> NoEffectProofV2:
    from contracts.node_app_link import parse_node_app_link
    value = loads_object(raw, max_bytes=MAX_LIFECYCLE_BYTES)
    if (value is None or set(value) != {"schema", "kind", "operation_id", "revalidation_id", "quiescent_event_id", "app_link"}
            or type(value["schema"]) is not int or value["schema"] != 2 or value["kind"] != "app_no_effect"):
        raise ValueError("app_no_effect_invalid")
    try:
        return NoEffectProofV2(UUID(value["operation_id"]), UUID(value["revalidation_id"]),
            UUID(value["quiescent_event_id"]), parse_node_app_link(json.dumps(value["app_link"]).encode()))
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("app_no_effect_invalid") from exc


@dataclass(frozen=True, slots=True)
class StopPermitReceiptV2:
    permit: StopPermitV2
    recovery_only: bool = True

    def __post_init__(self):
        if type(self.permit) is not StopPermitV2 or self.recovery_only is not True:
            raise ValueError("app_permit_receipt_recovery_only")


def encode_stop_permit_receipt(value: StopPermitReceiptV2) -> bytes:
    return json.dumps({"schema": 2, "kind": "app_stop_permit_receipt", "recovery_only": True,
                       "permit": json.loads(encode_stop_permit(value.permit))},
                      sort_keys=True, separators=(",", ":")).encode()


def parse_stop_permit_receipt(raw: bytes) -> StopPermitReceiptV2:
    value = loads_object(raw, max_bytes=MAX_LIFECYCLE_BYTES)
    if (value is None or set(value) != {"schema", "kind", "recovery_only", "permit"}
            or type(value["schema"]) is not int or value["schema"] != 2
            or value["kind"] != "app_stop_permit_receipt" or value["recovery_only"] is not True):
        raise ValueError("app_permit_receipt_invalid")
    return StopPermitReceiptV2(parse_stop_permit(json.dumps(value["permit"]).encode()))
