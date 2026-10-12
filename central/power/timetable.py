"""The power timetable, pure (Power and the week: slice T for Night off, slice S2 for holds).

Rules this module owns (one home each):
- **Spans:** a Frame's wanted power over any range, as consecutive spans, each with its reason:
  the newest live hold on the Frame (until its `ends_at`, or for good when until resumed), else
  Night off's windows when the Frame follows it ("night-off", off), else on ("on-underneath").
  Adjacent spans with the same power and reason merge.
- **The horizon:** a timetable runs from Central's `now` to `horizon_end`: the start of the house
  day HORIZON_DAYS + 1 days after now's house day, so it moves once a house day, at midnight, and
  always covers at least HORIZON_DAYS days.
- **The timetable:** `now` is the wanted power at Central's now; `changes` are the span starts in
  (now, until) where the power changes (a reason change with the same power is no change).
- **A hold's end:** Night off's next on or off time after `now` (house-wide: the same moment for a
  Frame that does not follow Night off), or None when holds last until resumed.
The 1b console test is not here: it sits on top of the timetable in the Output document
(central/displays/model.py), counted on the Pi.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from central.house_time.model import HouseTime
from central.power.model import HoldLasts, HoldSource, NightOff, PowerBecause, PowerPolicy
from contracts.node_output import Darkness, Power

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
        raise NotImplementedError

    def next_change_after(self, at: float) -> TimedChange | None:
        """The first change strictly after `at`, or None."""
        raise NotImplementedError


def horizon_end(house: HouseTime, now: float) -> float:
    """The start of house day (house_date(now) + HORIZON_DAYS + 1), in UTC seconds."""
    raise NotImplementedError


def follows_night_off(frame_id: str, night_off: NightOff) -> bool:
    """Whether `frame_id` follows Night off: every Frame under AllFrames, else the chosen ones."""
    raise NotImplementedError


def spans(frame_id: str, policy: PowerPolicy, house: HouseTime, start: float, end: float) -> tuple[PowerSpan, ...]:
    """`frame_id`'s wanted power over [start, end) as consecutive spans (the rule above), the first
    starting at `start` and the last ending at `end`. ValueError("range") unless start < end."""
    raise NotImplementedError


def frame_timetable(frame_id: str, policy: PowerPolicy, house: HouseTime, now: float) -> Timetable:
    """`frame_id`'s timetable at Central's `now`: `spans` over [now, horizon_end), folded into
    `now`, `because` and the changes; `darkness` is Night off's."""
    raise NotImplementedError


def unbound_timetable(house: HouseTime, now: float) -> Timetable:
    """The timetable of an Output no Frame is bound to: on, no changes, until horizon_end,
    Darkness.DISPLAY_OFF."""
    raise NotImplementedError


def hold_end(night_off: NightOff, house: HouseTime, now: float, lasts: HoldLasts) -> float | None:
    """When a hold pressed at Central's `now` ends: None when `lasts` is "until-resumed"; else
    Night off's next on or off time after `now`, or `horizon_end(house, now)` when Night off has
    none within the calculator's look-ahead (nothing stays held forever by accident)."""
    raise NotImplementedError


__all__ = ["HORIZON_DAYS", "PowerSpan", "TimedChange", "Timetable", "follows_night_off", "frame_timetable",
           "hold_end", "horizon_end", "spans", "unbound_timetable"]
