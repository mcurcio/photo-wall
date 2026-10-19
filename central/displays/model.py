"""The Display rules, pure (roadmap 1b; run ledger .claude/runs/display-1b.md, slices C1-C3).

One home each for:
- **Same display** (C1; design rule 1; owner q2, 2026-10-10). A serial counts only when it is
  present and no other Output reports the same maker, product and serial at the same time (an
  Output of this Node, or of a Node Central's hub holds now; then it is recorded as shared for
  that maker and product, for good, and every Output holding its serial-keyed Display is keyed
  again without it, the bound Frame's Position following: not a display change). A display with a usable serial
  is keyed (maker, product, serial) across the wall: its settings follow it anywhere. A display
  with none is keyed (maker, product, Frame) of the Frame bound to the Output it is seen on: it
  stays the same Display through a Pi swap or a port change, and on another Frame it starts over.
  On an unbound Output it is pending (no Display) until the Output is bound. Two identical
  no-serial monitors on one Pi's two ports, bound to two Frames, are two Displays. An unplug, a
  blip or an unreadable identity (None) never changes the Display recorded on an Output; only a
  readable identity with another key does.
- **Ready** (C2; design rule 3, G5). A Frame is ready when it is bound, its latest Position
  commit was made at its current generation (bind, unbind, a profile change and a turn raise the
  generation: a Position made for the other hanging needs checking), and that commit names the Display last seen on its Output, or no Display has ever
  been seen there. A Display first seen on an Output (none before) is adopted by the Frame bound
  there when its Position commit names none: a Frame committed before 1b, or against a display
  with no EDID, is not made "display changed" by the first report.
- **The Output document** (C1, C3; roadmap 2a). Its power part is the Frame's power timetable
  (central/power/timetable.py: Central's decision now, the set times it changes at, `until`, the
  darkness), with the Frame's live console test on top; method and switches come from the Display
  last seen on the Output (defaults when none); its turn is the bound Frame's (frames.turn), 0 for
  an unbound Output, so `change` rises for a turn as for power. The console's one test is Test: turn off. It
  lasts TEST_SECONDS, "or less if you press Turn on. When the test ends, the request underneath
  takes over" (owner q1, 2026-10-10): Turn on ends the live test, so the timetable is underneath
  again. The Pi counts a test on its own clock, and Central drops it from the document at its own
  `ends_at` (Central's clock, compared only with itself) or at Turn on, whichever comes first.
  No reading of Central's clock is in the document: its set times are calendar times, so its
  digest moves only at a set time, a house midnight or an edit.
- **Power status** (C3): what the Power tab says about the latest test, and the Pi's result for
  what Central has in force (`power_result`): the live test, else the timetable.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeGuard
from uuid import UUID

from central.power.timetable import Timetable
from contracts.display_turn import DisplayTurn
from contracts.frame_fit import OutputSize
from contracts.node_output import (
    BEST_DETECTED,
    DisplayIdentity,
    DisplayMode,
    InForceKind,
    OutputDocument,
    OutputPower,
    OutputReport,
    Power,
    PowerChange,
    PowerMethod,
    PowerRequest,
    PowerResult,
    encode_output_document,
    encode_set_time,
)

TEST_SECONDS: Final = 300                    # owner q1, 2026-10-10
_DIGEST_CHANGE: Final = 1                    # any fixed change number: the digest does not cover it


class Readiness(StrEnum):
    """Worked out, never stored. The order is the console's: the first that holds is shown."""
    UNBOUND = "unbound"                       # no Pi and HDMI port feeds this Frame
    DISPLAY_CHANGED = "display-changed"       # the Display on its Output is not the one its Position was set against
    POSITION_NEEDED = "position-needed"       # no Position commit since the last bind or profile change
    READY = "ready"


class PowerStatus(StrEnum):
    """The Power tab's state of the latest console test."""
    UNBOUND = "unbound"       # no Output: nothing can be tested
    IDLE = "idle"             # no live test
    WAITING = "waiting"       # a live test the Pi has not answered yet (its report names an older change)
    ANSWERED = "answered"     # the Pi's report answers the document change carrying this test


