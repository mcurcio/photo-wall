"""The one calculator of house time (Power and the week, slice T). Pure: `zoneinfo` (stdlib, with
the `tzdata` package as its database) and `astral` for sun times; Central only, never the Pi.

Rules this module owns (one home each):
- **Daylight saving** (iCalendar's): a clock time the spring change skips moves forward by the
  gap (01:30 on a 01:00->02:00 day is 02:30); a clock time the autumn change repeats takes its
  first occurrence (fold=0).
- **Crossing midnight**: an end at or before the start, on the start's house day, is on the next
  house day.
- **Sun times**: astral's sunrise or sunset at the Place on that house day, plus the offset; a day
  with none (polar day or night) has no window. astral's errors are caught here only.
- **Never overlapping**: one When's windows never overlap; a window that would overlap the next
  one is cut at the next one's start.
- **Problems**: `when_problems` is the one home of WhenProblem.
- **Look-ahead**: no search goes more than LOOKAHEAD_DAYS house days past its starting point.

Every function takes UTC seconds and returns UTC seconds or house-time strings; none reads a clock.
"""
from __future__ import annotations

from datetime import date
from typing import Final

from central.house_time.model import HouseTime, LocalDateTime, TimeOfDay, When, WhenProblem, Window

LOOKAHEAD_DAYS: Final = 8      # the furthest any search looks past its starting point


def known_zone(zone: str) -> bool:
    """Whether the IANA database (zoneinfo over tzdata) knows `zone`."""
    raise NotImplementedError


def when_problems(when: When, house: HouseTime) -> tuple[WhenProblem, ...]:
    """Why `when` cannot be saved, in WhenProblem order: "no-days" when `days` is empty,
    "needs-place" when its start or end is a sun time and `house.place` is None. Empty: none."""
    raise NotImplementedError


def house_date(house: HouseTime, at: float) -> date:
    """The house's calendar date at UTC instant `at`."""
    raise NotImplementedError


def day_start(house: HouseTime, day: date) -> float:
    """The UTC instant house day `day` begins (its first existing local moment)."""
    raise NotImplementedError


def local_date_time(house: HouseTime, at: float) -> LocalDateTime:
    """`at` on the house clock, to the minute: "2026-10-12T07:00" (display only, never parsed back
    into an instant)."""
    raise NotImplementedError


def moment(time: TimeOfDay, house: HouseTime, day: date) -> float | None:
    """The UTC instant `time` names on house day `day` (the daylight-saving rule above); None for a
    sun time on a day with no sunrise or sunset. ValueError("needs-place") for a sun time when
    `house.place` is None."""
    raise NotImplementedError


def windows(when: When, house: HouseTime, start: float, end: float) -> tuple[Window, ...]:
    """Every window of `when` that overlaps [start, end), in start order; a window that began
    before `start` is included whole. ValueError("no-days" | "needs-place") when `when_problems`
    names any. start < end, else ValueError("range")."""
    raise NotImplementedError


def next_window(when: When, house: HouseTime, after: float) -> Window | None:
    """The first window of `when` whose start is strictly after `after`, looking at most
    LOOKAHEAD_DAYS house days ahead; None when there is none in that reach (every day in it has no
    sun time). ValueError as `windows`."""
    raise NotImplementedError


def window_at(when: When, house: HouseTime, at: float) -> Window | None:
    """The window of `when` with start <= at < end, or None. ValueError as `windows`."""
    raise NotImplementedError


__all__ = ["LOOKAHEAD_DAYS", "day_start", "house_date", "known_zone", "local_date_time", "moment",
           "next_window", "when_problems", "window_at", "windows"]
