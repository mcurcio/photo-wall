"""The operator API's Display and power shapes (roadmap 1b; run ledger .claude/runs/display-1b.md,
slices C1-C3, read by the console slices K1 and K2). Plain models, one per answer; no generic
"apply". Times are Central's clock (seconds since the epoch), never a Pi's.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from central.displays.model import PowerStatus, Readiness
from central.displays.picture import PictureStatus, Setting
from contracts.models import Identifier, Instant, Model
from contracts.node_output import (
    GAMMA_TENTHS_MAX,
    GAMMA_TENTHS_MIN,
    KELVIN_MAX,
    KELVIN_MIN,
    PERCENT_MAX,
    ActsOn,
    Power,
    PowerMethod,
    PowerResult,
    RequestReason,
    WriteResult,
)


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
    remaining_seconds: int | None                 # Central's count for its live console test, else the Pi's


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
    result: PowerResult | None                    # the Pi's, for the request Central has in force (model.power_result)
    in_force: InForceView | None                  # what the Pi says it is carrying out
    linked: bool | None                           # whether Central's hub holds the Pi's link now (presence)
    last_heard_at: Instant | None                 # while not linked: when Central's hub saw the link go (presence)


class PowerTestAccepted(Model):
    """POST /v1/operator/frames/{frame_id}/power-tests (Test: turn off; no body). Turn on is
    DELETE on the same path: it ends the test, so it answers 204 with no body."""
    request_id: UUID
    for_seconds: Annotated[int, Field(ge=1)]


class DisplayPowerSettings(Model):
    """PUT /v1/operator/displays/{display_id}/power-settings, request and answer (#116-#118)."""
    power_method: PowerMethod | None
    switch_input_on_power_on: bool
    never_off_on_other_input: bool


# Roadmap 1c (run ledger .claude/runs/display-1c.md, slice C2; read by the console slice K1): the
# Picture tab's shapes. A value None is "as the display has it" (nothing set).

class PictureSettingView(Model):
    """Brightness or contrast on the Picture tab: where it acts (None: not placed yet, the display
    has not been seen), the value in force, and what the Pi reported for it."""
    acts_on: ActsOn | None
    value: Annotated[int, Field(ge=0, le=PERCENT_MAX)] | None
    answered: bool | None                         # the display answered its DDC/CI read; None: no report
    read: Annotated[int, Field(ge=0, le=PERCENT_MAX)] | None   # what the display last read back
    result: WriteResult | None                    # the Pi's write result for the latest picture change


class PictureSessionView(Model):
    """An open picture session: the sequence the next edit names, and its idle deadline."""
    session_id: UUID
    sequence: Annotated[int, Field(ge=1)]
    idle_until: Instant


class FramePictureView(Model):
    """GET /v1/operator/frames/{frame_id}/picture: the Picture tab."""
    frame_id: Identifier
    status: PictureStatus
    display_id: UUID | None                       # the Display last seen on the bound Output
    brightness: PictureSettingView
    contrast: PictureSettingView
    colour_temperature_kelvin: Annotated[int, Field(ge=KELVIN_MIN, le=KELVIN_MAX)]
    gamma_tenths: Annotated[int, Field(ge=GAMMA_TENTHS_MIN, le=GAMMA_TENTHS_MAX)]
    connected: bool | None                        # the latest Output report's; None: no report
    session: PictureSessionView | None            # the Display's open session, if any


class PictureEdit(Model):
    """POST /v1/operator/picture-sessions/{session_id}/edits: one setting, one value."""
    setting: Setting
    value: int                                    # percent, kelvin or gamma tenths, by setting
    expected_sequence: Annotated[int, Field(ge=1)]


class PicturePlace(Model):
    """POST /v1/operator/picture-sessions/{session_id}/places: move brightness or contrast."""
    setting: Literal[Setting.BRIGHTNESS, Setting.CONTRAST]
    acts_on: ActsOn
    expected_sequence: Annotated[int, Field(ge=1)]


class PictureSequence(Model):
    """An accepted edit or place: the sequence the next one names."""
    sequence: Annotated[int, Field(ge=1)]


class PictureCopyRequest(Model):
    """POST /v1/operator/frames/{frame_id}/picture-copies (#119): copy this Frame's Display values."""
    to_frames: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=64)]


class CopyOutcome(StrEnum):
    COPIED = "copied"
    NO_DISPLAY = "no-display"                     # the target has no Display recorded
    SAME_DISPLAY = "same-display"                 # the target is fed by the source's own Display
    SESSION_OPEN = "session-open"                 # someone is adjusting the target's Display now


class PictureCopyResult(Model):
    frame_id: Identifier
    outcome: CopyOutcome
