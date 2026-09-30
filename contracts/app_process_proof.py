"""Bounded local proof that a Player process holds its enrollment key.

The base OS verifies this proof against kernel peer credentials and PID1's
current Player service. It is not a Central credential or acceptance record.
"""

from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.player_control import ControlAppliedReceipt
from contracts.strict_json import loads_object

MAX_PROOF_CHALLENGE_BYTES = 2048
MAX_PROOF_RESPONSE_BYTES = 512
MAX_PROOF_BEGIN_BYTES = 256
MAX_PROOF_BEGIN_V2_BYTES = 768
MAX_PROOF_PACKET_BYTES = 2304


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class ProcessIdentity(_StrictModel):
    pid: int = Field(gt=0, le=2**31 - 1)
    start_ticks: int = Field(gt=0, le=2**63 - 1)
    invocation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    cgroup_unit: Literal["photo-wall-player.service"]


class AppProofChallenge(_StrictModel):
    schema_version: Literal[1] = Field(default=1, alias="schema")
    nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_audience: str = Field(pattern=r"^[A-Za-z0-9:/._-]{1,256}$")
    device_id: str = Field(pattern=r"^device-[0-9a-f]{64}$")
    device_generation: int = Field(ge=1, le=2**63 - 1)
    kernel_boot_id: UUID
    offer_id: UUID
    command_session_id: UUID
    attempt_id: UUID
    command_id: UUID
    trust_mode: Literal["t1", "t2"]
    claimed_player_id: str = Field(pattern=r"^p-[0-9a-f]{32}$")
    claimed_authority_epoch: int = Field(ge=1)
    process: ProcessIdentity

    @model_validator(mode="after")
    def commissioned_audience(self) -> AppProofChallenge:
        if self.installation_audience == "photo-wall-central-t0":
            raise ValueError("app_proof_t0_audience")
        return self


class AppProofChallengeV2(AppProofChallenge):
    """The current root-observed process signs one post-ACK control marker."""

    schema_version: Literal[2] = Field(default=2, alias="schema")
    purpose: Literal["control_applied"] = "control_applied"
    active_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt: ControlAppliedReceipt

    @model_validator(mode="after")
    def receipt_matches_epoch(self) -> AppProofChallengeV2:
        if self.receipt.authority_epoch != self.claimed_authority_epoch:
            raise ValueError("app_proof_receipt_epoch_mismatch")
        return self


class AppProofResponse(_StrictModel):
    nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    public_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(pattern=r"^[A-Za-z0-9+/]{86}==$")


class AppProofBegin(_StrictModel):
    schema_version: Literal[1] = Field(default=1, alias="schema")
    kind: Literal["begin"]
    claimed_player_id: str = Field(pattern=r"^p-[0-9a-f]{32}$")
    claimed_authority_epoch: int = Field(ge=1, le=2**63 - 1)


class AppProofBeginV2(_StrictModel):
    schema_version: Literal[2] = Field(default=2, alias="schema")
    kind: Literal["begin"]
    purpose: Literal["control_applied"] = "control_applied"
    claimed_player_id: str = Field(pattern=r"^p-[0-9a-f]{32}$")
    claimed_authority_epoch: int = Field(ge=1, le=2**63 - 1)
    receipt: ControlAppliedReceipt

    @model_validator(mode="after")
    def receipt_matches_epoch(self) -> AppProofBeginV2:
        if self.receipt.authority_epoch != self.claimed_authority_epoch:
            raise ValueError("app_proof_receipt_epoch_mismatch")
        return self


class AppProofChallengePacket(_StrictModel):
    schema_version: Literal[1] = Field(default=1, alias="schema")
    kind: Literal["challenge"]
    challenge: AppProofChallenge


class AppProofChallengePacketV2(_StrictModel):
    schema_version: Literal[2] = Field(default=2, alias="schema")
    kind: Literal["challenge"]
    challenge: AppProofChallengeV2


class AppProofResponsePacket(_StrictModel):
    schema_version: Literal[1] = Field(default=1, alias="schema")
    kind: Literal["response"]
    response: AppProofResponse


class AppProofResponsePacketV2(_StrictModel):
    schema_version: Literal[2] = Field(default=2, alias="schema")
    kind: Literal["response"]
    response: AppProofResponse


class AppProofResultPacket(_StrictModel):
    schema_version: Literal[1] = Field(default=1, alias="schema")
    kind: Literal["result"]
    status: Literal["recorded"]


