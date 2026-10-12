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

The one calculator of windows on the house clock (Schedules, Night off, the power timetable, the
week). Two other rules read the house calendar and keep their own homes: a Source's relative
dates ("last 3 months", media/pool_check.py `window_of`) and On this day (the ranked line's SQL
`AT TIME ZONE` the house zone, central/infra/photo_library_store.py).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astral import Observer
from astral.sun import sunrise, sunset

from central.house_time.model import (
    WEEKDAYS,
    ClockAt,
    HouseTime,
    LocalDateTime,
    TimeOfDay,
    When,
    WhenProblem,
    Window,
)

LOOKAHEAD_DAYS: Final = 8      # the furthest any search looks past its starting point

_DAY: Final = timedelta(days=1)
_MIDNIGHT: Final = ClockAt(time="00:00")


def known_zone(zone: str) -> bool:
    """Whether the IANA database (zoneinfo over tzdata) knows `zone`."""
    if not isinstance(zone, str) or not zone or zone.startswith("/"):
        return False
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def when_problems(when: When, house: HouseTime) -> tuple[WhenProblem, ...]:
    """Why `when` cannot be saved, in WhenProblem order: "no-days" when `days` is empty,
    "needs-place" when its start or end is a sun time and `house.place` is None. Empty: none."""
    problems: list[WhenProblem] = []
    if not when.days:
        problems.append("no-days")
    if house.place is None and (when.start.at != "clock" or when.end.at != "clock"):
        problems.append("needs-place")
    return tuple(problems)


def house_date(house: HouseTime, at: float) -> date:
    """The house's calendar date at UTC instant `at`."""
    return datetime.fromtimestamp(at, _zone(house)).date()


def day_start(house: HouseTime, day: date) -> float:
    """The UTC instant house day `day` begins (its first existing local moment)."""
    start = moment(_MIDNIGHT, house, day)
    assert start is not None     # a clock time always names a moment
    return start


def local_date_time(house: HouseTime, at: float) -> LocalDateTime:
    """`at` on the house clock, to the minute: "2026-10-12T07:00" (display only, never parsed back
    into an instant)."""
    return datetime.fromtimestamp(at, _zone(house)).strftime("%Y-%m-%dT%H:%M")


def moment(time: TimeOfDay, house: HouseTime, day: date) -> float | None:
    """The UTC instant `time` names on house day `day` (the daylight-saving rule above); None for a
    sun time on a day with no sunrise or sunset. ValueError("needs-place") for a sun time when
    `house.place` is None."""
    zone = _zone(house)
    if isinstance(time, ClockAt):
        hours, minutes = (int(part) for part in time.time.split(":"))
        # fold=0: a repeated time is its first occurrence; a skipped time takes the offset before
        # the change, which is the same instant as the local time moved forward by the gap.
        return datetime.combine(day, clock_time(hours, minutes, fold=0), tzinfo=zone).timestamp()
    if house.place is None:
        raise ValueError("needs-place")
    observer = Observer(latitude=house.place.latitude, longitude=house.place.longitude)
    try:
        sun = (sunrise if time.at == "sunrise" else sunset)(observer, day, tzinfo=zone)
    except ValueError:           # astral: the sun never rises or never sets on that day
        return None
    return float(round(sun.timestamp()) + 60 * time.offset_minutes)


def windows(when: When, house: HouseTime, start: float, end: float) -> tuple[Window, ...]:
    """Every window of `when` that overlaps [start, end), in start order; a window that began
    before `start` is included whole. ValueError("no-days" | "needs-place") when `when_problems`
    names any. start < end, else ValueError("range")."""
    if not start < end:
        raise ValueError("range")
    problems = when_problems(when, house)
    if problems:
        raise ValueError(problems[0])
    # A window lasts at most a day and a few hours (sun offsets), so one starting two house days
    # before `start` may still overlap it; one day past `end` is computed only to cut the last.
    first, last = house_date(house, start) - 2 * _DAY, house_date(house, end) + _DAY
    found: list[Window] = []
    day = first
    while day <= last:
        window = _window_on(when, house, day)
        if window is not None:
            found.append(window)
        day += _DAY
    cut = [replace(window, end=min(window.end, following.start))
           for window, following in zip(found, found[1:], strict=False)]
    if found:
        cut.append(found[-1])
    return tuple(window for window in cut if window.end > start and window.start < end)


def next_window(when: When, house: HouseTime, after: float) -> Window | None:
    """The first window of `when` whose start is strictly after `after`, looking at most
    LOOKAHEAD_DAYS house days ahead; None when there is none in that reach (every day in it has no
    sun time). ValueError as `windows`."""
    reach = day_start(house, house_date(house, after) + (LOOKAHEAD_DAYS + 1) * _DAY)
    return next((window for window in windows(when, house, after, reach) if window.start > after), None)


def window_at(when: When, house: HouseTime, at: float) -> Window | None:
    """The window of `when` with start <= at < end, or None. ValueError as `windows`."""
    return next((window for window in windows(when, house, at, at + 1) if window.start <= at < window.end), None)


def _zone(house: HouseTime) -> ZoneInfo:
    return ZoneInfo(house.zone)


def _window_on(when: When, house: HouseTime, day: date) -> Window | None:
    """The window starting on house day `day`, before the never-overlapping cut; None when `day` is
    not one of its days or a sun time it names does not happen."""
    if WEEKDAYS[day.weekday()] not in when.days:
        return None
    start = moment(when.start, house, day)
    if start is None:
        return None
    end = moment(when.end, house, day)
    if end is not None and end <= start:      # crossing midnight: the next house day
        end = moment(when.end, house, day + _DAY)
    if end is None or end <= start:
        return None
    return Window(start=start, end=end)


__all__ = ["LOOKAHEAD_DAYS", "day_start", "house_date", "known_zone", "local_date_time", "moment",
           "next_window", "when_problems", "window_at", "windows"]
