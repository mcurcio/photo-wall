"""The Display's picture rules, pure (roadmap 1c; run ledger .claude/runs/display-1c.md, slices C1
and C2; design rules 1 and 3; settings #111-#114, #119).

One home each for:
- **Where a setting acts** (rule 1). Brightness and contrast each act in exactly one place,
  recorded on the Display: placed at the Display's first probe (`placed_at_first_probe`: on the
  display when it answered the DDC/CI read, else picture adjustment) and kept; only the operator
  moves it (`place`), and only onto the display when it answered. A failed write never moves it.
  An unplaced setting acts nowhere (`output_picture` sends it unset). Colour temperature and
  gamma act only as picture adjustment in 1c.
- **As the display has it** (F7): a brightness or contrast nobody set is unset; nothing is
  written to the display and nothing adjusted.
- **The picture session** (the Live model; central/live_session.py): edits go to a draft that
  is in force while the session is live (`effective_picture`); Done stores the draft; Revert, or
  the session's lease running out, puts back what was in force before (`reverted`: the stored
  values, and a setting that was unset and was touched goes to the value the display read before
  the session). The projection is a pure function of the stored picture, the session and
  Central's `now`, so an abandoned console needs no reaper.
- **Done waits for the Pi** (`picture_status`): SHOWN once the report's picture part answers the
  document's picture_change and the Player presented it.
- **Copy** (#119): the four values, never where they act, never position (Position lives on the
  Frame); a percent means different things on different displays, so Copy matches settings, not
  light output.
- **Brightness from 1b's gain** (the migration's rule, mirrored in 075_display_picture.sql):
  gain 1.0 is unset; any other gain is a picture-adjustment brightness of round(gain * 50),
  so no panel gets a value nobody set.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final
from uuid import UUID

from central.live_session import Lease, StaleSequence, next_sequence, touched
from contracts.node_output import (
    AS_THE_DISPLAY_HAS_IT,
    GAMMA_TENTHS_MAX,
    GAMMA_TENTHS_MIN,
    GAMMA_TENTHS_NEUTRAL,
    KELVIN_MAX,
    KELVIN_MIN,
    KELVIN_NEUTRAL,
    KELVIN_STEP,
    PERCENT_MAX,
    ActsOn,
    OutputDocument,
    OutputPicture,
    OutputReport,
    PictureSetting,
    PictureValue,
)

PICTURE_IDLE_SECONDS: Final = 5.0     # a picture session ends this long after its last edit or keepalive


class Setting(StrEnum):
    BRIGHTNESS = "brightness"                   # #111
    CONTRAST = "contrast"                       # #112
    COLOUR_TEMPERATURE = "colour-temperature"   # #113
    GAMMA = "gamma"                             # #114


PLACEABLE: Final = (Setting.BRIGHTNESS, Setting.CONTRAST)   # the two that may act on the display


class PictureStatus(StrEnum):
    """What the Picture tab's Done waits on."""
    NO_DISPLAY = "no-display"   # no Display recorded on the bound Output, or not bound
    WAITING = "waiting"         # the Pi has not answered the latest picture change yet
    SHOWN = "shown"             # taken by the display or presented by the Player: Done may keep it


class PictureRefused(Exception):
    """A refusal the routes answer as {"error": code}: stale_sequence 409, out_of_range 422,
    session_ended 410, not_placed_yet 409, display_did_not_answer 409, not_presented_yet 409."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class StoredSetting:
    """Brightness or contrast as the Display record stores it; acts_on None = not placed yet."""
    acts_on: ActsOn | None
    value: PictureValue


@dataclass(frozen=True)
class DisplayPicture:
    """The Display record's four picture settings (displays row)."""
    brightness: StoredSetting
    contrast: StoredSetting
    colour_temperature_kelvin: int
    gamma_tenths: int


@dataclass(frozen=True)
class PictureSession:
    """One open picture session on one Display (picture_sessions row)."""
    session_id: UUID
    display_id: UUID
    sequence: int
    draft: DisplayPicture
    before_read: tuple[int | None, int | None]    # brightness, contrast the display read at begin
    touched: frozenset[Setting]
    lease: Lease


def default_picture() -> DisplayPicture:
    """A new Display's: both unplaced and unset, KELVIN_NEUTRAL, GAMMA_TENTHS_NEUTRAL."""
    unset = StoredSetting(None, AS_THE_DISPLAY_HAS_IT)
    return DisplayPicture(unset, unset, KELVIN_NEUTRAL, GAMMA_TENTHS_NEUTRAL)


def placed_at_first_probe(picture: DisplayPicture, *, brightness_answered: bool,
                          contrast_answered: bool) -> DisplayPicture:
    """Each unplaced setting placed on the display when it answered, else picture adjustment; a
    placed setting is kept whatever the probe says (sticky)."""
    def place(setting: StoredSetting, answered: bool) -> StoredSetting:
        if setting.acts_on is not None:
            return setting
        return StoredSetting(ActsOn.DISPLAY if answered else ActsOn.PICTURE_ADJUSTMENT, setting.value)
    return replace(picture, brightness=place(picture.brightness, brightness_answered),
                   contrast=place(picture.contrast, contrast_answered))


def effective_picture(stored: DisplayPicture, session: PictureSession | None, now: float) -> DisplayPicture:
    """What is in force at Central's `now`: the draft while the session is live; `reverted` once
    its lease ran out without Done; `stored` with no session."""
    if session is None:
        return stored
    return session.draft if session.lease.live(now) else reverted(stored, session)


