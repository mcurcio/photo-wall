"""The Display's picture rules, pure (decision 0022, the picture split; slices S2 and S4; roadmap
1c's Live model; settings #111-#114, #119).

A Display keeps two groups of picture settings, never coupled (owner, 2026-10-12: "i want to
independently control the hardware from the software"): **hardware values**, only the display
settings someone set in Photo Wall, by MCCS code (an absent code is "as the display has it": the
Pi never writes it), and **software values**, Photo Wall's four, always complete. No rule here
reads one group to decide the other. What a display offers, and whether a hardware value may be
set on it, is central/displays/hardware.py's.

One home each for:
- **The picture session** (the Live model; central/live_session.py): edits go to a draft that is
  in force while the session is live (`effective_picture`); Done stores the draft; Revert, or the
  session's lease running out, puts back what was in force before (`reverted`: the stored values,
  and a touched hardware code that was "as the display has it" goes to the value the Pi read just
  before its first write in the session, when one is known). The projection is a pure function of
  the stored picture, the session and Central's `now`, so an abandoned console needs no reaper.
- **The picture document** (`picture_document`, `picture_digest`): the Output's own key, with its
  own `picture_change`, which rises only when the digest differs (never with the Output
  document's `change`: picture never costs power).
- **Done waits for the Pi** (`picture_status`, `presented`): SHOWN once the report answers the
  document's picture change (every hardware write done) and the Player presented it, and the
  document carries the values Done would keep.
- **Copy** (#119; owner, 2026-10-12: "Both (Recommended)"; "Only ones set in Photo Wall
  (Recommended)"): all four software values; the hardware values set on the source where the
  target offers them (hardware.py's rule, slice S4); never position.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Final
from uuid import UUID

from central.displays.hardware import edit_refusal
from central.live_session import Lease, StaleSequence, next_sequence, touched
from contracts.node_picture import (
    NEUTRAL_SOFTWARE,
    PICTURE_LIST,
    HardwareSetting,
    PictureDocument,
    PictureReport,
    SoftwarePicture,
    VcpCode,
)

PICTURE_IDLE_SECONDS: Final = 5.0     # a picture session ends this long after its last edit or keepalive


class SoftwareSetting(StrEnum):
    """Photo Wall's four picture settings, as the routes name them."""
    BRIGHTNESS = "brightness"                   # #111, percent 0..150 (100 changes nothing)
    CONTRAST = "contrast"                       # #112, percent 0..150
    COLOUR_TEMPERATURE = "colour-temperature"   # #113, kelvin
    GAMMA = "gamma"                             # #114, tenths


_SOFTWARE_FIELDS: Final[Mapping[SoftwareSetting, str]] = {
    SoftwareSetting.BRIGHTNESS: "brightness_percent",
    SoftwareSetting.CONTRAST: "contrast_percent",
    SoftwareSetting.COLOUR_TEMPERATURE: "colour_temperature_kelvin",
    SoftwareSetting.GAMMA: "gamma_tenths",
}


class PictureStatus(StrEnum):
    """What the Picture tab's Done waits on."""
    NO_DISPLAY = "no-display"   # no Display recorded on the bound Output, or not bound
    WAITING = "waiting"         # the Pi has not answered the latest picture change yet
    SHOWN = "shown"             # written to the display and presented by the Player: Done may keep it


class PictureRefused(Exception):
    """A refusal the routes answer as {"error": code}: stale_sequence 409, not_offered 409,
    out_of_range 422, session_ended 410, not_presented_yet 409."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class DisplayPicture:
    """The Display record's picture: `hardware`, only codes set in Photo Wall (treat as read-only);
    `software`, always complete."""
    hardware: Mapping[VcpCode, int]
    software: SoftwarePicture


@dataclass(frozen=True)
class PictureSession:
    """One open picture session on one Display (picture_sessions row). `began_at_change`: the
    Output's picture change at begin (0: none), so the befores come from later results;
    `hardware_before`: the first `before` the Pi reported per code in this session (slice S4)."""
    session_id: UUID
    display_id: UUID
    sequence: int
    draft: DisplayPicture
    began_at_change: int
    hardware_before: Mapping[VcpCode, int]
    touched_hardware: frozenset[VcpCode]
    touched_software: frozenset[SoftwareSetting]
    lease: Lease


def default_picture() -> DisplayPicture:
    """A new Display's: no hardware value set, the software group neutral (NEUTRAL_SOFTWARE)."""
    return DisplayPicture({}, NEUTRAL_SOFTWARE)


def effective_picture(stored: DisplayPicture, session: PictureSession | None, now: float) -> DisplayPicture:
    """What is in force at Central's `now`: the draft while the session is live; `reverted` once
    its lease ran out without Done; `stored` with no session."""
    if session is None:
        return stored
    return session.draft if session.lease.live(now) else reverted(stored, session)


def reverted(stored: DisplayPicture, session: PictureSession) -> DisplayPicture:
    """`stored`, with each touched hardware code absent from `stored.hardware` set to
    `session.hardware_before[code]` when the Pi reported one, so the display goes back to what it
    showed before the session."""
    back = {code: session.hardware_before[code] for code in session.touched_hardware
            if code not in stored.hardware and code in session.hardware_before}
    if not back:
        return stored
    return replace(stored, hardware=_in_list_order({**stored.hardware, **back}))


