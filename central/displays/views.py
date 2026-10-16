"""The operator API's Display and power shapes (roadmap 1b; run ledger .claude/runs/display-1b.md,
slices C1-C3, read by the console slices K1 and K2). Plain models, one per answer; no generic
"apply". Times are Central's clock (seconds since the epoch), never a Pi's.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, Field

from central.displays.model import PowerStatus, PowerTest, Readiness, remaining_seconds
from central.displays.picture import PictureStatus, SoftwareSetting, TestPatternState
from central.displays.picture_history import PictureEntry, PictureEntryKind
from central.house_time.calculator import local_date_time
from central.house_time.model import HouseTime, LocalDateTime
from central.installation_models import FrameRefView
from contracts.models import Digest, Identifier, Instant, Model
from contracts.node_output import (
    InForceKind,
    OutputReport,
    Power,
    PowerMethod,
    PowerResult,
    decode_set_time,
)
from contracts.node_picture import (
    CHOICE_NAME_MAX,
    CHOICE_VALUE_MAX,
    GAMMA_TENTHS_MAX,
    GAMMA_TENTHS_MIN,
    KELVIN_MAX,
    KELVIN_MIN,
    LEVEL_MAX,
    MATCHED_LUX_MAX,
    MATCHED_LUX_MIN,
    MAX_CHOICES,
    PANEL_BLACK_MAX,
    PANEL_BLACK_MIN,
    SOFTWARE_PERCENT_MAX,
    SOFTWARE_PERCENT_MIN,
    VCP_CODE_PATTERN,
    DisplayLight,
    RangeKind,
    SignalRange,
    SignalResult,
    SoftwarePicture,
    WriteResult,
)
from contracts.tuning import TestPattern


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
    """What the Pi says it is carrying out (contracts.node_output.InForce), for the Power tab."""
    kind: InForceKind
    power: Power | None                           # None exactly when the timetable ran out
    request_id: str | None                        # the test's, else None
    remaining_seconds: int | None                 # Central's count for its live test, else the Pi's
    since: LocalDateTime | None                   # the set time in force, on the house clock


def in_force_view(report: OutputReport | None, house: HouseTime, live: PowerTest | None,
                  now: float) -> InForceView | None:
    """What the Pi says it carries out (None when it names nothing), for the Power tab and Why. A
    test's count is Central's for its live console test (the Pi reports only on a change, so its
    own count would stand still), else the Pi's; the set time in force is shown on the house clock."""
    if report is None or report.in_force is None:
        return None
    in_force = report.in_force
    remaining = in_force.remaining_seconds
    if live is not None and in_force.request_id == str(live.request_id):
        remaining = remaining_seconds(live, now)
    since = None if in_force.since is None else local_date_time(house, decode_set_time(in_force.since))
    return InForceView(kind=in_force.kind, power=in_force.power, request_id=in_force.request_id,
                       remaining_seconds=remaining, since=since)


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


# The Picture tab's shapes (roadmap 1c's Live model; decision 0022, the picture split, slice S2):
# two sections, Display (hardware) and Photo Wall (software). A hardware value None is "as the
# display has it" (nothing set).

class PictureSessionView(Model):
    """An open picture session: the sequence the next edit names, and its idle deadline."""
    session_id: UUID
    sequence: Annotated[int, Field(ge=1)]
    idle_until: Instant


class PictureSequence(Model):
    """An accepted edit: the sequence the next one names."""
    sequence: Annotated[int, Field(ge=1)]


class PictureCopyRequest(Model):
    """POST /v1/operator/frames/{frame_id}/picture-copies (#119): copy this Frame's Display values."""
    to_frames: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=64)]


class CopyOutcome(StrEnum):
    COPIED = "copied"
    NO_DISPLAY = "no-display"                     # the target has no Display recorded
    SAME_DISPLAY = "same-display"                 # the target is fed by the source's own Display
    SESSION_OPEN = "session-open"                 # someone is adjusting the target's Display now


VcpCodeField = Annotated[str, Field(pattern=f"^{VCP_CODE_PATTERN}$")]
SoftwarePercent = Annotated[int, Field(ge=SOFTWARE_PERCENT_MIN, le=SOFTWARE_PERCENT_MAX)]


class ChoiceView(Model):
    value: Annotated[int, Field(ge=0, le=CHOICE_VALUE_MAX)]
    name: Annotated[str, Field(min_length=1, max_length=CHOICE_NAME_MAX)]


class HardwareRangeView(Model):
    """How the display counts the setting (contracts.node_picture.HardwareRange)."""
    kind: RangeKind
    maximum: Annotated[int, Field(ge=1, le=LEVEL_MAX)] | None            # LEVEL only
    choices: Annotated[tuple[ChoiceView, ...], Field(max_length=MAX_CHOICES)]   # CHOICE only


class OfferedView(Model):
    """What the display offers for one code at its last check."""
    range: HardwareRangeView
    current: Annotated[int, Field(ge=0, le=LEVEL_MAX)] | None            # the display's latest read (ask, before a write, read-back, re-read)


