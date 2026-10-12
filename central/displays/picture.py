"""The Display's picture rules, pure (decision 0022, the picture split; slices S2 and S4; roadmap
1c's Live model; settings #111-#114, #119).

A Display keeps two groups of picture settings, never coupled (owner, 2026-10-12: "i want to
independently control the hardware from the software"): **hardware values**, only the display
settings someone set in Photo Wall, by MCCS code (an absent code is "as the display has it": the
Pi never writes it), and **software values**, Photo Wall's group (the picture split's four and the
print look's: saturation, highlight rolloff, black floor, the LUT and its amount), always complete. No rule here
reads one group to decide the other. What a display offers, and whether a hardware value may be
set on it, is central/displays/hardware.py's.

One home each for:
- **The picture session** (the Live model; central/live_session.py): edits go to a draft that is
  in force while the session is live (`effective_picture`, `kept_picture`); Done stores the draft;
  Revert ends the lease, and Revert or the lease running out puts back what was in force before.
  The stored values never change: Photo Wall keeps what it kept (`kept_picture`), and a touched
  hardware code that was "as the display has it" stays so. Putting the display back is a one-time
  instruction, never a stored value: an ended session's `restore_of` (each such code at the value
  the Pi read just before its first write in the session, when one is known: `with_befores`, from
  each picture report for a change after the session began) rides the document beside the values
  in force (`with_restore`) until the Pi answers a document that carries it (`restore_answered`);
  then the document carries the values in force alone. The projection is a pure function of the
  stored picture, the session, the pending instruction and Central's `now`, so an abandoned
  console needs no reaper.
- **The picture document** (`picture_document`, `picture_digest`): the Output's own key, with its
  own `picture_change`, which rises only when the digest differs (never with the Output
  document's `change`: picture never costs power).
- **Which report answers which picture** (`answers`, `carries`, `hardware_answered`): a report
  answers a document when its for_change is the document's picture_change; the tab's per-row
  results and "changed on the display's buttons" read a report only when it answers the document
  that carries the hardware in force (`hardware_answered`), so a result of an older change is never
  shown as the latest.
- **Done waits for the Pi** (`presented`, `picture_status`): SHOWN once the report answers the
  document that carries exactly the picture Done would keep and the Player presented it.
- **Copy** (#119; owner, 2026-10-12: "Both (Recommended)"; "Only ones set in Photo Wall
  (Recommended)"): every software value, the print look's included; the hardware values set on the source where the
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

from central.displays.hardware import copied_hardware, edit_refusal
from central.live_session import Lease, StaleSequence, next_sequence, touched
from contracts.lut import LutRef
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
    """Photo Wall's picture settings, as the routes name them (the ranges are
    contracts.node_picture's `SoftwarePicture`)."""
    BRIGHTNESS = "brightness"                   # #111, percent 0..150 (100 changes nothing)
    CONTRAST = "contrast"                       # #112, percent 0..150
    COLOUR_TEMPERATURE = "colour-temperature"   # #113, kelvin
    GAMMA = "gamma"                             # #114, tenths
    SATURATION = "saturation"                   # print look: percent 0..150, 100 changes nothing
    HIGHLIGHT_ROLLOFF = "highlight-rolloff"     # print look: percent 0..100, 0 = the clip at white
    BLACK_FLOOR = "black-floor"                 # print look: tenths of a percent of white light 0..100
    LUT_AMOUNT = "lut-amount"                   # print look: percent 0..100 of the LUT mixed in


class SoftwareChoice(StrEnum):
    """Photo Wall's picture settings that name a thing rather than set a number, each with its own
    edit (one route per question): the LUT choice (`edited_lut`, `.../lut-edits`)."""
    LUT = "lut"


SoftwareTouch = SoftwareSetting | SoftwareChoice     # what a session records as touched in the group


def software_touch(name: str) -> SoftwareTouch:
    """A touched software setting by its stored name. ValueError for an unknown one."""
    return SoftwareChoice(name) if name in SoftwareChoice._value2member_map_ else SoftwareSetting(name)


