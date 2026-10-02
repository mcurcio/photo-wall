"""Sampled AppManager preparation observations; never stop/readiness authority."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from uuid import UUID

from contracts.node_protocol import NodeProducerV2, counter, digest, identifier, token
from contracts.strict_json import loads_object

MAX_PREPARATION_BYTES = 8192


@dataclass(frozen=True, slots=True)
class ManagerPreparationV2:
    producer: NodeProducerV2
    sequence: int
    sampled_boottime_ms: int
    state: str
    operation_id: UUID | None = None
    target_sha256: str | None = None
    fallback_sha256: str | None = None
    available_bytes: int | None = None
    required_bytes: int | None = None
    fault: str | None = None

    def __post_init__(self):
        if type(self.producer) is not NodeProducerV2 or self.producer.owner != "app_manager":
            raise ValueError("manager_preparation_producer_invalid")
        counter(self.sequence, 1)
        counter(self.sampled_boottime_ms)
        if self.state not in {"idle", "preparing", "verified", "refused", "fault"}:
            raise ValueError("manager_preparation_state_invalid")
        if self.operation_id is not None:
            identifier(self.operation_id)
            digest(self.target_sha256)
        elif self.target_sha256 is not None or self.fallback_sha256 is not None or self.state in {"preparing", "verified"}:
            raise ValueError("manager_preparation_operation_required")
        if self.fallback_sha256 is not None:
            digest(self.fallback_sha256)
        for value in (self.available_bytes, self.required_bytes):
            if value is not None:
                counter(value)
        if self.fault is not None:
            token(self.fault, 128)
        if (self.state in {"fault", "refused"}) != (self.fault is not None):
            raise ValueError("manager_preparation_fault_required")


def encode_manager_preparation(value: ManagerPreparationV2) -> bytes:
    raw = json.dumps({"schema": 2, "kind": "manager_preparation", **asdict(value)}, default=str,
                     sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_PREPARATION_BYTES:
        raise ValueError("manager_preparation_too_large")
    return raw


def parse_manager_preparation(raw: bytes) -> ManagerPreparationV2:
    value = loads_object(raw, max_bytes=MAX_PREPARATION_BYTES)
    fields = {"schema", "kind", *ManagerPreparationV2.__dataclass_fields__}
    if value is None or set(value) != fields or type(value["schema"]) is not int or value["schema"] != 2 or value["kind"] != "manager_preparation":
        raise ValueError("manager_preparation_invalid")
    try:
        payload = {key: value[key] for key in ManagerPreparationV2.__dataclass_fields__}
        producer = dict(payload["producer"])
        for key in ("kernel_boot_id", "incarnation_id"):
            producer[key] = UUID(producer[key])
        payload["producer"] = NodeProducerV2(**producer)
        if payload["operation_id"] is not None:
            payload["operation_id"] = UUID(payload["operation_id"])
        return ManagerPreparationV2(**payload)
    except (TypeError, AttributeError, KeyError) as exc:
        raise ValueError("manager_preparation_invalid") from exc
