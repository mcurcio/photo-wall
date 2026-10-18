"""The Display's picture rules, pure (decision 0022, the picture split; slices S2 and S4; roadmap
1c's Live model; settings #111-#114, #119).

A Display keeps two groups of picture settings, never coupled (owner, 2026-10-12: "i want to
independently control the hardware from the software"): **hardware values**, only the display
settings someone set in Photo Wall, by MCCS code (an absent code is "as the display has it": the
Pi never writes it), and **software values**, Photo Wall's group (contrast, colour temperature and
gamma), always complete. No rule here reads one group to decide the other. Room light (decision 0028;
owner, 2026-10-15, Q2 "the paper replaces them") removed the print look's brightness, saturation,
highlight rolloff, black floor and LUT from the Display and added a fourth group, the **Display's
light** (`DisplayLight`: the matched lux and the panel's black), live in the session's draft
(`edited_light`), kept by Done, put back by Revert and the lease's end, copied by Copy and recorded
in the picture history. What a display offers, and whether a hardware value may be
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
- **Display tuning** (decision 0024; design rules 1-3): the Display's **signal range** (the Pi's
  HDMI RGB quantization range, `SignalRange`) is a third group, stored, live in the session's draft,
  kept by Done and put back by Revert and the lease's end (`reverted` keeps the stored one); Done
  also waits for the Pi's signal answer for the change (`signal_answered`: any result answers). A
  **test pattern** is the session's, never the draft's and never stored (`edited_test_pattern`,
  `TestPatternAsk`): it rides the document only while the session is live (`session_test_pattern`),
  so Done, Revert and the lease's end each take it off; `test_pattern_state` says what the tab
  shows of it.
- **Copy** (#119; owner, 2026-10-12: "Both (Recommended)"; "Only ones set in Photo Wall
  (Recommended)"): every software value and the Display's light; the hardware values set on the
  source where the target offers them (hardware.py's rule, slice S4); never position, never the
  signal range (it follows each display).
- **Restore** (configuration snapshots, migration 096; owner, 2026-10-13: "View and restore"):
  `restored` loads a picture history entry of the session's own Display into the draft, live and
  put back by Revert like any edit; Done still keeps it, and that kept entry names the entry it
  came from (`restored_from`). Hardware the display no longer offers is skipped, an entry from
  before the Display's light keeps the draft's light (and says so), and the test pattern is never
  restored. Another Display's values cross only by Copy.
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
from contracts.node_picture import (
    NEUTRAL_SOFTWARE,
    PICTURE_LIST,
    TEST_PATTERN_SECONDS,
    UPGRADE_DISPLAY_LIGHT,
    DisplayLight,
    HardwareSetting,
    PictureDocument,
    PictureReport,
    SignalRange,
    SoftwarePicture,
    TestPatternAsk,
    VcpCode,
)
from contracts.tuning import TestPattern

PICTURE_IDLE_SECONDS: Final = 5.0     # a picture session ends this long after its last edit or keepalive
TEST_PATTERN_IDLE_SECONDS: Final = 60.0   # ... while it asks a test pattern (`idle_seconds`)


class SoftwareSetting(StrEnum):
    """Photo Wall's picture settings, as the routes name them (the ranges are
    contracts.node_picture's `SoftwarePicture`)."""
    CONTRAST = "contrast"                       # #112, percent 0..150
    COLOUR_TEMPERATURE = "colour-temperature"   # #113, kelvin
    GAMMA = "gamma"                             # #114, tenths


SoftwareTouch = SoftwareSetting     # what a session records as touched in the group


def software_touch(name: str) -> SoftwareTouch:
    """A touched software setting by its stored name. ValueError for an unknown one."""
    return SoftwareSetting(name)


_SOFTWARE_FIELDS: Final[Mapping[SoftwareSetting, str]] = {
    SoftwareSetting.CONTRAST: "contrast_percent",
    SoftwareSetting.COLOUR_TEMPERATURE: "colour_temperature_kelvin",
    SoftwareSetting.GAMMA: "gamma_tenths",
}


class PictureStatus(StrEnum):
    """What the Picture tab's Done waits on."""
    NO_DISPLAY = "no-display"   # no Display recorded on the bound Output, or not bound
    WAITING = "waiting"         # the Pi has not answered the latest picture change yet
    SHOWN = "shown"             # written to the display and presented by the Player: Done may keep it