_SOFTWARE_FIELDS: Final[Mapping[SoftwareSetting, str]] = {
    SoftwareSetting.BRIGHTNESS: "brightness_percent",
    SoftwareSetting.CONTRAST: "contrast_percent",
    SoftwareSetting.COLOUR_TEMPERATURE: "colour_temperature_kelvin",
    SoftwareSetting.GAMMA: "gamma_tenths",
    SoftwareSetting.SATURATION: "saturation_percent",
    SoftwareSetting.HIGHLIGHT_ROLLOFF: "highlight_rolloff_percent",
    SoftwareSetting.BLACK_FLOOR: "black_floor_tenths",
    SoftwareSetting.LUT_AMOUNT: "lut_amount_percent",
}


class PictureStatus(StrEnum):
    """What the Picture tab's Done waits on."""
    NO_DISPLAY = "no-display"   # no Display recorded on the bound Output, or not bound
    WAITING = "waiting"         # the Pi has not answered the latest picture change yet
    SHOWN = "shown"             # written to the display and presented by the Player: Done may keep it


class PictureRefused(Exception):
    """A refusal the routes answer as {"error": code}: stale_sequence 409, not_offered 409,
    out_of_range 422, unknown_lut 422, session_ended 410, not_presented_yet 409."""

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
    touched_software: frozenset[SoftwareTouch]
    lease: Lease


def default_picture() -> DisplayPicture:
    """A new Display's: no hardware value set, the software group neutral (NEUTRAL_SOFTWARE)."""
    return DisplayPicture({}, NEUTRAL_SOFTWARE)


def effective_picture(stored: DisplayPicture, session: PictureSession | None, now: float) -> DisplayPicture:
    """What the picture document carries at Central's `now`: the draft while the session is live;
    `reverted` once it ended without Done (Revert, or its lease ran out) until the ended session
    goes; `stored` with no session."""
    if session is None:
        return stored
    return session.draft if session.lease.live(now) else reverted(stored, session)


def kept_picture(stored: DisplayPicture, session: PictureSession | None, now: float) -> DisplayPicture:
    """What Photo Wall keeps for the Display at `now`, as the tab shows it: the draft while the
    session is live, else `stored` (a reverted session sets nothing)."""
    return session.draft if session is not None and session.lease.live(now) else stored


def reverted(stored: DisplayPicture, session: PictureSession) -> DisplayPicture:
    """The one-time instruction that puts the display back: `stored`, with each touched hardware
    code absent from `stored.hardware` at `session.hardware_before[code]` when the Pi reported one,
    so the display goes back to what it showed before the session. Never stored."""
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
    """The session after one of Photo Wall's numeric edits. PictureRefused: session_ended,
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


def edited_lut(session: PictureSession, lut: LutRef | None, *, known: bool, expected_sequence: int,
               now: float) -> PictureSession:
    """The session after the LUT choice: `lut` None is no LUT; `known` says the LUTs list holds
    `lut`'s paper profile (the caller's read, in the edit's transaction). PictureRefused:
    session_ended, stale_sequence, then unknown_lut 422 for a LUT not on the list."""
    sequence = _next(session, expected_sequence, now)
    if lut is not None and not known:
        raise PictureRefused("unknown_lut", 422)
    software = replace(session.draft.software, lut=lut)
    return replace(session, sequence=sequence, draft=replace(session.draft, software=software),
                   touched_software=session.touched_software | {SoftwareChoice.LUT},
                   lease=touched(session.lease, now, idle_seconds=PICTURE_IDLE_SECONDS))


def picture_document(output_id: str, picture_change: int, picture: DisplayPicture) -> PictureDocument:
    """The Output's picture document at `picture_change`."""
    return PictureDocument(output_id, picture_change, dict(picture.hardware), picture.software)


