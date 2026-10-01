"""LAN-serial operational process/Registry linkage, with a separate signing domain.

The control receipt remains the existing Registry receipt, serialized verbatim
as canonical JSON. Central owns its validation. Base checks the connected kernel
process before emitting a challenge and after receiving the signed response.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from uuid import UUID

from contracts.node_commands import producer_document, producer_from_document
from contracts.node_protocol import NodeProcessIdentity, NodeProducerV2, counter, digest, identifier
from contracts.strict_json import loads_object

MAX_NODE_LINK_BYTES = 8192


@dataclass(frozen=True, slots=True)
class NodeAppLinkChallengeV2:
    producer: NodeProducerV2
    command_session_id: UUID
    process: NodeProcessIdentity
    app_epoch: int
    environment_sha256: str
    player_id: str
    authority_epoch: int
    control_receipt: str
    nonce: str
    sampled_boottime_ms: int

    def __post_init__(self) -> None:
        if type(self.producer) is not NodeProducerV2 or self.producer.owner != "app_effect_broker":
            raise ValueError("node_link_producer_invalid")
        identifier(self.command_session_id)
        if type(self.process) is not NodeProcessIdentity:
            raise ValueError("node_link_process_invalid")
        counter(self.app_epoch, 1)
        counter(self.authority_epoch, 1)
        counter(self.sampled_boottime_ms)
        digest(self.environment_sha256)
        digest(self.nonce)
        if not isinstance(self.player_id, str) or not re.fullmatch(r"p-[0-9a-f]{32}", self.player_id):
            raise ValueError("node_link_player_invalid")
        if not isinstance(self.control_receipt, str):
            raise ValueError("node_link_receipt_invalid")
        receipt = loads_object(self.control_receipt.encode(), max_bytes=1024)
        if receipt is None or receipt.get("authority_epoch") != self.authority_epoch:
            raise ValueError("node_link_receipt_invalid")


@dataclass(frozen=True, slots=True)
class NodeAppLinkV2:
    challenge: NodeAppLinkChallengeV2
    public_key: str
    signature: str

    def __post_init__(self) -> None:
        if type(self.challenge) is not NodeAppLinkChallengeV2:
            raise ValueError("node_link_challenge_invalid")
        digest(self.public_key)
        if not isinstance(self.signature, str) or not re.fullmatch(r"[0-9a-f]{128}", self.signature):
            raise ValueError("node_link_signature_invalid")


def _json(value: dict) -> bytes:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_NODE_LINK_BYTES:
        raise ValueError("node_link_too_large")
    return raw


def _challenge_document(value: NodeAppLinkChallengeV2) -> dict:
    return {"producer": producer_document(value.producer),
            "command_session_id": str(value.command_session_id),
            "process": {"pid": value.process.pid, "start_ticks": value.process.start_ticks,
                        "invocation_id": str(value.process.invocation_id)},
            "app_epoch": value.app_epoch, "environment_sha256": value.environment_sha256,
            "player_id": value.player_id, "authority_epoch": value.authority_epoch,
            "control_receipt": value.control_receipt, "nonce": value.nonce,
            "sampled_boottime_ms": value.sampled_boottime_ms}


def _challenge(value: dict) -> NodeAppLinkChallengeV2:
    try:
        return NodeAppLinkChallengeV2(**{**value,
            "producer": producer_from_document(value["producer"]),
            "command_session_id": UUID(value["command_session_id"]),
            "process": NodeProcessIdentity(**{**value["process"],
                                               "invocation_id": UUID(value["process"]["invocation_id"])})})
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("node_link_challenge_invalid") from exc


def node_app_link_message(challenge: NodeAppLinkChallengeV2) -> bytes:
    return b"photo-wall-node-app-link-v2\x00" + _json(_challenge_document(challenge))


def encode_node_app_link_challenge(value: NodeAppLinkChallengeV2) -> bytes:
    return _json({"schema": 2, "kind": "challenge", "challenge": _challenge_document(value)})


def parse_node_app_link_challenge(raw: bytes) -> NodeAppLinkChallengeV2:
    value = loads_object(raw, max_bytes=MAX_NODE_LINK_BYTES)
    if (value is None or set(value) != {"schema", "kind", "challenge"}
            or type(value["schema"]) is not int or value["schema"] != 2 or value["kind"] != "challenge"):
        raise ValueError("node_link_challenge_invalid")
    return _challenge(value["challenge"])


def encode_node_app_link(value: NodeAppLinkV2) -> bytes:
    return _json({"schema": 2, "kind": "app_link", "challenge": _challenge_document(value.challenge),
                  "public_key": value.public_key, "signature": value.signature})


def parse_node_app_link(raw: bytes) -> NodeAppLinkV2:
    value = loads_object(raw, max_bytes=MAX_NODE_LINK_BYTES)
    if (value is None or set(value) != {"schema", "kind", "challenge", "public_key", "signature"}
            or type(value["schema"]) is not int or value["schema"] != 2 or value["kind"] != "app_link"):
        raise ValueError("node_link_invalid")
    return NodeAppLinkV2(_challenge(value["challenge"]), value["public_key"], value["signature"])


def encode_node_app_link_begin(*, player_id: str, authority_epoch: int,
                               control_receipt: str) -> bytes:
    raw = _json({"schema": 2, "kind": "begin", "player_id": player_id,
                 "authority_epoch": authority_epoch, "control_receipt": control_receipt})
    parse_node_app_link_begin(raw)
    return raw


def parse_node_app_link_begin(raw: bytes) -> dict:
    value = loads_object(raw, max_bytes=MAX_NODE_LINK_BYTES)
    if (value is None or set(value) != {"schema", "kind", "player_id", "authority_epoch", "control_receipt"}
            or type(value["schema"]) is not int or value["schema"] != 2 or value["kind"] != "begin"
            or not isinstance(value["player_id"], str)
            or not re.fullmatch(r"p-[0-9a-f]{32}", value["player_id"])
            or not isinstance(value["control_receipt"], str)):
        raise ValueError("node_link_begin_invalid")
    counter(value["authority_epoch"], 1)
    receipt = loads_object(value["control_receipt"].encode(), max_bytes=1024)
    if receipt is None or receipt.get("authority_epoch") != value["authority_epoch"]:
        raise ValueError("node_link_receipt_invalid")
    return {key: value[key] for key in ("player_id", "authority_epoch", "control_receipt")}
