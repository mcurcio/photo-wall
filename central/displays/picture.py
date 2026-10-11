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

from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from central.live_session import Lease
from contracts.node_output import (
    ActsOn,
    OutputDocument,
    OutputPicture,
    OutputReport,
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
    raise NotImplementedError


def placed_at_first_probe(picture: DisplayPicture, *, brightness_answered: bool,
                          contrast_answered: bool) -> DisplayPicture:
    """Each unplaced setting placed on the display when it answered, else picture adjustment; a
    placed setting is kept whatever the probe says (sticky)."""
    raise NotImplementedError


def effective_picture(stored: DisplayPicture, session: PictureSession | None, now: float) -> DisplayPicture:
    """What is in force at Central's `now`: the draft while the session is live; `reverted` once
    its lease ran out without Done; `stored` with no session."""
    raise NotImplementedError


def reverted(stored: DisplayPicture, session: PictureSession) -> DisplayPicture:
    """`stored`, with each touched setting that is unset there set to its `before_read` value
    (when one was read), so the display goes back to what it showed before the session."""
    raise NotImplementedError


def edited(session: PictureSession, setting: Setting, value: int, *, expected_sequence: int,
           now: float) -> PictureSession:
    """The session after one edit: the draft with `setting` at `value`, the next sequence, the
    lease touched. PictureRefused: session_ended (lease not live), stale_sequence, out_of_range
    (percent 0..100; kelvin KELVIN_MIN..KELVIN_MAX in KELVIN_STEP; gamma tenths in range),
    not_placed_yet (brightness or contrast still unplaced)."""
    raise NotImplementedError


def placed(session: PictureSession, setting: Setting, acts_on: ActsOn, *, expected_sequence: int,
           answered: bool, now: float) -> PictureSession:
    """The session after the operator moves brightness or contrast to `acts_on`. PictureRefused:
    session_ended, stale_sequence, out_of_range (a setting outside PLACEABLE),
    display_did_not_answer (onto DISPLAY when the report says it did not answer)."""
    raise NotImplementedError


def output_picture(picture: DisplayPicture) -> OutputPicture:
    """The document's picture part: an unplaced setting goes as picture adjustment, unset."""
    raise NotImplementedError


def picture_status(*, bound: bool, document: OutputDocument | None,
                   report: OutputReport | None) -> PictureStatus:
    """NO_DISPLAY unless bound with a document carrying a picture part; SHOWN when the report's
    picture part has for_change == document.picture_change and presented_change >=
    document.picture_change; else WAITING."""
    raise NotImplementedError


def copied(source: DisplayPicture, target: DisplayPicture) -> DisplayPicture:
    """`target` with the four values of `source`; where each acts stays the target's."""
    raise NotImplementedError


__all__ = ["PICTURE_IDLE_SECONDS", "PLACEABLE", "DisplayPicture", "PictureRefused", "PictureSession",
           "PictureStatus", "Setting", "StoredSetting", "copied", "default_picture", "edited",
           "effective_picture", "output_picture", "picture_status", "placed", "placed_at_first_probe",
           "reverted"]