def picture_digest(picture: DisplayPicture) -> str:
    """sha256 hex of the canonical {hardware, software}; the projector raises the Output's
    `picture_change` when it differs."""
    body = {"hardware": dict(picture.hardware), "software": asdict(picture.software)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def answers(document: PictureDocument | None, report: PictureReport | None) -> bool:
    """`report` answers `document`: its for_change is the document's picture_change."""
    return document is not None and report is not None and report.for_change == document.picture_change


def carries(document: PictureDocument | None, picture: DisplayPicture) -> bool:
    """`document` carries exactly `picture`, both groups (the document is projected from the
    session asynchronously, so the recorded one may still be the previous)."""
    return (document is not None and dict(document.hardware) == dict(picture.hardware)
            and document.software == picture.software)


def hardware_answered(hardware: Mapping[VcpCode, int], document: PictureDocument | None,
                      report: PictureReport | None) -> bool:
    """`report` answers the document that carries exactly `hardware` (the software group is not
    read: the two groups are never coupled)."""
    return answers(document, report) and document is not None and dict(document.hardware) == dict(hardware)


def presented(picture: DisplayPicture, document: PictureDocument | None, report: PictureReport | None) -> bool:
    """The Pi answered the document that carries exactly `picture`, not an older one, and the
    Player presented it: a report answering the previous document must neither show the Picture
    tab `shown` nor let Done keep the draft."""
    return (carries(document, picture) and answers(document, report) and report is not None
            and document is not None and report.presented_change is not None
            and report.presented_change >= document.picture_change)


def picture_status(*, bound: bool, picture: DisplayPicture, document: PictureDocument | None,
                   report: PictureReport | None) -> PictureStatus:
    """NO_DISPLAY unless bound with a document; SHOWN when `presented(picture, …)`; else WAITING."""
    if not bound or document is None:
        return PictureStatus.NO_DISPLAY
    return PictureStatus.SHOWN if presented(picture, document, report) else PictureStatus.WAITING


def restore_of(stored: DisplayPicture, session: PictureSession) -> dict[VcpCode, int]:
    """The one-time instruction an ended session leaves: the codes `reverted` puts back that
    `stored` does not set, each at the Pi's first `before`."""
    return {code: value for code, value in reverted(stored, session).hardware.items() if code not in stored.hardware}


def with_restore(picture: DisplayPicture, restore: Mapping[VcpCode, int]) -> DisplayPicture:
    """What the picture document carries: `picture`, with each code of the pending instruction
    `restore` that `picture` does not set (a value in force always wins)."""
    back = {code: value for code, value in restore.items() if code not in picture.hardware}
    if not back:
        return picture
    return replace(picture, hardware=_in_list_order({**picture.hardware, **back}))


def restore_answered(restore: Mapping[VcpCode, int], in_force: DisplayPicture, document: PictureDocument | None,
                     report: PictureReport | None) -> bool:
    """The pending instruction is done: `report` answers a document that carries it (each code at
    its restore value, or at the value in force that overrides it), never an older one."""
    return answers(document, report) and document is not None and all(
        document.hardware.get(code) == in_force.hardware.get(code, value) for code, value in restore.items())


def copied(source: DisplayPicture, target: DisplayPicture, *,
           source_offered: tuple[HardwareSetting, ...] | None,
           target_offered: tuple[HardwareSetting, ...] | None) -> tuple[DisplayPicture, tuple[VcpCode, ...]]:
    """(`target` with `source`'s software and the copied hardware merged over the target's, the
    source codes skipped in picture-list order): `hardware.copied_hardware` decides which hardware
    values carry over; a skipped code, and every target code the source does not set, stays as it
    is on the target."""
    hardware, skipped = copied_hardware(source.hardware, source_offered=source_offered,
                                        target_offered=target_offered)
    return DisplayPicture(_in_list_order({**target.hardware, **hardware}), source.software), skipped


def with_befores(session: PictureSession, report: PictureReport) -> PictureSession:
    """`session` with the befores `report` brings: when the report answers a picture change after
    the session began (`for_change > began_at_change`), each result's `before` (when the display
    answered it) for a code with none yet. The first one per code is kept, so a second write in the
    session never replaces what the display showed before it began."""
    if report.for_change is None or report.for_change <= session.began_at_change:
        return session
    befores = {code: result.before for code, result in report.results.items()
               if result.before is not None and code not in session.hardware_before}
    if not befores:
        return session
    return replace(session, hardware_before=_in_list_order({**session.hardware_before, **befores}))


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
           "SoftwareChoice", "SoftwareSetting", "SoftwareTouch", "answers", "carries", "copied", "default_picture",
           "edited_hardware", "edited_lut", "edited_software", "software_touch",
           "effective_picture", "hardware_answered", "kept_picture", "picture_digest", "picture_document",
           "picture_status", "presented", "restore_answered", "restore_of", "reverted", "with_befores", "with_restore"]
