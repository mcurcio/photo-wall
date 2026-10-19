"""Typed read models for the centrally owned Installation boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from central.displays.model import Readiness
from contracts.enrollment import OutputReport
from contracts.frame_lighting import FrameLighting
from contracts.liveness import REPORT_INTERVAL, SILENT_AFTER_SECONDS
from contracts.models import Calibration, ExactTurn, FrameProfile, Identifier, Instant, Model
from contracts.node_commands import Owner


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


# A Frame's or a wall's name (#92, #94): Frame names are unique ignoring case (migration 091's
# `frames_name` index, the one home of that rule).
Name = Annotated[str, Field(min_length=1, max_length=60)]


class FrameRefView(Model):
    """A Frame by id and its name today (a history entry's or a snapshot's reference to it)."""
    id: Identifier
    name: str


class FrameName(Model):
    """PUT /v1/operator/frames/{frame_id}/name."""
    name: Name


class WallNaming(Model):
    """PUT /v1/operator/walls/{surface_id}: the wall's name and its room label (#92)."""
    name: Name
    room: Annotated[str, Field(max_length=60)] = ""


class WallInventory(Model):
    """A wall (one per surface the Frames are placed on; one today, roadmap 6 adds more)."""
    surface_id: Identifier
    name: str
    room: str


Pixels = Annotated[int, Field(ge=1, le=65536, strict=True)]


class FrameInventory(Model):
    id: Identifier
    name: str
    surface_id: Identifier
    x_mm: float
    y_mm: float
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)
    profile: FrameProfile
    video: bool                               # Shows videos: may the Planner put a video here (frames.video)
    generation: int = Field(ge=0)
    calibration: Calibration
    lighting: FrameLighting                   # the Frame's lights, mat and paper (decision 0028), on its binding
    readiness: Readiness                      # worked out (central/displays/model.py), never stored
    turn: ExactTurn                           # how the Frame's screen hangs (frames.turn; render redesign S2)
    configuration_revision: int = Field(ge=1)
    player_id: Identifier | None = None
    output_id: Identifier | None = None
    screen_px: tuple[Pixels, Pixels] | None    # the bound Output's screen (central/displays/model.py `screen_px`); None: unbound or unknown
    display_mm: tuple[int, int] | None         # the Display seen there's size (its EDID's); None: none seen, or none stated



PositiveMm = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class FrameOutput(Model):
    """One Frame as the photo fit rule and the copy choice see it (follow-ups 2026-10-11, slice A1;
    frame box run, .claude/runs/frame-box/page.md fb-8; owner, 2026-10-19: "diagonal inches is a
    weird concept when we already have width and height").

    `output_px` is `contracts.models.frame_output(calibration, display_px, turn)`: the Frame's
    picture in output pixels, (width, height) along its own axes, its shape the Position's box.
    `turn` is how the Frame's screen hangs (frames.turn). `display_px` is the screen's mode as
    built (central/displays/model.py `screen_px`). Both are None for a Frame bound to no Output, or
    whose screen size is not known: such a Frame has no shape, so it is judged for no photo
    (central/wall_plan/fit.py `fit_target`). `video` is the Frame's own "Shows videos"
    (frames.video). `size_mm` is the Frame's physical size as it hangs,
    `contracts.models.frame_size_mm` of its box and the seen Display's size; None when no Display
    states one. Read by `central.infra.display_store.frame_outputs_in`."""

    frame_id: Identifier
    turn: ExactTurn
    video: bool
    display_px: tuple[Pixels, Pixels] | None
    output_px: tuple[Pixels, Pixels] | None
    size_mm: tuple[PositiveMm, PositiveMm] | None


class PlayerReports(Model):
    """When Central last accepted a readiness report from each non-retired Player, for that
    Player's current authority epoch only (player id -> received_at, Central's clock).

    Every received_at in `reports` is <= `read_at`, so an age taken from `read_at` is never
    negative, even after the wall clock steps backward."""

    read_at: Instant
    reports: Mapping[Identifier, Instant]


class OutputInterruption(Model):
    """An unresolved Output loss that fences a Frame's CURRENT Binding (console DDD §16).

    Served only when the loss matches the Player's current authority epoch and the Frame's
    current Binding (same Frame, Player, Output and binding generation): the exact key Runtime
    fences on. `cause_layer` is the owner of the node producer that reported the loss;
    `interrupted_at` is when Central recorded it (Central's clock). Central records only the
    losses it could link, so the absence of a row says nothing about the Output."""

    frame_id: Identifier
    player_id: Identifier
    output_id: Identifier
    binding_generation: int = Field(ge=0, strict=True)
    cause_layer: Owner
    interrupted_at: Instant


class InstallationInventory(Model):
    players: tuple[PlayerInventory, ...]
    outputs: tuple[OutputInventory, ...]
    frames: tuple[FrameInventory, ...]
    walls: tuple[WallInventory, ...]
    # Liveness facts, set by with_liveness: Central's clock at read time (no Player timestamp
    # in the payload is later), the silence threshold the console classifies against, and the
    # Player's report interval (the console's grace before a fresh enrollment counts as a to-do).
    read_at: Instant | None = None
    silent_after_seconds: float | None = None
    report_interval_seconds: float | None = None

    def with_liveness(self, reports: PlayerReports) -> InstallationInventory:
        """A copy carrying each Player's last accepted report, read after this inventory.

        read_at is at least every last_seen and every report, so no age taken from it is
        negative."""
        return self.model_copy(update={
            "players": tuple(
                player.model_copy(update={"last_report_at": reports.reports.get(player.id)})
                for player in self.players
            ),
            "read_at": max((reports.read_at, *(player.last_seen for player in self.players))),
            "silent_after_seconds": SILENT_AFTER_SECONDS,
            "report_interval_seconds": REPORT_INTERVAL,
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