@dataclass(frozen=True)
class DisplayKey:
    """A Display's key: exactly one of `serial` (usable serial) and `frame_id` (no usable serial)."""
    maker: str
    product: int
    serial: str | None
    frame_id: str | None

    def __post_init__(self) -> None:
        """ValueError("display_key") unless exactly one of serial and frame_id is set."""
        if (self.serial is None) == (self.frame_id is None):
            raise ValueError("display_key")


@dataclass(frozen=True)
class Sighting:
    """An identity reported on one Output, now."""
    player_id: str
    output_id: str
    identity: DisplayIdentity


def serial_usable(sighting: Sighting, others: Collection[Sighting],
                  shared: Collection[tuple[str, int, str]]) -> bool:
    """Whether `sighting`'s serial counts: present, not in `shared` ((maker, product, serial)
    recorded as shared), and reported by no other Output in `others` (every other Output's current
    identity, Pi-wide and wall-wide)."""
    identity = sighting.identity
    if identity.serial is None or (identity.maker, identity.product, identity.serial) in shared:
        return False
    here = (sighting.player_id, sighting.output_id)
    return not any((other.player_id, other.output_id) != here
                   and (other.identity.maker, other.identity.product, other.identity.serial)
                   == (identity.maker, identity.product, identity.serial) for other in others)


def display_key(identity: DisplayIdentity, *, serial_counts: bool, bound_frame_id: str | None) -> DisplayKey | None:
    """The key `identity` is recognised by; None = pending (no usable serial, Output unbound)."""
    if serial_counts:
        return DisplayKey(identity.maker, identity.product, identity.serial, None)
    if bound_frame_id is None:
        return None
    return DisplayKey(identity.maker, identity.product, None, bound_frame_id)


@dataclass(frozen=True)
class FramePosition:
    """The facts readiness is worked out from (frames, bindings, output_displays)."""
    bound: bool
    generation: int
    position_generation: int | None          # the Frame generation its latest Position commit was made at
    position_display_id: UUID | None         # the Display that commit named (None: none was seen)
    seen_display_id: UUID | None             # the Display last seen on its bound Output (None: never)


def readiness(position: FramePosition) -> Readiness:
    """UNBOUND, then DISPLAY_CHANGED (a Display is seen and the commit names another), then
    POSITION_NEEDED (position_generation != generation), else READY. With no Position commit there
    is no Display it could name, so a Frame never positioned needs Position, not a display check."""
    if not position.bound:
        return Readiness.UNBOUND
    if (position.seen_display_id is not None and position.position_generation is not None
            and position.position_display_id != position.seen_display_id):
        return Readiness.DISPLAY_CHANGED
    if position.position_generation != position.generation:
        return Readiness.POSITION_NEEDED
    return Readiness.READY


def screen_px(reported: OutputSize | None, modes: Sequence[DisplayMode]) -> OutputSize | None:
    """The mode as built of a Frame's screen (frame box run, .claude/runs/frame-box/page.md):
    `reported` (its Pi's report of the bound Output, outputs.observation width_px x height_px) when
    both sides are >= 1, else the first preferred mode of `modes` (the Display seen on that Output,
    displays.modes; Weston, given no mode, runs the preferred one), else None. The one home of a
    Frame's screen size: central/infra/display_store.py `frame_outputs_in` and the inventory's
    `FrameInventory.screen_px` ask here (migration 102 and 104 state the same rule in SQL). Pure."""
    ...


def adopts(position: FramePosition, seen_before: UUID | None, seen_now: UUID) -> bool:
    """Whether the bound Frame takes `seen_now` as its Position's Display when it is recorded on its
    Output: only on a first sighting (seen_before None) and only when the commit names none."""
    return position.bound and seen_before is None and position.position_display_id is None


@dataclass(frozen=True)
class DisplaySettings:
    """The Display's power settings (#116-#118), as stored; power_method None = best detected."""
    power_method: PowerMethod | None
    switch_input_on_power_on: bool
    never_off_on_other_input: bool


DEFAULT_SETTINGS: Final = DisplaySettings(None, True, True)


@dataclass(frozen=True)
class PowerTest:
    """A Frame's live console test (power_requests row). `ends_at` is Central's clock."""
    request_id: UUID
    power: Power
    for_seconds: int
    ends_at: float


