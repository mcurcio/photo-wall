"""The power timetable, pure (Power and the week: slice T for Night off, slice S2 for holds).

Rules this module owns (one home each):
- **Spans:** a Frame's wanted power over any range, as consecutive spans, each with its reason:
  the newest live hold on the Frame (until its `ends_at`, or for good when until resumed), else
  Night off's windows when Night off is in force and the Frame follows it ("night-off", off), else
  on ("on-underneath").
- **Night off in force:** only once the operator has confirmed the house's zone (`night_off_in_force`):
  before that the zone is Central's own, which need not be the house's, and its 23:00 could fall
  in the house's afternoon.
  Adjacent spans with the same power and reason merge.
- **The horizon:** a timetable runs from Central's `now` to `horizon_end`: the start of the house
  day HORIZON_DAYS + 1 days after now's house day, so it moves once a house day, at midnight, and
  always covers at least HORIZON_DAYS days.
- **The timetable:** `now` is the wanted power at Central's now; `changes` are the span starts in
  (now, until) where the power changes (a reason change with the same power is no change).
- **A hold's end, per Frame** (owner, 2026-10-12: "Each Frame's hold ends at its own next change; a
  Frame with no Night off holds until 07:00 tomorrow"): for a Frame that follows Night off, Night
  off's next on or off time after `now`; for one that does not, the next time of day Night off turns
  displays on (07:00 by default) after `now` on the house clock; the horizon's end while Night off
  is not in force or has no such time in the look-ahead; None when holds last until resumed.
- **The hold in force:** the newest live hold on the Frame (`hold_in_force`).
The 1b console test is not here: it sits on top of the timetable in the Output document
(central/displays/model.py), counted on the Pi.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from uuid import UUID

from central.house_time.calculator import LOOKAHEAD_DAYS, day_start, house_date, windows
from central.house_time.calculator import moment as house_moment
from central.house_time.model import HouseTime, Window
from central.power.model import (
    AllFrames,
    Hold,
    HoldSource,
    NightOff,
    PowerBecause,
    PowerPolicy,
)
from contracts.node_output import MAX_POWER_CHANGES, Darkness, Power

HORIZON_DAYS: Final = 7       # the frame's "next 7 days" (owner, 2026-10-12: "Yes, send 7 days of set times")


@dataclass(frozen=True, slots=True)
class PowerSpan:
    """[start, end) in UTC seconds with one wanted power and its reason. `hold_id` and `source` name
    the hold when `because` is "hold", else None."""
    start: float
    end: float
    power: Power
    because: PowerBecause
    hold_id: UUID | None = None
    source: HoldSource | None = None


@dataclass(frozen=True, slots=True)
class TimedChange:
    """A set time of the timetable in UTC seconds, the power it brings and why."""
    at: float
    power: Power
    because: PowerBecause


@dataclass(frozen=True, slots=True)
class Timetable:
    """One Frame's timetable at Central's `now` (the Output document's power part but the test)."""
    now: Power
    because: PowerBecause
    changes: tuple[TimedChange, ...]
    until: float
    darkness: Darkness

    def wanted_at(self, at: float) -> tuple[Power, PowerBecause]:
        """The power and reason in force at `at` by this timetable (the last change at or before
        `at`, else `now`/`because`); past `until` the last one in force."""
        in_force = [change for change in self.changes if change.at <= at]
        if in_force:
            return in_force[-1].power, in_force[-1].because
        return self.now, self.because

    def next_change_after(self, at: float) -> TimedChange | None:
        """The first change strictly after `at`, or None."""
        return next((change for change in self.changes if change.at > at), None)


def horizon_end(house: HouseTime, now: float) -> float:
    """The start of house day (house_date(now) + HORIZON_DAYS + 1), in UTC seconds."""
    return day_start(house, house_date(house, now) + timedelta(days=HORIZON_DAYS + 1))


def night_off_in_force(policy: PowerPolicy) -> NightOff | None:
    """Night off when it switches displays (the house's zone is confirmed), else None."""
    return policy.night_off if policy.zone_confirmed else None


def follows_night_off(frame_id: str, night_off: NightOff) -> bool:
    """Whether `frame_id` follows Night off: every Frame under AllFrames, else the chosen ones."""
    return isinstance(night_off.frames, AllFrames) or frame_id in night_off.frames.frame_ids


def spans(frame_id: str, policy: PowerPolicy, house: HouseTime, start: float, end: float) -> tuple[PowerSpan, ...]:
    """`frame_id`'s wanted power over [start, end) as consecutive spans (the rule above), the first
    starting at `start` and the last ending at `end`. ValueError("range") unless start < end."""
    if not start < end:
        raise ValueError("range")
    holds = tuple(hold for hold in policy.holds if frame_id in hold.frame_ids)
    night_off = night_off_in_force(policy)
    nights = _night_windows(night_off, house, start, end) if night_off is not None and follows_night_off(
        frame_id, night_off) else ()
    edges = {start, end}
    edges.update(moment for hold in holds for moment in (hold.started_at, hold.ends_at)
                 if moment is not None and start < moment < end)
    edges.update(moment for night in nights for moment in (night.start, night.end) if start < moment < end)
    cuts = sorted(edges)
    found: list[PowerSpan] = []
    for left, right in zip(cuts, cuts[1:], strict=False):
        piece = _span_at(frame_id, policy, left, right, nights)
        previous = found[-1] if found else None
        if previous is not None and (previous.power, previous.because, previous.hold_id, previous.source) == (
                piece.power, piece.because, piece.hold_id, piece.source):
            found[-1] = PowerSpan(previous.start, right, piece.power, piece.because, piece.hold_id, piece.source)
        else:
            found.append(piece)
    return tuple(found)


def frame_timetable(frame_id: str, policy: PowerPolicy, house: HouseTime, now: float) -> Timetable:
    """`frame_id`'s timetable at Central's `now`: `spans` over [now, horizon_end), folded into
    `now`, `because` and the changes; `darkness` is Night off's."""
    until = horizon_end(house, now)
    found = spans(frame_id, policy, house, now, until)
    changes: list[TimedChange] = []
    power = found[0].power
    for span in found[1:]:
        if span.power is not power:
            # Set times are whole seconds on the wire; a hold's end on Central's clock rounds up.
            changes.append(TimedChange(at=float(math.ceil(span.start)), power=span.power, because=span.because))
            power = span.power
    if len(changes) > MAX_POWER_CHANGES:      # the timetable then ends at its first change left out
        until = changes[MAX_POWER_CHANGES].at
        changes = changes[:MAX_POWER_CHANGES]
    return Timetable(now=found[0].power, because=found[0].because, changes=tuple(changes), until=until,
                     darkness=policy.night_off.darkness)


def unbound_timetable(house: HouseTime, now: float) -> Timetable:
    """The timetable of an Output no Frame is bound to: on, no changes, until horizon_end,
    Darkness.DISPLAY_OFF."""
    return Timetable(now=Power.ON, because="on-underneath", changes=(), until=horizon_end(house, now),
                     darkness=Darkness.DISPLAY_OFF)


def hold_end(frame_id: str, policy: PowerPolicy, house: HouseTime, now: float) -> float | None:
    """When a hold pressed at Central's `now` ends on `frame_id` (the rule above): None when
    `policy.hold_lasts` is "until-resumed"; `horizon_end(house, now)` when there is no such time
    within the calculator's look-ahead (nothing stays held forever by accident)."""
    if policy.hold_lasts == "until-resumed":
        return None
    night_off = night_off_in_force(policy)
    if night_off is None:
        return horizon_end(house, now)
    reach = day_start(house, house_date(house, now) + timedelta(days=LOOKAHEAD_DAYS + 1))
    if follows_night_off(frame_id, night_off):
        moments = [edge for night in _night_windows(night_off, house, now, reach)
                   for edge in (night.start, night.end) if edge > now]
    else:
        moments = _times_of_day(night_off, house, now)
    return min(moments) if moments else horizon_end(house, now)


def hold_in_force(frame_id: str, policy: PowerPolicy, at: float) -> Hold | None:
    """The newest hold on `frame_id` live at `at` (started at or before it, not yet ended), or None."""
    live = [hold for hold in policy.holds if frame_id in hold.frame_ids and hold.started_at <= at
            and (hold.ends_at is None or at < hold.ends_at)]
    return max(live, key=lambda hold: hold.started_at, default=None)


def _times_of_day(night_off: NightOff, house: HouseTime, now: float) -> list[float]:
    """Night off's on time on each house day from now's to the look-ahead's end, after `now`
    (every day, whatever nights Night off starts on); none while it cannot be worked out (a sun
    time with no Place)."""
    first = house_date(house, now)
    try:
        found = (house_moment(night_off.on, house, first + timedelta(days=offset)) for offset in range(LOOKAHEAD_DAYS + 1))
        return [at for at in found if at is not None and at > now]
    except ValueError:
        return []


def _night_windows(night_off: NightOff, house: HouseTime, start: float, end: float) -> tuple[Window, ...]:
    """Night off's windows over [start, end); none while it cannot be worked out (no days, or a sun
    time with no Place since the house's Place was cleared)."""
    try:
        return windows(night_off.when, house, start, end)
    except ValueError:
        return ()


def _span_at(frame_id: str, policy: PowerPolicy, left: float, right: float,
             nights: tuple[Window, ...]) -> PowerSpan:
    """The span [left, right), which no hold or night edge crosses, by its state at `left`."""
    held = hold_in_force(frame_id, policy, left)
    if held is not None:
        return PowerSpan(left, right, held.power, "hold", held.hold_id, held.source)
    if any(night.start <= left < night.end for night in nights):
        return PowerSpan(left, right, Power.OFF, "night-off")
    return PowerSpan(left, right, Power.ON, "on-underneath")


__all__ = ["HORIZON_DAYS", "PowerSpan", "TimedChange", "Timetable", "follows_night_off", "frame_timetable",
           "hold_end", "hold_in_force", "horizon_end", "night_off_in_force", "spans", "unbound_timetable"]