class TestPatternState(StrEnum):
    """What the tab says of the session's test pattern."""
    __test__ = False        # a domain name, not a pytest class

    WAITING = "waiting"     # asked; the Pi has not reported it on screen yet
    SHOWN = "shown"         # the report's test_pattern_shown is this ask
    ENDED = "ended"         # the report answers and presents the document carrying this ask, yet shows
                            # no pattern: the Pi took it off at its bound (TEST_PATTERN_SECONDS)


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
    `software`, always complete; `signal_range`, the Pi's HDMI signal range for this display (no
    default: every constructor decides, and Copy keeps the target's); `light`, the Display's light
    (decision 0028)."""
    hardware: Mapping[VcpCode, int]
    software: SoftwarePicture
    signal_range: SignalRange
    light: DisplayLight


@dataclass(frozen=True)
class PictureSession:
    """One open picture session on one Display (picture_sessions row). `began_at_change`: the
    Output's picture change at begin (0: none), so the befores come from later results;
    `hardware_before`: the first `before` the Pi reported per code in this session (slice S4);
    `test_pattern`: the session's test pattern ask, never in the draft (None: off); `restored_from`:
    the picture history entry the draft was last restored from (None: none)."""
    session_id: UUID
    display_id: UUID
    sequence: int
    draft: DisplayPicture
    began_at_change: int
    hardware_before: Mapping[VcpCode, int]
    touched_hardware: frozenset[VcpCode]
    touched_software: frozenset[SoftwareTouch]
    lease: Lease
    test_pattern: TestPatternAsk | None
    restored_from: UUID | None


def default_picture() -> DisplayPicture:
    """A new Display's: no hardware value set, the software group neutral (NEUTRAL_SOFTWARE), the
    signal range Auto (the kernel's choice: no change), the light the placeholder every Display
    starts from (UPGRADE_DISPLAY_LIGHT) until the paper match is typed in."""
    return DisplayPicture({}, NEUTRAL_SOFTWARE, SignalRange.AUTO, UPGRADE_DISPLAY_LIGHT)


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
    so the display goes back to what it showed before the session; the signal range is `stored`'s.
    Never stored."""
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
                   lease=_touched(session, now))


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
                   lease=_touched(session, now))


def edited_light(session: PictureSession, light: DisplayLight, *, expected_sequence: int,
                 now: float) -> PictureSession:
    """The session after the Display's light's edit (decision 0028): the draft's light, live on the
    Display, kept by Done and put back by Revert. PictureRefused: session_ended, stale_sequence,
    out_of_range 422 (not a DisplayLight; its ranges are the contract's)."""
    sequence = _next(session, expected_sequence, now)
    if not isinstance(light, DisplayLight):
        raise PictureRefused("out_of_range", 422)
    return replace(session, sequence=sequence, draft=replace(session.draft, light=light),
                   lease=_touched(session, now))


def edited_signal_range(session: PictureSession, signal_range: SignalRange, *, expected_sequence: int,
                        now: float) -> PictureSession:
    """The session after the signal range's edit: the draft's range, live on the Display, kept by
    Done and put back by Revert. PictureRefused: session_ended, stale_sequence, out_of_range 422
    (not a SignalRange)."""
    sequence = _next(session, expected_sequence, now)
    if not isinstance(signal_range, SignalRange):
        raise PictureRefused("out_of_range", 422)
    return replace(session, sequence=sequence, draft=replace(session.draft, signal_range=signal_range),
                   lease=_touched(session, now))


def edited_test_pattern(session: PictureSession, pattern: TestPattern | None, *, expected_sequence: int,
                        now: float) -> PictureSession:
    """The session after the test pattern's edit: `test_pattern` the ask (`pattern`, this edit's
    sequence, so asking again gives a new ask the Pi bounds afresh), or None (off); the draft is
    untouched. PictureRefused: session_ended, stale_sequence, out_of_range 422 (not a TestPattern)."""
    sequence = _next(session, expected_sequence, now)
    if pattern is not None and not isinstance(pattern, TestPattern):
        raise PictureRefused("out_of_range", 422)
    ask = None if pattern is None else TestPatternAsk(pattern, sequence)
    return _kept(replace(session, sequence=sequence, test_pattern=ask), now)


def session_test_pattern(session: PictureSession | None, now: float) -> TestPatternAsk | None:
    """The test pattern the picture document carries at Central's `now`: the live session's ask;
    None with no session or once its lease ended, so Done, Revert and the lease's end each take it
    off (design rule 1). The Pi's own bound (TEST_PATTERN_SECONDS) covers Central going away."""
    return session.test_pattern if session is not None and session.lease.live(now) else None


def picture_document(output_id: str, picture_change: int, picture: DisplayPicture,
                     test_pattern: TestPatternAsk | None) -> PictureDocument:
    """The Output's picture document at `picture_change`: `picture`'s four groups and the live
    session's `test_pattern` (`session_test_pattern`)."""
    return PictureDocument(output_id, picture_change, dict(picture.hardware), picture.software,
                           picture.signal_range, test_pattern, light=picture.light)


