"""Canonical node reboot wire contract and the producer document (standard library).

A parsed command is inert until the receiver verifies it. Central's node sessions are gone
(decision 0034, S4c-2a-v); the host's reboot record keeps this shape until the App Manager goes.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Literal
from uuid import UUID

from contracts.node_protocol import NodeProducerV2, digest, identifier
from contracts.strict_json import loads_object

MAX_COMMAND_BYTES = 8192
Owner = Literal["host_core", "app_effect_broker", "display_host", "app_manager", "player_runtime"]


def producer_document(producer: NodeProducerV2) -> dict:
    return {key: str(value) if isinstance(value, UUID) else value
            for key, value in asdict(producer).items()}


def producer_from_document(value: dict) -> NodeProducerV2:
    return NodeProducerV2(**{**value, "kernel_boot_id": UUID(value["kernel_boot_id"]),
                            "incarnation_id": UUID(value["incarnation_id"])})


def _json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True)
class RebootRequest:
    command_id: UUID
    command_sha256: str
    command_session_id: UUID
    offer_id: UUID
    producer: NodeProducerV2

    def __post_init__(self) -> None:
        identifier(self.command_id)
        identifier(self.command_session_id)
        identifier(self.offer_id)
        digest(self.command_sha256)
        if type(self.producer) is not NodeProducerV2 or self.producer.owner != "host_core":
            raise ValueError("reboot_producer_invalid")


def _reboot_payload(request: RebootRequest) -> dict:
    return {"schema": 2, "scope": "operator_reboot", "trust_mode": "lan_serial",
            "command_id": str(request.command_id),
            "command_session_id": str(request.command_session_id),
            "offer_id": str(request.offer_id), "producer": producer_document(request.producer)}


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