class AppProofResultPacketV2(_StrictModel):
    schema_version: Literal[2] = Field(default=2, alias="schema")
    kind: Literal["result"]
    status: Literal["recorded"]


class AppProofErrorPacket(_StrictModel):
    schema_version: Literal[1] = Field(default=1, alias="schema")
    kind: Literal["error"]
    code: str = Field(pattern=r"^[a-z0-9_]{1,64}$")


class AppProofErrorPacketV2(_StrictModel):
    schema_version: Literal[2] = Field(default=2, alias="schema")
    kind: Literal["error"]
    code: str = Field(pattern=r"^[a-z0-9_]{1,64}$")


class LocalAppProof(_StrictModel):
    """An OS-local observation; Central must separately authenticate its carrier."""

    challenge: AppProofChallenge
    response: AppProofResponse
    verified_boottime_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def matching_nonce(self) -> LocalAppProof:
        if self.challenge.nonce != self.response.nonce:
            raise ValueError("app_proof_nonce_mismatch")
        return self


class LocalAppProofV2(_StrictModel):
    """OS-local post-ACK evidence; Registry must still join its current row."""

    challenge: AppProofChallengeV2
    response: AppProofResponse
    verified_boottime_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def matching_nonce(self) -> LocalAppProofV2:
        if self.challenge.nonce != self.response.nonce:
            raise ValueError("app_proof_nonce_mismatch")
        return self


def app_proof_message(challenge: AppProofChallenge) -> bytes:
    """One domain-separated canonical signing input shared by app and base."""
    if type(challenge) is not AppProofChallenge:
        raise TypeError("app_proof_challenge_required")
    value = {"purpose": "photo-wall-local-app-proof-v1",
             **challenge.model_dump(mode="json", by_alias=True)}
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def app_proof_message_v2(challenge: AppProofChallengeV2) -> bytes:
    """Separate signing domain; never reinterpret a V1 signature as V2."""
    if type(challenge) is not AppProofChallengeV2:
        raise TypeError("app_proof_challenge_v2_required")
    value = {"domain": "photo-wall-local-app-proof-v2",
             "challenge": challenge.model_dump(mode="json", by_alias=True)}
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def parse_app_proof_challenge(raw: bytes) -> AppProofChallenge:
    value = loads_object(raw, max_bytes=MAX_PROOF_CHALLENGE_BYTES)
    if value is None:
        raise ValueError("app_proof_challenge_invalid_json")
    return AppProofChallenge.model_validate_json(json.dumps(value).encode("utf-8"))


def parse_app_proof_response(raw: bytes) -> AppProofResponse:
    value = loads_object(raw, max_bytes=MAX_PROOF_RESPONSE_BYTES)
    if value is None:
        raise ValueError("app_proof_response_invalid_json")
    return AppProofResponse.model_validate_json(json.dumps(value).encode("utf-8"))


def parse_app_proof_begin(raw: bytes) -> AppProofBegin:
    value = loads_object(raw, max_bytes=MAX_PROOF_BEGIN_BYTES)
    if value is None or "schema" not in value:
        raise ValueError("app_proof_begin_invalid_json")
    return AppProofBegin.model_validate_json(json.dumps(value).encode("utf-8"))


def parse_app_proof_begin_any(raw: bytes) -> AppProofBegin | AppProofBeginV2:
    value = loads_object(raw, max_bytes=MAX_PROOF_BEGIN_V2_BYTES)
    if value is None or "schema" not in value:
        raise ValueError("app_proof_begin_invalid_json")
    if value["schema"] == 1:
        return parse_app_proof_begin(raw)
    if value["schema"] == 2:
        return AppProofBeginV2.model_validate(value, strict=True)
    raise ValueError("app_proof_begin_schema")


def parse_app_proof_response_packet(raw: bytes) -> AppProofResponse:
    value = loads_object(raw, max_bytes=MAX_PROOF_PACKET_BYTES)
    if value is None or "schema" not in value:
        raise ValueError("app_proof_packet_invalid_json")
    return AppProofResponsePacket.model_validate_json(json.dumps(value).encode("utf-8")).response


def parse_app_proof_response_packet_any(raw: bytes, *, schema: int) -> AppProofResponse:
    if schema == 1:
        return parse_app_proof_response_packet(raw)
    if schema != 2:
        raise ValueError("app_proof_response_schema")
    value = loads_object(raw, max_bytes=MAX_PROOF_PACKET_BYTES)
    if value is None or "schema" not in value:
        raise ValueError("app_proof_packet_invalid_json")
    return AppProofResponsePacketV2.model_validate(value, strict=True).response