class HardwareResultView(Model):
    """The Pi's write for the latest picture change, judged by its read-back."""
    result: WriteResult
    before: Annotated[int, Field(ge=0, le=LEVEL_MAX)] | None
    after: Annotated[int, Field(ge=0, le=LEVEL_MAX)] | None


class HardwareSettingView(Model):
    """One row of the Display (hardware) section: every offered setting and every set code."""
    code: VcpCodeField
    name: str                                       # contracts.node_picture.PICTURE_LIST's
    offered: OfferedView | None                     # None: set here and not offered at the last check, or
                                                    # never checked (offered_known False on the view)
    value: Annotated[int, Field(ge=0, le=LEVEL_MAX)] | None   # in force; None: as the display has it
    result: HardwareResultView | None
    changed_on_buttons: bool                        # hardware.changed_on_buttons: set here, read otherwise


class SoftwarePictureView(Model):
    """The Photo Wall (software) section's values in force (contracts.node_picture.SoftwarePicture's
    ranges). Room light (decision 0028) removed the print look's sliders and LUT: the Frame's paper
    replaces them."""
    contrast_percent: SoftwarePercent
    colour_temperature_kelvin: Annotated[int, Field(ge=KELVIN_MIN, le=KELVIN_MAX)]
    gamma_tenths: Annotated[int, Field(ge=GAMMA_TENTHS_MIN, le=GAMMA_TENTHS_MAX)]


MatchedLux = Annotated[float, Field(ge=MATCHED_LUX_MIN, le=MATCHED_LUX_MAX, allow_inf_nan=False)]
PanelBlack = Annotated[float, Field(ge=PANEL_BLACK_MIN, le=PANEL_BLACK_MAX, allow_inf_nan=False)]


class LightView(Model):
    """The Display's light (decision 0028, contracts.node_picture.DisplayLight): the matched lux (the
    light at the Frame when the TV's full white matched a sheet of paper) and the panel's black (a
    share of its white)."""
    matched_lux: MatchedLux
    black: PanelBlack


class HardwareEdit(Model):
    """POST /v1/operator/picture-sessions/{session_id}/hardware-edits: one display setting, or
    None to hand it back (as the display has it)."""
    code: VcpCodeField
    value: Annotated[int, Field(ge=0, le=LEVEL_MAX)] | None
    expected_sequence: Annotated[int, Field(ge=1)]


class SoftwareEdit(Model):
    """POST /v1/operator/picture-sessions/{session_id}/software-edits: one of Photo Wall's numeric settings."""
    setting: SoftwareSetting
    value: int                                      # percent, kelvin or tenths, by setting
    expected_sequence: Annotated[int, Field(ge=1)]


class LightEdit(Model):
    """POST /v1/operator/picture-sessions/{session_id}/light-edits: the Display's light (decision
    0028), live in the session, kept by Done, put back by Revert, copied by Copy."""
    matched_lux: MatchedLux
    black: PanelBlack
    expected_sequence: Annotated[int, Field(ge=1)]


class SignalRangeEdit(Model):
    """POST /v1/operator/picture-sessions/{session_id}/signal-range-edits: the Display's signal range
    (decision 0024), live in the session, kept by Done, put back by Revert."""
    signal_range: SignalRange
    expected_sequence: Annotated[int, Field(ge=1)]


class TestPatternEdit(Model):
    """POST /v1/operator/picture-sessions/{session_id}/test-pattern-edits: the session's test
    pattern, or None to take it off; never kept."""
    __test__ = False        # a domain name, not a pytest class

    test_pattern: TestPattern | None
    expected_sequence: Annotated[int, Field(ge=1)]


class SignalView(Model):
    """The Pi's answer to the signal range, from the Output's latest picture report."""
    in_force: SignalRange           # the connector's range the Pi read back
    result: SignalResult
    answered: bool                  # it answers the recorded document (picture.signal_answered)


class TestPatternView(Model):
    """The live session's test pattern and what the tab says of it."""
    __test__ = False        # a domain name, not a pytest class

    pattern: TestPattern
    state: TestPatternState


# The LUTs list (print look, design "The LUT"): the paper profiles the house keeps, each the one
# source of the LUT baked from it.

class LutView(Model):
    """One LUT on the list: its paper profile's digest, the profile's own name, when it was added
    (Central's clock), and the Frames whose paper names it (saved, or drafted in an open Position
    trial), by name."""
    profile_sha256: Digest
    name: str
    added_at: Instant
    used_by: tuple[str, ...]


class LutsView(Model):
    """GET /v1/operator/luts."""
    luts: tuple[LutView, ...]