@dataclass(frozen=True)
class ProjectedOutput:
    """One Output's document, all but its change number: the power part and the turn (picture rides
    its own key with its own change number, central/displays/picture.py; decision 0022), so a
    picture edit never raises `change` and never re-runs power."""
    output_id: str
    power: OutputPower
    settings: DisplaySettings
    turn: DisplayTurn

    def digest(self) -> str:
        """sha256 hex of the canonical document but its change number; the projector raises `change`
        when it differs."""
        part = json.loads(encode_output_document(self.at_change(_DIGEST_CHANGE)))
        del part["change"]
        return _sha256(part)

    def at_change(self, change: int) -> OutputDocument:
        settings = self.settings
        return OutputDocument(self.output_id, change, self.power,
                              BEST_DETECTED if settings.power_method is None else settings.power_method,
                              settings.switch_input_on_power_on, settings.never_off_on_other_input,
                              self.turn)


def _sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def output_power(timetable: Timetable, test: PowerTest | None, now: float) -> OutputPower:
    """The document's power part at Central's `now`: `now`, the changes (set times via
    `encode_set_time`), `until` and the darkness from `timetable`; `test` on top while it is live."""
    live = live_test(test, now)
    return OutputPower(
        now=timetable.now,
        changes=tuple(PowerChange(encode_set_time(_whole(change.at)), change.power) for change in timetable.changes),
        until=encode_set_time(_whole(timetable.until)), darkness=timetable.darkness,
        test=None if live is None else PowerRequest(str(live.request_id), live.power, live.for_seconds))


def project_output(output_id: str, *, timetable: Timetable, test: PowerTest | None,
                   settings: DisplaySettings | None, turn: DisplayTurn, now: float) -> ProjectedOutput:
    """The Output's document body at Central's `now`: its power part (`output_power`); `settings`
    or DEFAULT_SETTINGS; `turn`, the bound Frame's (0 unbound)."""
    return ProjectedOutput(output_id, output_power(timetable, test, now),
                           DEFAULT_SETTINGS if settings is None else settings, turn)


def power_status(*, bound: bool, test: PowerTest | None, document: OutputDocument | None,
                 report: OutputReport | None, now: float) -> PowerStatus:
    """UNBOUND without an Output; IDLE without a live test; ANSWERED when the report's for_change is
    the document's change and that document's test is the live test; else WAITING."""
    if not bound:
        return PowerStatus.UNBOUND
    if not _live(test, now):
        return PowerStatus.IDLE
    if (document is not None and report is not None and report.for_change == document.change
            and document.power.test is not None and document.power.test.request_id == str(test.request_id)):
        return PowerStatus.ANSWERED
    return PowerStatus.WAITING


def power_result(*, test: PowerTest | None, document: OutputDocument | None, report: OutputReport | None,
                 now: float) -> PowerResult | None:
    """The Pi's result for what Central has in force: set only when the report answers the latest
    document and its in-force is the live test (kind TEST, the same id), or, with no live test, the
    timetable (kind TIMETABLE); None while the Pi has not caught up."""
    if document is None or report is None or report.in_force is None or report.for_change != document.change:
        return None
    in_force = report.in_force
    if _live(test, now):
        answers = in_force.kind is InForceKind.TEST and in_force.request_id == str(test.request_id)
    else:
        answers = in_force.kind is InForceKind.TIMETABLE
    return report.result if answers else None


def live_test(test: PowerTest | None, now: float) -> PowerTest | None:
    """`test` while it is still on the stack at Central's `now`; None once it ended (or none)."""
    return test if _live(test, now) else None


def remaining_seconds(test: PowerTest, now: float) -> int:
    """Whole seconds until `test` ends at Central's `now` (rounded up, never below 0): the Power
    tab's count, on Central's clock only."""
    return max(0, math.ceil(test.ends_at - now))


def _live(test: PowerTest | None, now: float) -> TypeGuard[PowerTest]:
    """Whether `test` is still on the stack at Central's `now` (it ends at its own `ends_at`)."""
    return test is not None and now < test.ends_at


def _whole(seconds: float) -> int:
    """A set time's whole second (the timetable's are whole already; a float carries them)."""
    return math.ceil(seconds)
