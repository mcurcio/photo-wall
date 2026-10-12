"""The operator API's power shapes (Power and the week: slice T serves Night off's read, slice S2
the rest). Plain models, one per answer; no generic "apply". Instants are Central's clock; house
times are display strings worked out in Central (the console works out no times).
"""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import Field

from central.house_time.model import LocalDateTime
from central.power.model import HoldSource, NightOff, NightOffDraft
from contracts.models import Identifier, Instant, Model
from contracts.node_output import Power


class NightOffSave(Model):
    """PUT /v1/operator/power/night-off: the editor's draft and the revision it was loaded at."""
    expected_revision: Annotated[int, Field(ge=1)]
    night_off: NightOffDraft


class NightOffView(Model):
    """GET /v1/operator/power/night-off: Night off and its next on and off times (house time)."""
    night_off: NightOff
    next_off: LocalDateTime | None
    next_on: LocalDateTime | None


class HoldRequest(Model):
    """POST /v1/operator/power/all-off and /all-on: the console's request id (a double tap makes
    one hold). The hold covers every current Frame."""
    request_id: UUID


class HoldView(Model):
    """One live hold, as the Power lane draws it (#91)."""
    hold_id: UUID
    power: Power
    frame_ids: tuple[Identifier, ...]
    source: HoldSource
    started_at: Instant
    until_at: Instant | None                      # None: until resumed
    until: LocalDateTime | None


class ResumeRequest(Model):
    """POST /v1/operator/power/resume: end the holds on these Frames (#91, Resume schedule)."""
    frame_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=256)]


__all__ = ["HoldRequest", "HoldView", "NightOffSave", "NightOffView", "ResumeRequest"]
