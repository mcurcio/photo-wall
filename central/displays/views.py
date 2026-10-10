"""The operator API's Display and power shapes (roadmap 1b; run ledger .claude/runs/display-1b.md,
slices C1-C3, read by the console slices K1 and K2). Plain models, one per answer; no generic
"apply". Times are Central's clock (seconds since the epoch), never a Pi's.
"""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import Field

from central.displays.model import PowerStatus, Readiness
from contracts.models import Identifier, Instant, Model
from contracts.node_output import Power, PowerMethod, PowerResult, RequestReason


class ModeView(Model):
    width: int = Field(ge=1, le=16384)
    height: int = Field(ge=1, le=16384)
    refresh_millihertz: int = Field(ge=1000, le=1_000_000)
    preferred: bool


class DisplayView(Model):
    """One Display: its identity, what its EDID offers and its power settings (#116-#118)."""
    id: UUID
    maker: str                                    # "XYM"
    product: int                                  # 5475
    name: str                                     # "MNN"; "" when the EDID names none
    serial: str | None                            # the usable serial; None when it is tied to a Frame
    tied_to_frame: bool                           # no usable serial: recognised by make and model on this Frame
    modes: tuple[ModeView, ...]
    power_method: PowerMethod | None              # None = best detected
    switch_input_on_power_on: bool
    never_off_on_other_input: bool


class FrameDisplayView(Model):
    """GET /v1/operator/frames/{frame_id}/display: the Hardware tab's Display and display-changed card."""
    frame_id: Identifier
    readiness: Readiness
    player_id: Identifier | None
    output_id: Identifier | None
    display: DisplayView | None                   # the Display last seen on the bound Output; None: none yet
    position_display: DisplayView | None          # the Display the latest Position commit names; differs when changed
    connected: bool | None                        # the latest Output report's; None: no report from this Output
    reported_at: Instant | None                   # when Central recorded that report


class InForceView(Model):
    request_id: str
    reason: RequestReason
    power: Power
    remaining_seconds: int | None                 # the Pi's own count


class PowerTestView(Model):
    request_id: UUID
    power: Power
    for_seconds: int
    ends_at: Instant


class FramePowerView(Model):
    """GET /v1/operator/frames/{frame_id}/power: the Power tab."""
    frame_id: Identifier
    status: PowerStatus
    player_id: Identifier | None
    output_id: Identifier | None
    display_id: UUID | None                       # the Display last seen on the bound Output
    power_method: PowerMethod | None              # its setting; None = best detected
    switch_input_on_power_on: bool
    never_off_on_other_input: bool
    answers: tuple[PowerMethod, ...]              # methods that answered the Pi's read-only check
    method_in_use: PowerMethod | None
    test: PowerTestView | None                    # the live console test, if any
    result: PowerResult | None                    # set when status is answered
    in_force: InForceView | None                  # what the Pi says it is carrying out
    linked: bool | None                           # whether Central's hub holds the Pi's link now (presence)
    last_heard_at: Instant | None                 # when Central last recorded anything from this Output's display


class PowerTestRequest(Model):
    """POST /v1/operator/frames/{frame_id}/power-tests: Test: turn off / turn on."""
    power: Power


class PowerTestAccepted(Model):
    request_id: UUID
    power: Power
    for_seconds: Annotated[int, Field(ge=1)]


class DisplayPowerSettings(Model):
    """PUT /v1/operator/displays/{display_id}/power-settings, request and answer (#116-#118)."""
    power_method: PowerMethod | None
    switch_input_on_power_on: bool
    never_off_on_other_input: bool