class FramePictureView(Model):
    """GET /v1/operator/frames/{frame_id}/picture: the Picture tab."""
    frame_id: Identifier
    status: PictureStatus
    display_id: UUID | None                         # the Display last seen on the bound Output
    connected: bool | None                          # the latest Output report's; None: no report
    offered_known: bool                             # False: never checked ("Checking what this display offers"
                                                    # while connected); a list kept while the display is away stays known
    hardware: tuple[HardwareSettingView, ...]       # hardware.hardware_rows, names from PICTURE_LIST
    software: SoftwarePictureView
    light: LightView                                # in force: the draft's while a session is live, else stored
    signal_range: SignalRange                       # in force: the draft's while a session is live, else stored
    signal: SignalView | None                       # the latest report's; None: none since the Pi's controller started
    test_pattern: TestPatternView | None            # the live session's; None: off
    test_pattern_seconds: int                       # the Pi's bound (contracts.node_picture.TEST_PATTERN_SECONDS)
    session: PictureSessionView | None              # the Display's open session, if any


class PictureCopyResult(Model):
    """One target's outcome; `hardware_skipped`: the source's display settings left as they are on
    the target (in picture-list order)."""
    frame_id: Identifier
    outcome: CopyOutcome
    hardware_skipped: tuple[VcpCodeField, ...]


# Configuration snapshots (migration 096): a Display's picture history and the Picture tab's Restore.
# Times are aware UTC (ISO 8601 in JSON), PostgreSQL's clock at the append: the agent reading a photo
# matches it against these, never a phone's or a Pi's clock.

class DisplayRefView(Model):
    """A Display by id and its name today."""
    id: UUID
    name: str


class PictureValuesView(Model):
    """The values an entry records, as the Picture tab shows values: the hardware codes set in Photo
    Wall (MCCS code -> value; a code absent is "as the display has it"), the software group, the
    signal range and the Display's light (None: the entry was written before the Display had one;
    its Restore keeps the draft's)."""
    hardware: dict[VcpCodeField, int]
    software: SoftwarePictureView
    signal_range: SignalRange
    light: LightView | None


class PictureEntryView(Model):
    """One picture history entry (central/displays/picture_history.py). `number`: a kept entry's
    commit order per Display, a sent entry's port picture change; `frame`: a sent entry's Frame
    then (None: kept, unbound, or deleted since); `test_pattern`: a sent entry's (None: off, and
    always None when kept)."""
    entry_id: UUID
    display: DisplayRefView
    kind: PictureEntryKind
    number: Annotated[int, Field(ge=1)]
    at: AwareDatetime
    frame: FrameRefView | None
    session_id: UUID | None
    restored_from: UUID | None
    picture: PictureValuesView
    test_pattern: TestPattern | None


class PictureHistoryView(Model):
    """GET /v1/operator/displays/{display_id}/picture-history: the Display's kept and sent entries,
    newest first by `at` then number. `unrecorded_change`: the Display's stored values differ from
    its newest kept entry (design rule 3, flagged until the next kept write records it)."""
    display: DisplayRefView
    unrecorded_change: bool
    entries: tuple[PictureEntryView, ...]


class PictureRestore(Model):
    """POST /v1/operator/picture-sessions/{session_id}/restores: load a picture history entry into
    the session's draft (picture.restored)."""
    entry_id: UUID
    expected_sequence: Annotated[int, Field(ge=1)]


class PictureRestoreResult(Model):
    """An accepted Restore: the sequence the next edit names; the entry's hardware codes this display
    does not offer now (not in the draft); whether it had no Display's light (decision 0028), so the
    draft kept its own."""
    sequence: Annotated[int, Field(ge=1)]
    hardware_skipped: tuple[VcpCodeField, ...]
    light_kept: bool                                # the entry had no Display's light: the draft kept its own


def software_view(software: SoftwarePicture) -> SoftwarePictureView:
    """The Photo Wall section's values. The one mapping of a SoftwarePicture to its view: the Picture
    tab's read and every picture history entry."""
    return SoftwarePictureView(contrast_percent=software.contrast_percent,
                               colour_temperature_kelvin=software.colour_temperature_kelvin,
                               gamma_tenths=software.gamma_tenths)


def light_view(light: DisplayLight) -> LightView:
    """The Display's light as the Picture tab and every picture history entry show it."""
    return LightView(matched_lux=light.matched_lux, black=light.black)


def picture_entry_view(entry: PictureEntry, *, display_name: str, frame_name: str | None) -> PictureEntryView:
    """`entry` as the routes answer it, with the names read today: the Display's and the sent entry's
    Frame's (None: no Frame then, or it was deleted)."""
    record = entry.record
    return PictureEntryView(
        entry_id=entry.entry_id, display=DisplayRefView(id=entry.display_id, name=display_name), kind=record.kind,
        number=entry.number, at=entry.at,
        frame=None if entry.frame_id is None or frame_name is None else FrameRefView(id=entry.frame_id,
                                                                                    name=frame_name),
        session_id=record.session_id, restored_from=record.restored_from,
        picture=PictureValuesView(hardware=dict(record.picture.hardware),
                                  software=software_view(record.picture.software),
                                  signal_range=record.picture.signal_range,
                                  light=light_view(record.picture.light) if record.light_recorded else None),
        test_pattern=record.test_pattern)