def reverted(stored: DisplayPicture, session: PictureSession) -> DisplayPicture:
    """`stored`, with each touched setting that is unset there set to its `before_read` value
    (when one was read), so the display goes back to what it showed before the session."""
    def back(setting: StoredSetting, name: Setting, read: int | None) -> StoredSetting:
        if name in session.touched and setting.value == AS_THE_DISPLAY_HAS_IT and read is not None:
            return StoredSetting(setting.acts_on, read)
        return setting
    brightness_read, contrast_read = session.before_read
    return replace(stored, brightness=back(stored.brightness, Setting.BRIGHTNESS, brightness_read),
                   contrast=back(stored.contrast, Setting.CONTRAST, contrast_read))


def edited(session: PictureSession, setting: Setting, value: int, *, expected_sequence: int,
           now: float) -> PictureSession:
    """The session after one edit: the draft with `setting` at `value`, the next sequence, the
    lease touched. PictureRefused: session_ended (lease not live), stale_sequence, out_of_range
    (percent 0..100; kelvin KELVIN_MIN..KELVIN_MAX in KELVIN_STEP; gamma tenths in range),
    not_placed_yet (brightness or contrast still unplaced)."""
    sequence = _next(session, expected_sequence, now)
    if not _in_range(setting, value):
        raise PictureRefused("out_of_range", 422)
    draft = session.draft
    if setting in PLACEABLE:
        current = _setting(draft, setting)
        if current.acts_on is None:
            raise PictureRefused("not_placed_yet", 409)
        draft = _with(draft, setting, StoredSetting(current.acts_on, value))
    elif setting is Setting.COLOUR_TEMPERATURE:
        draft = replace(draft, colour_temperature_kelvin=value)
    else:
        draft = replace(draft, gamma_tenths=value)
    return replace(session, sequence=sequence, draft=draft, touched=session.touched | {setting},
                   lease=touched(session.lease, now, idle_seconds=PICTURE_IDLE_SECONDS))


def placed(session: PictureSession, setting: Setting, acts_on: ActsOn, *, expected_sequence: int,
           answered: bool, now: float) -> PictureSession:
    """The session after the operator moves brightness or contrast to `acts_on`. PictureRefused:
    session_ended, stale_sequence, out_of_range (a setting outside PLACEABLE),
    display_did_not_answer (onto DISPLAY when the report says it did not answer)."""
    sequence = _next(session, expected_sequence, now)
    if setting not in PLACEABLE or not isinstance(acts_on, ActsOn):
        raise PictureRefused("out_of_range", 422)
    if acts_on is ActsOn.DISPLAY and not answered:
        raise PictureRefused("display_did_not_answer", 409)
    draft = _with(session.draft, setting, StoredSetting(acts_on, _setting(session.draft, setting).value))
    return replace(session, sequence=sequence, draft=draft,
                   lease=touched(session.lease, now, idle_seconds=PICTURE_IDLE_SECONDS))


def output_picture(picture: DisplayPicture) -> OutputPicture:
    """The document's picture part: an unplaced setting goes as picture adjustment, unset."""
    def wire(setting: StoredSetting) -> PictureSetting:
        if setting.acts_on is None:
            return PictureSetting(ActsOn.PICTURE_ADJUSTMENT, AS_THE_DISPLAY_HAS_IT)
        return PictureSetting(setting.acts_on, setting.value)
    return OutputPicture(wire(picture.brightness), wire(picture.contrast), picture.colour_temperature_kelvin,
                         picture.gamma_tenths)


def picture_status(*, bound: bool, document: OutputDocument | None,
                   report: OutputReport | None) -> PictureStatus:
    """NO_DISPLAY unless bound with a document; SHOWN when the report's picture part has
    for_change == document.picture_change and presented_change >= document.picture_change; else
    WAITING."""
    if not bound or document is None:
        return PictureStatus.NO_DISPLAY
    shown = (report is not None and report.picture is not None
             and report.picture.for_change == document.picture_change
             and report.picture.presented_change is not None
             and report.picture.presented_change >= document.picture_change)
    return PictureStatus.SHOWN if shown else PictureStatus.WAITING


def copied(source: DisplayPicture, target: DisplayPicture) -> DisplayPicture:
    """`target` with the four values of `source`; where each acts stays the target's."""
    return DisplayPicture(StoredSetting(target.brightness.acts_on, source.brightness.value),
                          StoredSetting(target.contrast.acts_on, source.contrast.value),
                          source.colour_temperature_kelvin, source.gamma_tenths)


def _next(session: PictureSession, expected_sequence: int, now: float) -> int:
    """The session's next sequence; session_ended, then stale_sequence."""
    if not session.lease.live(now):
        raise PictureRefused("session_ended", 410)
    try:
        return next_sequence(session.sequence, expected_sequence)
    except StaleSequence:
        raise PictureRefused("stale_sequence", 409) from None


def _in_range(setting: Setting, value: int) -> bool:
    if type(value) is not int:
        return False
    if setting in PLACEABLE:
        return 0 <= value <= PERCENT_MAX
    if setting is Setting.COLOUR_TEMPERATURE:
        return KELVIN_MIN <= value <= KELVIN_MAX and value % KELVIN_STEP == 0
    return GAMMA_TENTHS_MIN <= value <= GAMMA_TENTHS_MAX


def _setting(picture: DisplayPicture, setting: Setting) -> StoredSetting:
    return picture.brightness if setting is Setting.BRIGHTNESS else picture.contrast


def _with(picture: DisplayPicture, setting: Setting, value: StoredSetting) -> DisplayPicture:
    return replace(picture, **{"brightness" if setting is Setting.BRIGHTNESS else "contrast": value})


__all__ = ["PICTURE_IDLE_SECONDS", "PLACEABLE", "DisplayPicture", "PictureRefused", "PictureSession",
           "PictureStatus", "Setting", "StoredSetting", "copied", "default_picture", "edited",
           "effective_picture", "output_picture", "picture_status", "placed", "placed_at_first_probe",
           "reverted"]
