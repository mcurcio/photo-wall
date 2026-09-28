"""Typed read models for the centrally owned Installation boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Self

from pydantic import Field, JsonValue, model_validator

from contracts.enrollment import OutputReport
from contracts.liveness import SILENT_AFTER_SECONDS
from contracts.models import Calibration, FrameProfile, Identifier, Instant, Model

if TYPE_CHECKING:
    from central.coordination import PlayerReports


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
    # When Central last accepted a readiness report on this Player's current authority epoch
    # (Central's clock); None until it has. Enrollment is not a report: see last_seen.
    last_report_at: Instant | None = None


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
    # Liveness facts, set by with_liveness: Central's clock at read time (no Player timestamp
    # in the payload is later) and the silence threshold the console classifies against.
    read_at: Instant | None = None
    silent_after_seconds: float | None = None

    def with_liveness(self, reports: PlayerReports) -> InstallationInventory:
        """A copy carrying each Player's last accepted report, read after this inventory.

        read_at is at least every last_seen and every report, so no age taken from it is
        negative; it does not bound preview_expires, which is in the future by design."""
        return self.model_copy(update={
            "players": tuple(
                player.model_copy(update={"last_report_at": reports.reports.get(player.id)})
                for player in self.players
            ),
            "read_at": max((reports.read_at, *(player.last_seen for player in self.players))),
            "silent_after_seconds": SILENT_AFTER_SECONDS,
        })


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
