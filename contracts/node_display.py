"""Bounded authenticated DisplayHost exchange; decisions are separate from observations."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from uuid import UUID

from contracts.node_commands import producer_from_document
from contracts.node_protocol import (
    NodeProcessIdentity,
    NodeProducerV2,
    OutputKey,
    SurfaceFact,
    counter,
    identifier,
    token,
)
from contracts.strict_json import loads_object

MAX_DISPLAY_BYTES = 16384


@dataclass(frozen=True)
class Surface:
    output: OutputKey
    process: NodeProcessIdentity
    app_epoch: int
    binding_generation: int
    config_revision: int
    frame_id: str

    def __post_init__(self) -> None:
        token(self.frame_id, 96)
        self.fact("invalidated")

    def fact(self, state: str, buffer_id: str | None = None) -> SurfaceFact:
        return SurfaceFact(
            self.output,
            self.process,
            self.app_epoch,
            self.binding_generation,
            self.config_revision,
            state,
            buffer_id,
            frame_id=self.frame_id,
        )


def output_from(value: dict) -> OutputKey:
    return OutputKey(
        **{
            **value,
            "kernel_boot_id": UUID(value["kernel_boot_id"]),
            "display_host_incarnation": UUID(value["display_host_incarnation"]),
        }
    )


def surface_from(value: dict) -> Surface:
    return Surface(
        **{
            **value,
            "output": output_from(value["output"]),
            "process": NodeProcessIdentity(
                **{**value["process"], "invocation_id": UUID(value["process"]["invocation_id"])}
            ),
        }
    )


def document(value) -> dict:
    return json.loads(json.dumps(asdict(value), default=str, allow_nan=False))


@dataclass(frozen=True)
class DisplayReceipt:
    surface: Surface
    grant_id: UUID
    buffer_id: str
    frame_tag: str
    sampled_boottime_ms: int

    def __post_init__(self) -> None:
        if type(self.surface) is not Surface:
            raise ValueError("display_receipt_surface")
        identifier(self.grant_id)
        token(self.buffer_id)
        token(self.frame_tag)
        counter(self.sampled_boottime_ms)


@dataclass(frozen=True)
class DisplayExchange:
    producer: NodeProducerV2
    request_id: UUID
    sampled_boottime_ms: int
    output: OutputKey
    connected: bool
    admitted: Surface | None = None
    candidate: Surface | None = None
    receipt: DisplayReceipt | None = None
    completed_decision_id: UUID | None = None
    completed_boottime_ms: int | None = None

    def __post_init__(self) -> None:
        identifier(self.request_id)
        if self.completed_decision_id is not None:
            identifier(self.completed_decision_id)
            counter(self.completed_boottime_ms)
            if self.completed_boottime_ms > self.sampled_boottime_ms:
                raise ValueError("display_completion_time")
        elif self.completed_boottime_ms is not None:
            raise ValueError("display_completion_identity")
        counter(self.sampled_boottime_ms)
        if (
            type(self.producer) is not NodeProducerV2
            or self.producer.owner != "display_host"
            or type(self.output) is not OutputKey
            or type(self.connected) is not bool
            or self.output.kernel_boot_id != self.producer.kernel_boot_id
            or self.output.display_host_incarnation != self.producer.incarnation_id
        ):
            raise ValueError("display_exchange_identity")
        for surface in (self.admitted, self.candidate):
            if surface is not None and (
                type(surface) is not Surface or surface.output != self.output
            ):
                raise ValueError("display_exchange_surface")
        if self.receipt is not None and (
            type(self.receipt) is not DisplayReceipt
            or self.receipt.surface.output != self.output
            or self.receipt.sampled_boottime_ms > self.sampled_boottime_ms
        ):
            raise ValueError("display_exchange_receipt")


@dataclass(frozen=True)
class DisplayDecision:
    producer: NodeProducerV2
    request_id: UUID
    decision_id: UUID
    output: OutputKey
    operation: str
    expires_boottime_ms: int
    reason: str
    surface: Surface | None = None
    receipt: DisplayReceipt | None = None
    runtime_fence: str | None = None
    trial: str | None = None

    def __post_init__(self) -> None:
        identifier(self.request_id)
        identifier(self.decision_id)
        if self.trial is not None:
            from contracts.node_calibration import parse_trial

            candidate = parse_trial(self.trial)
            if candidate.baseline.output != self.output:
                raise ValueError("display_trial_output")
        counter(self.expires_boottime_ms)
        token(self.reason)
        if self.operation not in ("retain", "candidate", "handoff", "revision", "withdraw"):
            raise ValueError("display_decision_operation")
        if (
            type(self.producer) is not NodeProducerV2
            or self.producer.owner != "display_host"
            or type(self.output) is not OutputKey
            or self.output.kernel_boot_id != self.producer.kernel_boot_id
            or self.output.display_host_incarnation != self.producer.incarnation_id
        ):
            raise ValueError("display_decision_identity")
        if self.surface is not None and (
            type(self.surface) is not Surface or self.surface.output != self.output
        ):
            raise ValueError("display_decision_surface")
        if self.operation != "retain" and (self.surface is None or self.runtime_fence is None):
            raise ValueError("display_decision_authority_required")
        if self.runtime_fence is not None:
            token(self.runtime_fence, 256)
        if self.receipt is not None and (
            type(self.receipt) is not DisplayReceipt or self.receipt.surface != self.surface
        ):
            raise ValueError("display_decision_receipt")
        if self.operation == "handoff" and self.receipt is None:
            raise ValueError("display_handoff_receipt_required")


def _encode(value, kind: str) -> bytes:
    raw = json.dumps(
        {"schema": 2, "kind": kind, **document(value)},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    if len(raw) > MAX_DISPLAY_BYTES:
        raise ValueError("display_message_bound")
    return raw


def encode_display_exchange(value: DisplayExchange) -> bytes:
    return _encode(value, "display_exchange")


def encode_display_decision(value: DisplayDecision) -> bytes:
    return _encode(value, "display_decision")


def _parse(raw: bytes, kind: str) -> dict:
    value = loads_object(raw, max_bytes=MAX_DISPLAY_BYTES)
    if (
        value is None
        or type(value.pop("schema", None)) is not int
        or value.pop("kind", None) != kind
    ):
        raise ValueError("display_message_envelope")
    # The schema is checked separately to avoid bool-as-int and destructive ambiguity.
    envelope = loads_object(raw, max_bytes=MAX_DISPLAY_BYTES)
    if envelope["schema"] != 2:
        raise ValueError("display_message_schema")
    value["producer"] = producer_from_document(value["producer"])
    value["request_id"] = UUID(value["request_id"])
    if value.get("completed_decision_id") is not None:
        value["completed_decision_id"] = UUID(value["completed_decision_id"])
    value["output"] = output_from(value["output"])
    for name in ("surface", "candidate", "admitted"):
        if value.get(name) is not None:
            value[name] = surface_from(value[name])
    if value.get("receipt") is not None:
        row = value["receipt"]
        value["receipt"] = DisplayReceipt(
            **{**row, "surface": surface_from(row["surface"]), "grant_id": UUID(row["grant_id"])}
        )
    return value


def parse_display_exchange(raw: bytes) -> DisplayExchange:
    try:
        return DisplayExchange(**_parse(raw, "display_exchange"))
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("display_exchange_invalid") from exc


def parse_display_decision(raw: bytes) -> DisplayDecision:
    try:
        value = _parse(raw, "display_decision")
        value["decision_id"] = UUID(value["decision_id"])
        return DisplayDecision(**value)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("display_decision_invalid") from exc