def edited_hardware(session: PictureSession, code: VcpCode, value: int | None, *,
                    offered: tuple[HardwareSetting, ...] | None, expected_sequence: int,
                    now: float) -> PictureSession:
    """The session after one display setting's edit: `value` None removes `code` from the draft
    (as the display has it). PictureRefused: session_ended, stale_sequence, then not_offered 409 or
    out_of_range 422 (`hardware.edit_refusal` against `offered`)."""
    sequence = _next(session, expected_sequence, now)
    refusal = edit_refusal(code, value, offered)
    if refusal is not None:
        raise PictureRefused(refusal, 409 if refusal == "not_offered" else 422)
    hardware = {key: kept for key, kept in session.draft.hardware.items() if key != code}
    if value is not None:
        hardware[code] = value
    return replace(session, sequence=sequence, draft=replace(session.draft, hardware=_in_list_order(hardware)),
                   touched_hardware=session.touched_hardware | {code},
                   lease=touched(session.lease, now, idle_seconds=PICTURE_IDLE_SECONDS))


def edited_software(session: PictureSession, setting: SoftwareSetting, value: int, *,
                    expected_sequence: int, now: float) -> PictureSession:
    """The session after one of Photo Wall's four edits. PictureRefused: session_ended,
    stale_sequence, out_of_range 422 (contracts.node_picture's ranges: `SoftwarePicture`)."""
    sequence = _next(session, expected_sequence, now)
    if not isinstance(setting, SoftwareSetting) or type(value) is not int:
        raise PictureRefused("out_of_range", 422)
    try:
        software = replace(session.draft.software, **{_SOFTWARE_FIELDS[setting]: value})
    except ValueError:
        raise PictureRefused("out_of_range", 422) from None
    return replace(session, sequence=sequence, draft=replace(session.draft, software=software),
                   touched_software=session.touched_software | {setting},
                   lease=touched(session.lease, now, idle_seconds=PICTURE_IDLE_SECONDS))


def picture_document(output_id: str, picture_change: int, picture: DisplayPicture) -> PictureDocument:
    """The Output's picture document at `picture_change`."""
    return PictureDocument(output_id, picture_change, dict(picture.hardware), picture.software)


def picture_digest(picture: DisplayPicture) -> str:
    """sha256 hex of the canonical {hardware, software}; the projector raises the Output's
    `picture_change` when it differs."""
    body = {"hardware": dict(picture.hardware), "software": asdict(picture.software)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def picture_status(*, bound: bool, document: PictureDocument | None,
                   report: PictureReport | None) -> PictureStatus:
    """NO_DISPLAY unless bound with a document; SHOWN when the report's for_change is the document's
    picture_change and its presented_change >= it; else WAITING."""
    if not bound or document is None:
        return PictureStatus.NO_DISPLAY
    shown = (report is not None and report.for_change == document.picture_change
             and report.presented_change is not None and report.presented_change >= document.picture_change)
    return PictureStatus.SHOWN if shown else PictureStatus.WAITING


def presented(picture: DisplayPicture, document: PictureDocument | None, report: PictureReport | None) -> bool:
    """The Pi carried out the document that carries exactly `picture`, not an older one: the
    document is projected from the session asynchronously, so a report answering the previous
    document must neither show the Picture tab `shown` nor let Done keep the draft."""
    return (document is not None and dict(document.hardware) == dict(picture.hardware)
            and document.software == picture.software
            and picture_status(bound=True, document=document, report=report) is PictureStatus.SHOWN)


def copied(source: DisplayPicture, target: DisplayPicture, *,
           source_offered: tuple[HardwareSetting, ...] | None,
           target_offered: tuple[HardwareSetting, ...] | None) -> tuple[DisplayPicture, tuple[VcpCode, ...]]:
    """(`target` with `source`'s software and the copied hardware merged over the target's, the
    source codes skipped in picture-list order). Slice S2 copies no hardware value (every source
    code is skipped and left as it is on the target); S4 copies by `hardware.copied_hardware`."""
    del source_offered, target_offered   # S4's rule reads them
    skipped = tuple(code for code in PICTURE_LIST if code in source.hardware)
    return DisplayPicture(dict(target.hardware), source.software), skipped


def _next(session: PictureSession, expected_sequence: int, now: float) -> int:
    """The session's next sequence; session_ended, then stale_sequence."""
    if not session.lease.live(now):
        raise PictureRefused("session_ended", 410)
    try:
        return next_sequence(session.sequence, expected_sequence)
    except StaleSequence:
        raise PictureRefused("stale_sequence", 409) from None


def _in_list_order(hardware: Mapping[VcpCode, int]) -> dict[VcpCode, int]:
    return {code: hardware[code] for code in PICTURE_LIST if code in hardware}


__all__ = ["PICTURE_IDLE_SECONDS", "DisplayPicture", "PictureRefused", "PictureSession", "PictureStatus",
           "SoftwareSetting", "copied", "default_picture", "edited_hardware", "edited_software",
           "effective_picture", "picture_digest", "picture_document", "picture_status", "presented",
           "reverted"]
