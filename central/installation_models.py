"""Typed read models for the centrally owned Installation boundary."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from contracts.enrollment import OutputReport
from contracts.models import Calibration, FrameProfile, Identifier, Instant, Model


class PlayerInventory(Model):
    id: Identifier
    device_id: Identifier
    authority_epoch: int = Field(ge=1, strict=True)
    registered_at: Instant
    last_seen: Instant
    retired_at: Instant | None = None
    health: dict[str, JsonValue]
    # The operator's pending queue is retired_at is None and is_bound is False.
    # Defaulted (not always present on the wire, e.g. older fixtures/probes) rather than
    # required, so this addition does not break existing inventory consumers.
    is_bound: bool = False


class OutputInventory(Model):
    player_id: Identifier
    output_id: Identifier
    observation: OutputReport


class FrameInventory(Model):
    id: Identifier
    surface_id: Identifier
    x_mm: float
    y_mm: float
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)
    profile: FrameProfile
    generation: int = Field(ge=0)
    calibration: Calibration
    calibration_valid: bool
    preview: Calibration | None = None
    preview_expires: Instant | None = None
    configuration_revision: int = Field(ge=1)
    player_id: Identifier | None = None
    output_id: Identifier | None = None


class InstallationInventory(Model):
    players: tuple[PlayerInventory, ...]
    outputs: tuple[OutputInventory, ...]
    frames: tuple[FrameInventory, ...]


class EquipmentSessionObservation(Model):
    """Small read model used by deployment acceptance observers."""

    player_id: str = Field(pattern=r"^p-[a-f0-9]{32}$")
    device_id: str = Field(pattern=r"^device-[a-f0-9]{64}$")
    authority_epoch: int = Field(ge=1, strict=True)
    retired: bool = Field(strict=True)


class EnrollmentObservation(Model):
    """Eventually consistent enrollment state, separate from transport success."""

    state: Literal["pending", "ready"]
    session: EquipmentSessionObservation | None = None

    @model_validator(mode="after")
    def session_matches_state(self) -> Self:
        if (self.state == "ready") != (self.session is not None):
            raise ValueError("ready enrollment requires a session; pending enrollment has none")
        return self
