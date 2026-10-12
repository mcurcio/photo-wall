"""Why's answers (Power and the week; frame "Why is this playing, and the week"). Plain models, one
per answer; times on the house clock are display strings Central worked out.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from central.displays.views import InForceView
from central.house_time.model import LocalDateTime
from central.power.model import HoldSource, PowerBecause
from central.runtime import Priority
from contracts.models import Identifier, Instant, Model
from contracts.node_output import Power

ContentKind = Literal["program", "show-now", "default-scene"]
BeatenBecause = Literal["lower-priority", "admitted-earlier", "kept-out-by-protected-frames"]


class ContentRequest(Model):
    """A request for content on a Frame: a Program's window, Show now (any operator activation) or
    the Default Scene. `run_id` None: it was refused, so no Run exists. `name`: the Program's
    name; for Show now and the Default Scene, its Scene id."""
    kind: ContentKind
    activation_id: str
    run_id: str | None
    scene_id: Identifier
    program_id: Identifier | None
    name: str
    rank: int                                      # Runtime's priority rank (central.runtime PRIORITY_RANK)


class Beaten(Model):
    request: ContentRequest
    because: BeatenBecause


class ContentWhy(Model):
    """`winner` None: nothing plays on the Frame. `beat`: every other request on the Frame now,
    winner's precedence order (the strongest loser first)."""
    winner: ContentRequest | None
    beat: tuple[Beaten, ...]


class PowerWhy(Model):
    """`wanted`: Central's decision now and `because` its reason; `until`: its next set time on the
    house clock (None: none within the horizon). `pi_says`: what the Pi reports carrying out for the
    latest document change, or "not-heard" (no report answers it). `resting`: dark on purpose
    (wanted off, and the Pi says it carries out an off from the test or the timetable): the
    amended U1's Resting."""
    wanted: Power
    because: PowerBecause
    until: LocalDateTime | None
    pi_says: InForceView | Literal["not-heard"]
    resting: bool


class FrameWhy(Model):
    """GET /v1/operator/frames/{frame_id}/why. `power` None: the Frame is bound to no Output."""
    frame_id: Identifier
    content: ContentWhy
    power: PowerWhy | None


class WeekWindow(Model):
    """One Program window on the week page."""
    program_id: Identifier
    name: str
    scene_id: Identifier
    priority: Priority
    start_at: Instant
    end_at: Instant
    start: LocalDateTime
    end: LocalDateTime


class PowerBand(Model):
    """One span of a Frame's wanted power on the week page's Power lane (#91: a hold names its
    source)."""
    frame_id: Identifier
    power: Power
    because: PowerBecause
    start_at: Instant
    end_at: Instant
    start: LocalDateTime
    end: LocalDateTime
    hold_id: UUID | None
    source: HoldSource | None


class Week(Model):
    """GET /v1/operator/week?first_day=YYYY-MM-DD: seven house days from `first_day`."""
    first_day: date
    zone: str
    default_scene_id: Identifier | None
    windows: Annotated[tuple[WeekWindow, ...], Field(max_length=2048)]
    power: Annotated[tuple[PowerBand, ...], Field(max_length=8192)]


__all__ = ["Beaten", "BeatenBecause", "ContentKind", "ContentRequest", "ContentWhy", "FrameWhy", "PowerBand",
           "PowerWhy", "Week", "WeekWindow"]
