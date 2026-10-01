"""Canonical LAN-serial node session and reboot wire contracts (standard library).

Serial possession is a LAN claim, never physical-device authentication. Client
credentials are independently generated per owner and must remain in root-only
boot storage. A parsed command is inert until the receiver verifies its session.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Literal
from uuid import UUID

from contracts.node_protocol import NodeProducerV2, counter, digest, identifier, token
from contracts.strict_json import loads_object

MAX_COMMAND_BYTES = 8192
Owner = Literal["host_core", "app_effect_broker", "display_host", "app_manager", "player_runtime"]
OWNERS = ("host_core", "app_effect_broker", "display_host", "app_manager", "player_runtime")


def scope_for_owner(owner: str) -> str:
    if owner not in OWNERS:
        raise ValueError("invalid_node_owner")
    return {"host_core": "operator_reboot", "app_effect_broker": "app_effect"}.get(owner, "evidence")


def producer_document(producer: NodeProducerV2) -> dict:
    return {key: str(value) if isinstance(value, UUID) else value
            for key, value in asdict(producer).items()}


def producer_from_document(value: dict) -> NodeProducerV2:
    return NodeProducerV2(**{**value, "kernel_boot_id": UUID(value["kernel_boot_id"]),
                            "incarnation_id": UUID(value["incarnation_id"])})


def _json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True, slots=True)
class NodeSessionClaim:
    serial: str
    offer_id: UUID
    kernel_boot_id: UUID
    owner: Owner
    incarnation_id: UUID
    session_id: UUID
    credential: str
    sampled_boottime_ms: int

    def __post_init__(self) -> None:
        token(self.serial, 128)
        for value in (self.offer_id, self.kernel_boot_id, self.incarnation_id, self.session_id):
            identifier(value)
        scope_for_owner(self.owner)
        digest(self.credential)
        counter(self.sampled_boottime_ms)


def encode_session_claim(claim: NodeSessionClaim) -> bytes:
    return _json({"schema": 2, **{key: str(value) if isinstance(value, UUID) else value
                                 for key, value in asdict(claim).items()}})


def parse_session_claim(raw: bytes) -> NodeSessionClaim:
    value = loads_object(raw, max_bytes=MAX_COMMAND_BYTES)
    if (value is None or type(value.get("schema")) is not int
            or value.pop("schema") != 2):
        raise ValueError("invalid_node_session_claim")
    try:
        for key in ("offer_id", "kernel_boot_id", "incarnation_id", "session_id"):
            value[key] = UUID(value[key])
        return NodeSessionClaim(**value)
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("invalid_node_session_claim") from exc


@dataclass(frozen=True, slots=True)
class NodeSessionGrant:
    producer: NodeProducerV2
    session_id: UUID
    offer_id: UUID
    expires_boottime_ms: int
    scope: str
    trust_mode: str = "lan_serial"
    command_eligible: bool = False
    command_reason: str = "not_evaluated"

    def __post_init__(self) -> None:
        if type(self.producer) is not NodeProducerV2:
            raise ValueError("invalid_session_producer")
        identifier(self.session_id)
        identifier(self.offer_id)
        counter(self.expires_boottime_ms, 1)
        if type(self.command_eligible) is not bool:
            raise ValueError("invalid_command_eligibility")
        token(self.command_reason, 64)
        if self.scope != scope_for_owner(self.producer.owner) or self.trust_mode != "lan_serial":
            raise ValueError("invalid_session_scope")


def encode_session_grant(grant: NodeSessionGrant) -> bytes:
    return _json({"schema": 2, "producer": producer_document(grant.producer),
                  "session_id": str(grant.session_id), "offer_id": str(grant.offer_id),
                  "expires_boottime_ms": grant.expires_boottime_ms,
                  "scope": grant.scope, "trust_mode": grant.trust_mode,
                  "command_eligible": grant.command_eligible, "command_reason": grant.command_reason})


def parse_session_grant(raw: bytes) -> NodeSessionGrant:
    value = loads_object(raw, max_bytes=MAX_COMMAND_BYTES)
    if value is None or type(value.get("schema")) is not int or value.pop("schema") != 2:
        raise ValueError("invalid_node_session_grant")
    try:
        return NodeSessionGrant(**{**value, "producer": producer_from_document(value["producer"]),
                                   "session_id": UUID(value["session_id"]),
                                   "offer_id": UUID(value["offer_id"])})
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("invalid_node_session_grant") from exc


@dataclass(frozen=True)
class RebootRequest:
    command_id: UUID
    command_sha256: str
    command_session_id: UUID
    offer_id: UUID
    producer: NodeProducerV2
    expires_boottime_ms: int

    def __post_init__(self) -> None:
        identifier(self.command_id)
        identifier(self.command_session_id)
        identifier(self.offer_id)
        digest(self.command_sha256)
        counter(self.expires_boottime_ms, 1)
        if type(self.producer) is not NodeProducerV2 or self.producer.owner != "host_core":
            raise ValueError("reboot_producer_invalid")


def _reboot_payload(request: RebootRequest) -> dict:
    return {"schema": 2, "scope": "operator_reboot", "trust_mode": "lan_serial",
            "command_id": str(request.command_id),
            "command_session_id": str(request.command_session_id),
            "offer_id": str(request.offer_id), "producer": producer_document(request.producer),
            "expires_boottime_ms": request.expires_boottime_ms}


def reboot_digest(request: RebootRequest) -> str:
    return sha256(_json(_reboot_payload(request))).hexdigest()


def encode_reboot_request(request: RebootRequest) -> bytes:
    if request.command_sha256 != reboot_digest(request):
        raise ValueError("reboot_digest_mismatch")
    return _json({**_reboot_payload(request), "command_sha256": request.command_sha256})


def parse_reboot_request(raw: bytes) -> RebootRequest:
    value = loads_object(raw, max_bytes=MAX_COMMAND_BYTES)
    if (value is None or type(value.get("schema")) is not int or value.pop("schema") != 2
            or value.pop("scope", None) != "operator_reboot"
            or value.pop("trust_mode", None) != "lan_serial"):
        raise ValueError("invalid_reboot_request")
    try:
        request = RebootRequest(**{**value, "producer": producer_from_document(value["producer"]),
                                   "command_id": UUID(value["command_id"]),
                                   "command_session_id": UUID(value["command_session_id"]),
                                   "offer_id": UUID(value["offer_id"])})
        if request.command_sha256 != reboot_digest(request):
            raise ValueError("reboot_digest_mismatch")
        return request
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("invalid_reboot_request") from exc
