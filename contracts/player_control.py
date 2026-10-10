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


# A plan Layer carries `after_end` (contracts.models.AfterEnd) only to a Player that offered
# this; every Player released before it parses the yes/no `retain_on_expiry` instead, with
# unknown fields forbidden (tests/test_published_player_wire.py).
LAYER_AFTER_END = "layer_after_end"
CAPABILITIES = ("identify_output", LAYER_AFTER_END)


def select_control(offer: ControlHello) -> ControlSelection | None:
    """Select only a mutually known envelope, ignoring bounded future offers."""
    common = set(offer.schemas) & {1, 2}
    if not common:
        return None
    schema = max(common)
    capabilities = tuple(
        capability for capability in CAPABILITIES if capability in offer.capabilities
    ) if schema == 2 else ()
    return ControlSelection(authority_epoch=offer.authority_epoch, schema=schema,
                            capabilities=capabilities)


def plan_for_selection(plan: dict, selection: ControlSelection) -> dict:
    """A wire plan as the selected session reads it. Without `layer_after_end`, each layer's
    after-state becomes the flag that Player knows: keep this photo, or not; it never drops a
    kept photo (its own release's behaviour)."""
    if LAYER_AFTER_END in selection.capabilities:
        return plan
    layers = []
    for layer in plan["layers"]:
        legacy = {key: value for key, value in layer.items() if key != "after_end"}
        legacy["retain_on_expiry"] = layer.get("after_end") == "keep_this_photo"
        layers.append(legacy)
    return {**plan, "layers": layers}


def plan_from_selection(plan: dict, selection: ControlSelection) -> dict:
    """The reverse, on the Player: a plan from a Central that did not select `layer_after_end`
    (one older than this Player) carries the flag; read it as the after-state it meant."""
    if LAYER_AFTER_END in selection.capabilities:
        return plan
    layers = []
    for layer in plan["layers"]:
        if not isinstance(layer, dict):
            layers.append(layer)  # unparseable either way; the plan's validation refuses it
            continue
        current = {key: value for key, value in layer.items() if key != "retain_on_expiry"}
        current["after_end"] = ("keep_this_photo" if layer.get("retain_on_expiry")
                                else "leave_as_is")
        layers.append(current)
    return {**plan, "layers": layers}


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
