"""Versioned Player application control-envelope negotiation and receipts."""

import re
from typing import Literal

from pydantic import Field, model_validator

from contracts.models import Model


class ControlHello(Model):
    authority_epoch: int = Field(ge=1)
    schemas: tuple[int, ...] = Field(min_length=1, max_length=4)
    capabilities: tuple[str, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def valid_offer(self):
        if len(set(self.schemas)) != len(self.schemas) or any(
            not 1 <= schema <= 65535 for schema in self.schemas
        ):
            raise ValueError("invalid_control_schemas")
        if len(set(self.capabilities)) != len(self.capabilities) or any(
            not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", capability)
            for capability in self.capabilities
        ):
            raise ValueError("duplicate_control_capability")
        return self


class ControlSelection(Model):
    authority_epoch: int = Field(ge=1)
    schema_version: Literal[1, 2] = Field(alias="schema")
    capabilities: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_selection(self):
        if self.schema_version == 1 and self.capabilities:
            raise ValueError("legacy_control_capabilities")
        return self


class ControlDelivery(Model):
    delivery_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    delivery_sequence: int = Field(ge=1, le=2**63 - 1)


def select_control(offer: ControlHello) -> ControlSelection | None:
    """Select only a mutually known envelope, ignoring bounded future offers."""
    common = set(offer.schemas) & {1, 2}
    if not common:
        return None
    schema = max(common)
    capabilities = ("identify_output",) if (
        schema == 2 and "identify_output" in offer.capabilities
    ) else ()
    return ControlSelection(authority_epoch=offer.authority_epoch, schema=schema,
                            capabilities=capabilities)


class ControlAck(Model):
    authority_epoch: int = Field(ge=1)
    delivery_id: str = Field(min_length=32, max_length=64, pattern=r"^[a-f0-9]+$")
    result: Literal["applied", "parsed_execution_unavailable", "rejected"]


class ControlAppliedReceipt(Model):
    """Post-commit evidence that Registry accepted one exact applied ACK.

    The nonce is a correlation secret for a later process-bound local proof,
    not a credential or evidence that pixels were rendered.
    """

    schema_version: Literal[1] = Field(default=1, alias="schema")
    authority_epoch: int = Field(ge=1)
    delivery_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    delivery_sequence: int = Field(ge=1, le=2**63 - 1)
    state_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    ack_nonce: str = Field(pattern=r"^[a-f0-9]{64}$")


class ControlAckResponse(Model):
    accepted: bool
    receipt: ControlAppliedReceipt | None = None

    @model_validator(mode="after")
    def receipt_requires_acceptance(self):
        if self.receipt is not None and not self.accepted:
            raise ValueError("control_receipt_without_acceptance")
        return self
