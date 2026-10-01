"""Inert node V2 evidence contracts; stdlib only, never effect authorization."""
from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from contracts.strict_json import loads_object

MAX_NODE_MESSAGE_BYTES = 65536
MAX_FACTS = 32


def token(value: str, maximum: int = 128) -> None:
    if not isinstance(value, str) or len(value) > maximum or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", value):
        raise ValueError("invalid_node_token")


def counter(value: int, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ValueError("invalid_node_counter")


def digest(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid_node_digest")


def identifier(value: UUID) -> None:
    if type(value) is not UUID:
        raise ValueError("invalid_node_uuid")


@dataclass(frozen=True, slots=True)
class NodeProducerV2:
    installation_audience: str
    device_id: str
    device_generation: int
    kernel_boot_id: UUID
    owner: Literal["host_core", "app_manager", "app_effect_broker", "display_host", "player_runtime"]
    incarnation_id: UUID

    def __post_init__(self) -> None:
        token(self.installation_audience, 256)
        if not isinstance(self.device_id, str) or not re.fullmatch(r"device-[0-9a-f]{64}", self.device_id):
            raise ValueError("invalid_node_device")
        counter(self.device_generation, 1)
        identifier(self.kernel_boot_id)
        identifier(self.incarnation_id)
        if self.owner not in ("host_core", "app_manager", "app_effect_broker", "display_host", "player_runtime"):
            raise ValueError("invalid_node_owner")


@dataclass(frozen=True, slots=True)
class OutputKey:
    kernel_boot_id: UUID
    display_host_incarnation: UUID
    output_id: str
    connection_generation: int
    mode_generation: int

    def __post_init__(self) -> None:
        identifier(self.kernel_boot_id)
        identifier(self.display_host_incarnation)
        token(self.output_id)
        counter(self.connection_generation, 1)
        counter(self.mode_generation, 1)


@dataclass(frozen=True, slots=True)
class NodeProcessIdentity:
    """Base-neutral process identity; no app proof or readiness is implied."""
    pid: int
    start_ticks: int
    invocation_id: UUID

    def __post_init__(self) -> None:
        counter(self.pid, 1)
        if self.pid > 2**31 - 1:
            raise ValueError("invalid_node_pid")
        counter(self.start_ticks, 1)
        identifier(self.invocation_id)


@dataclass(frozen=True, slots=True)
class RebootFact:
    trigger: Literal["operator_command", "authorized_local", "unknown"]
    source: Literal["requester", "base_observer"]

    def __post_init__(self) -> None:
        if self.trigger not in ("operator_command", "authorized_local", "unknown") or self.source not in ("requester", "base_observer"):
            raise ValueError("invalid_reboot_fact")


@dataclass(frozen=True, slots=True)
class AppProcessFact:
    process: NodeProcessIdentity
    app_epoch: int
    environment_sha256: str
    state: Literal["running", "exited"]

    def __post_init__(self) -> None:
        if type(self.process) is not NodeProcessIdentity:
            raise ValueError("invalid_node_process")
        counter(self.app_epoch, 1)
        digest(self.environment_sha256)
        if self.state not in ("running", "exited"):
            raise ValueError("invalid_process_state")


@dataclass(frozen=True, slots=True)
class SurfaceFact:
    output: OutputKey
    process: NodeProcessIdentity
    app_epoch: int
    binding_generation: int
    config_revision: int
    state: Literal["presented_to_compositor", "withdrawn", "invalidated"]
    buffer_id: str | None = None
    frame_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.output) is not OutputKey or type(self.process) is not NodeProcessIdentity:
            raise ValueError("invalid_surface_identity")
        counter(self.app_epoch, 1)
        counter(self.binding_generation, 1)
        counter(self.config_revision)
        if self.state not in ("presented_to_compositor", "withdrawn", "invalidated"):
            raise ValueError("invalid_surface_state")
        if self.frame_id is not None:
            token(self.frame_id)
        if self.buffer_id is not None:
            token(self.buffer_id)
        if self.state == "presented_to_compositor" and self.buffer_id is None:
            raise ValueError("presentation_buffer_required")


NodeFact = RebootFact | AppProcessFact | SurfaceFact


def fact_key(fact: NodeFact) -> tuple:
    """Never overwrite a different process, Output generation, or binding."""
    if type(fact) is RebootFact:
        return ("reboot",)
    if type(fact) is AppProcessFact:
        return ("process", fact.process, fact.app_epoch)
    if type(fact) is SurfaceFact:
        return ("surface", fact.output, fact.process, fact.app_epoch, fact.binding_generation, fact.frame_id)
    raise ValueError("unknown_node_fact")


def _facts(producer: NodeProducerV2, facts: tuple[NodeFact, ...]) -> None:
    if type(producer) is not NodeProducerV2 or type(facts) is not tuple or not 1 <= len(facts) <= MAX_FACTS:
        raise ValueError("invalid_node_facts")
    keys = set()
    owners = {RebootFact: "host_core", AppProcessFact: "app_effect_broker", SurfaceFact: "display_host"}
    for fact in facts:
        key = fact_key(fact)
        if key in keys or producer.owner != owners[type(fact)]:
            raise ValueError("duplicate_or_unowned_node_fact")
        keys.add(key)
        if type(fact) is SurfaceFact and (fact.output.kernel_boot_id != producer.kernel_boot_id or fact.output.display_host_incarnation != producer.incarnation_id):
            raise ValueError("surface_producer_mismatch")


def validate_owned_fact(producer: NodeProducerV2, fact: NodeFact) -> None:
    """Canonical owner and boot/incarnation check for a single stored fact."""
    _facts(producer, (fact,))


@dataclass(frozen=True, slots=True)
class NodeEventV2:
    producer: NodeProducerV2
    event_id: UUID
    sequence: int
    occurred_boottime_ms: int
    facts: tuple[NodeFact, ...]
    causative_command_id: UUID | None = None

    def __post_init__(self) -> None:
        identifier(self.event_id)
        counter(self.sequence, 1)
        counter(self.occurred_boottime_ms)
        _facts(self.producer, self.facts)
        if self.causative_command_id is not None:
            identifier(self.causative_command_id)


@dataclass(frozen=True, slots=True)
class NodeSnapshotV2:
    producer: NodeProducerV2
    snapshot_id: UUID
    covered_through_sequence: int
    sampled_boottime_ms: int
    facts: tuple[NodeFact, ...]
    stream_gap: bool = False

    def __post_init__(self) -> None:
        identifier(self.snapshot_id)
        counter(self.covered_through_sequence)
        counter(self.sampled_boottime_ms)
        _facts(self.producer, self.facts)
        if type(self.stream_gap) is not bool:
            raise ValueError("invalid_stream_gap")


@dataclass(frozen=True, slots=True)
class NodeCommandResponseV2:
    producer: NodeProducerV2
    command_id: UUID
    command_sha256: str
    command_session_id: UUID
    scope: Literal["operator_reboot", "app_effect"]
    decision: Literal["received", "accepted", "rejected"]
    reason: str
    trust_mode: Literal["lan_serial"] = "lan_serial"

    def __post_init__(self) -> None:
        if type(self.producer) is not NodeProducerV2:
            raise ValueError("invalid_response_producer")
        identifier(self.command_id)
        identifier(self.command_session_id)
        digest(self.command_sha256)
        token(self.reason, 64)
        owners = {"operator_reboot": "host_core", "app_effect": "app_effect_broker"}
        if owners.get(self.scope) != self.producer.owner or self.trust_mode != "lan_serial":
            raise ValueError("invalid_response_scope")
        if self.decision not in ("received", "accepted", "rejected"):
            raise ValueError("invalid_response_decision")


NodeMessage = NodeEventV2 | NodeSnapshotV2 | NodeCommandResponseV2
_TYPES = {cls.__name__: cls for cls in (
    NodeProducerV2, OutputKey, NodeProcessIdentity, RebootFact, AppProcessFact,
    SurfaceFact, NodeEventV2, NodeSnapshotV2, NodeCommandResponseV2,
)}


def _encode(value: object) -> object:
    if dataclasses.is_dataclass(value) and type(value).__name__ in _TYPES:
        return {"type": type(value).__name__, **{field.name: _encode(getattr(value, field.name)) for field in dataclasses.fields(value)}}
    if type(value) is UUID:
        return {"uuid": str(value)}
    if type(value) is tuple:
        return [_encode(item) for item in value]
    return value


def encode_node_message(message: NodeMessage) -> bytes:
    if type(message) not in (NodeEventV2, NodeSnapshotV2, NodeCommandResponseV2):
        raise ValueError("invalid_node_message")
    raw = json.dumps({"schema": 2, "message": _encode(message)}, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > MAX_NODE_MESSAGE_BYTES:
        raise ValueError("node_message_too_large")
    return raw


def _decode(value: object) -> object:
    if type(value) is list:
        return tuple(_decode(item) for item in value)
    if type(value) is dict:
        if set(value) == {"uuid"}:
            parsed = UUID(value["uuid"])
            if str(parsed) != value["uuid"]:
                raise ValueError("noncanonical_node_uuid")
            return parsed
        cls = _TYPES.get(value.get("type"))
        if cls is SurfaceFact and "frame_id" not in value:
            value = {**value, "frame_id": None}  # Historical evidence only, never current authority.
        if cls is None or set(value) != {"type", *(field.name for field in dataclasses.fields(cls))}:
            raise ValueError("invalid_node_shape")
        return cls(**{key: _decode(item) for key, item in value.items() if key != "type"})
    return value


def parse_node_message(raw: bytes) -> NodeMessage:
    value = loads_object(raw, max_bytes=MAX_NODE_MESSAGE_BYTES)
    if value is None or set(value) != {"schema", "message"} or type(value["schema"]) is not int or value["schema"] != 2:
        raise ValueError("invalid_node_message")
    try:
        message = _decode(value["message"])
    except (TypeError, AttributeError, KeyError, RecursionError) as exc:
        raise ValueError("invalid_node_message") from exc
    if type(message) not in (NodeEventV2, NodeSnapshotV2, NodeCommandResponseV2):
        raise ValueError("invalid_node_message")
    return message