def picture_digest(picture: DisplayPicture, test_pattern: TestPatternAsk | None) -> str:
    """sha256 hex of the canonical {hardware, software, signal_range, light, test_pattern}; the
    projector raises the Output's `picture_change` when it differs, so any of them moving raises it."""
    pattern = None if test_pattern is None else {"pattern": test_pattern.pattern.value, "ask": test_pattern.ask}
    body = {"hardware": dict(picture.hardware), "software": asdict(picture.software),
            "signal_range": picture.signal_range.value, "light": asdict(picture.light), "test_pattern": pattern}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def answers(document: PictureDocument | None, report: PictureReport | None) -> bool:
    """`report` answers `document`: its for_change is the document's picture_change."""
    return document is not None and report is not None and report.for_change == document.picture_change


def carries(document: PictureDocument | None, picture: DisplayPicture) -> bool:
    """`document` carries exactly `picture`, all four groups (the document is projected from the
    session asynchronously, so the recorded one may still be the previous). The test pattern is not
    read: it is never the draft's."""
    return (document is not None and dict(document.hardware) == dict(picture.hardware)
            and document.software == picture.software and document.signal_range == picture.signal_range
            and document.light == picture.light)


def hardware_answered(hardware: Mapping[VcpCode, int], document: PictureDocument | None,
                      report: PictureReport | None) -> bool:
    """`report` answers the document that carries exactly `hardware` (the software group is not
    read: the two groups are never coupled)."""
    return answers(document, report) and document is not None and dict(document.hardware) == dict(hardware)


def signal_answered(document: PictureDocument | None, report: PictureReport | None) -> bool:
    """The Pi answered `document`'s signal range: `report.signal` is for its picture_change. Any
    result answers it (applied, unsupported or failed; the tab shows which and the range in force)."""
    return (document is not None and report is not None and report.signal is not None
            and report.signal.for_change == document.picture_change)


def presented(picture: DisplayPicture, document: PictureDocument | None, report: PictureReport | None) -> bool:
    """The Pi answered the document that carries exactly `picture`, not an older one, its signal
    range included (`signal_answered`), and the Player presented it: a report answering the previous
    document must neither show the Picture tab `shown` nor let Done keep the draft."""
    return carries(document, picture) and _presents(document, report) and signal_answered(document, report)


def test_pattern_state(ask: TestPatternAsk, document: PictureDocument | None,
                       report: PictureReport | None) -> TestPatternState:
    """SHOWN when the report's `test_pattern_shown` is `ask`; ENDED when the report answers and
    presents the document carrying `ask` yet shows no pattern (the Pi took it off at its bound,
    TEST_PATTERN_SECONDS after it received it); else WAITING."""
    if report is not None and report.test_pattern_shown == ask:
        return TestPatternState.SHOWN
    if (document is not None and document.test_pattern == ask and _presents(document, report)
            and report is not None and report.test_pattern_shown is None):
        return TestPatternState.ENDED
    return TestPatternState.WAITING


test_pattern_state.__test__ = False     # a domain rule, not a pytest test (tests import it by name)


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
    """(`target` with `source`'s software and light and the copied hardware merged over the
    target's, the source codes skipped in picture-list order): `hardware.copied_hardware` decides
    which hardware values carry over; a skipped code, and every target code the source does not set,
    stays as it is on the target, and so does the target's signal range (it follows each display)."""
    hardware, skipped = copied_hardware(source.hardware, source_offered=source_offered,
                                        target_offered=target_offered)
    return DisplayPicture(_in_list_order({**target.hardware, **hardware}), source.software,
                          target.signal_range, source.light), skipped


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


@dataclass(frozen=True)
class PictureRestored:
    """A Restore's outcome (configuration snapshots; `restored`): the session after it; the entry's
    hardware codes the display does not offer now, in picture-list order (not in the draft); and
    whether the entry had no Display's light (written before decision 0028), so the draft kept its
    own light."""
    session: PictureSession
    hardware_skipped: tuple[VcpCode, ...]
    light_kept: bool


def restored(session: PictureSession, *, entry_id: UUID, entry_display_id: UUID, picture: DisplayPicture,
             light_recorded: bool, offered: tuple[HardwareSetting, ...] | None, expected_sequence: int,
             now: float) -> PictureRestored:
    """The session after a Restore loads a history entry's `picture` (its id `entry_id`, written for
    `entry_display_id`) into the draft, live on the Display; the operator still presses Done.
    PictureRefused: session_ended 410, stale_sequence 409, then other_display 409 when
    `entry_display_id` is not the session's Display (another display's values cross only by Copy).
    The draft becomes:
    - hardware: each of `picture`'s codes that `hardware.edit_refusal` allows against `offered`
      (the rest are `hardware_skipped`, in picture-list order; `offered` None skips them all); a
      code `picture` does not set is "as the display has it";
    - software and signal range: `picture`'s;
    - light: `picture`'s when `light_recorded`, else the draft's own (`light_kept`: the entry was
      written before the Display had a light).
    The test pattern is never restored (the session's ask stays). Every hardware code and software
    setting whose draft value changed joins the touched sets (so Revert puts the display back),
    `restored_from` becomes `entry_id`, and the lease is touched."""
    sequence = _next(session, expected_sequence, now)
    if entry_display_id != session.display_id:
        raise PictureRefused("other_display", 409)
    hardware = {code: value for code, value in picture.hardware.items() if edit_refusal(code, value, offered) is None}
    skipped = tuple(code for code in PICTURE_LIST if code in picture.hardware and code not in hardware)
    before = session.draft
    draft = DisplayPicture(_in_list_order(hardware), picture.software, picture.signal_range,
                           picture.light if light_recorded else before.light)
    touched_hardware = {code for code in {*before.hardware, *draft.hardware}
                        if before.hardware.get(code) != draft.hardware.get(code)}
    touched_software = {setting for setting, field in _SOFTWARE_FIELDS.items()
                        if getattr(before.software, field) != getattr(draft.software, field)}
    return PictureRestored(
        replace(session, sequence=sequence, draft=draft, touched_hardware=session.touched_hardware | touched_hardware,
                touched_software=session.touched_software | touched_software, restored_from=entry_id,
                lease=_touched(session, now)),
        skipped, not light_recorded)


def idle_seconds(session: PictureSession) -> float:
    """How long the session lives after its last edit or keepalive (the one home of the rule):
    TEST_PATTERN_IDLE_SECONDS while it asks a test pattern, so the pattern stays up while the
    operator's phone goes from the browser to its camera (owner, 2026-10-14: "It would be nice if
    the testing screen persisted, even for a minute"); PICTURE_IDLE_SECONDS otherwise. The Pi's own
    bound on a pattern (TEST_PATTERN_SECONDS) is unchanged."""
    return TEST_PATTERN_IDLE_SECONDS if session.test_pattern is not None else PICTURE_IDLE_SECONDS


def kept_alive(session: PictureSession, now: float) -> PictureSession:
    """The session after a keepalive at `now`: its lease touched (`idle_seconds`). PictureRefused
    session_ended once the lease ran out."""
    if not session.lease.live(now):
        raise PictureRefused("session_ended", 410)
    return _kept(session, now)


def _kept(session: PictureSession, now: float) -> PictureSession:
    return replace(session, lease=_touched(session, now))


def _touched(session: PictureSession, now: float) -> Lease:
    return touched(session.lease, now, idle_seconds=idle_seconds(session))


def _next(session: PictureSession, expected_sequence: int, now: float) -> int:
    """The session's next sequence; session_ended, then stale_sequence."""
    if not session.lease.live(now):
        raise PictureRefused("session_ended", 410)
    try:
        return next_sequence(session.sequence, expected_sequence)
    except StaleSequence:
        raise PictureRefused("stale_sequence", 409) from None


def _presents(document: PictureDocument | None, report: PictureReport | None) -> bool:
    """`report` answers `document` and the Player presented that change (or a later one)."""
    return (answers(document, report) and document is not None and report is not None
            and report.presented_change is not None and report.presented_change >= document.picture_change)


def _in_list_order(hardware: Mapping[VcpCode, int]) -> dict[VcpCode, int]:
    return {code: hardware[code] for code in PICTURE_LIST if code in hardware}


__all__ = ["PICTURE_IDLE_SECONDS", "TEST_PATTERN_IDLE_SECONDS", "TEST_PATTERN_SECONDS", "DisplayPicture", "PictureRefused", "PictureSession",
           "PictureRestored", "PictureStatus", "SoftwareSetting", "SoftwareTouch",
           "TestPatternState", "answers",
           "carries", "copied", "default_picture", "edited_hardware", "edited_light", "edited_signal_range",
           "edited_software", "edited_test_pattern", "software_touch",
           "effective_picture", "hardware_answered", "idle_seconds", "kept_alive", "kept_picture", "picture_digest", "picture_document",
           "picture_status", "presented", "restore_answered", "restore_of", "restored", "reverted",
           "session_test_pattern",
           "signal_answered", "test_pattern_state", "with_befores", "with_restore"]
