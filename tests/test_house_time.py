"""House time, the one calculator of every time of day (Power and the week, slice T, A1;
central/house_time/calculator.py).

Europe/London throughout: the spring change skips 01:00-02:00 on 2027-03-28, the autumn change
repeats 01:00-02:00 on 2026-10-25. Every assertion reads the answer on the house clock.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from astral import Observer
from astral.sun import sunset
from pydantic import ValidationError

from central.house_time.calculator import (
    day_start,
    house_date,
    known_zone,
    local_date_time,
    moment,
    next_window,
    when_problems,
    window_at,
    windows,
)
from central.house_time.model import ClockAt, HouseTime, Place, SunAt, When

LONDON = HouseTime(zone="Europe/London")
PLACE = Place(latitude=51.4769, longitude=-0.0005)
LONDON_PLACED = HouseTime(zone="Europe/London", place=PLACE)
EVERY_DAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _at(text: str, zone: str = "Europe/London") -> float:
    """A house-clock time (no ambiguity in the moments used here) as UTC seconds."""
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(zone)).timestamp()


def _clock(start: str, end: str, days=EVERY_DAY) -> When:
    return When(days=days, start=ClockAt(time=start), end=ClockAt(time=end))


def test_a_time_the_spring_change_skips_moves_forward_by_the_gap():
    start = moment(ClockAt(time="01:30"), LONDON, date(2027, 3, 28))
    assert local_date_time(LONDON, start) == "2027-03-28T02:30"
    assert datetime.fromtimestamp(start, UTC).strftime("%H:%M%z") == "01:30+0000"


def test_a_time_the_autumn_change_repeats_takes_its_first_occurrence():
    first = moment(ClockAt(time="01:30"), LONDON, date(2026, 10, 25))
    # 01:30 BST is 00:30 UTC; the second 01:30 (GMT) would be 01:30 UTC.
    assert datetime.fromtimestamp(first, UTC).strftime("%H:%M") == "00:30"


def test_a_window_across_both_changes_keeps_its_house_clock_times():
    when = _clock("01:30", "03:00", days=("sun",))
    spring = window_at(when, LONDON, _at("2027-03-28T02:45"))
    assert spring is not None
    assert (local_date_time(LONDON, spring.start), local_date_time(LONDON, spring.end)) == (
        "2027-03-28T02:30", "2027-03-28T03:00")
    autumn = next_window(when, LONDON, _at("2026-10-24T12:00"))
    assert autumn is not None
    assert datetime.fromtimestamp(autumn.start, UTC).strftime("%Y-%m-%d %H:%M") == "2026-10-25 00:30"
    assert autumn.end - autumn.start == 2.5 * 3600      # the repeated hour is inside the window


def test_an_end_before_the_start_is_the_next_house_day():
    when = _clock("22:00", "02:00", days=("fri",))
    found = windows(when, LONDON, _at("2026-10-12T00:00"), _at("2026-10-19T00:00"))
    assert [(local_date_time(LONDON, w.start), local_date_time(LONDON, w.end)) for w in found] == [
        ("2026-10-16T22:00", "2026-10-17T02:00")]
    # A window that began before the range is included whole.
    assert windows(when, LONDON, _at("2026-10-17T01:00"), _at("2026-10-17T03:00")) == tuple(found)


def test_an_end_equal_to_the_start_lasts_a_whole_day():
    when = _clock("09:00", "09:00", days=("mon",))
    window = next_window(when, LONDON, _at("2026-10-11T12:00"))
    assert (local_date_time(LONDON, window.start), local_date_time(LONDON, window.end)) == (
        "2026-10-12T09:00", "2026-10-13T09:00")


def test_weekdays_only():
    when = _clock("17:00", "23:00", days=("mon", "tue", "wed", "thu", "fri"))
    found = windows(when, LONDON, _at("2026-10-12T00:00"), _at("2026-10-19T00:00"))
    assert [local_date_time(LONDON, w.start) for w in found] == [
        f"2026-10-{day}T17:00" for day in (12, 13, 14, 15, 16)]


def test_sunset_minus_thirty_at_the_place_matches_astral():
    when = When(days=EVERY_DAY, start=SunAt(at="sunset", offset_minutes=-30), end=ClockAt(time="23:00"))
    window = next_window(when, LONDON_PLACED, _at("2026-10-12T00:00"))
    expected = sunset(Observer(latitude=PLACE.latitude, longitude=PLACE.longitude), date(2026, 10, 12),
                      tzinfo=ZoneInfo("Europe/London")).timestamp() - 1800
    assert window.start == round(expected)
    assert window.start == int(window.start)        # whole seconds: every set time encodes exactly


def test_a_polar_day_has_no_window():
    svalbard = HouseTime(zone="Arctic/Longyearbyen", place=Place(latitude=78.22, longitude=15.65))
    when = When(days=EVERY_DAY, start=SunAt(at="sunset"), end=ClockAt(time="23:00"))
    assert moment(SunAt(at="sunset"), svalbard, date(2026, 6, 21)) is None
    midsummer = _at("2026-06-18T12:00", "Arctic/Longyearbyen")
    assert windows(when, svalbard, midsummer, midsummer + 7 * 86400) == ()
    assert next_window(when, svalbard, midsummer) is None


def test_windows_never_overlap():
    placed = LONDON_PLACED
    when = When(days=EVERY_DAY, start=SunAt(at="sunset"), end=SunAt(at="sunset", offset_minutes=-1))
    found = windows(when, placed, _at("2026-10-12T00:00"), _at("2026-10-19T00:00"))
    assert all(left.end <= right.start for left, right in zip(found, found[1:], strict=False))
    assert all(window.start < window.end for window in found)


def test_when_problems_in_order():
    sun = When(days=(), start=SunAt(at="sunrise"), end=ClockAt(time="09:00"))
    assert when_problems(sun, LONDON) == ("no-days", "needs-place")
    assert when_problems(sun, LONDON_PLACED) == ("no-days",)
    assert when_problems(_clock("09:00", "10:00"), LONDON) == ()
    with pytest.raises(ValueError, match="^no-days$"):
        windows(sun, LONDON, 0, 1)
    with pytest.raises(ValueError, match="^needs-place$"):
        moment(SunAt(at="sunrise"), LONDON, date(2026, 10, 12))


def test_house_dates_and_day_starts_are_on_the_house_clock():
    assert house_date(LONDON, _at("2026-10-12T00:30")) == date(2026, 10, 12)
    assert local_date_time(LONDON, day_start(LONDON, date(2026, 10, 25))) == "2026-10-25T00:00"
    # Havana's spring change skips midnight: the day begins at its first existing moment.
    havana = HouseTime(zone="America/Havana")
    assert local_date_time(havana, day_start(havana, date(2027, 3, 14))) == "2027-03-14T01:00"


def test_the_vocabulary_refuses_what_it_cannot_mean():
    assert known_zone("Europe/London") and not known_zone("Mars/Olympus") and not known_zone("/etc/passwd")
    with pytest.raises(ValidationError, match="unknown-zone"):
        HouseTime(zone="Mars/Olympus")
    with pytest.raises(ValidationError, match="days"):
        When(days=("tue", "mon"), start=ClockAt(time="09:00"), end=ClockAt(time="10:00"))
    with pytest.raises(ValidationError):
        ClockAt(time="24:00")
    with pytest.raises(ValidationError):
        SunAt(at="sunset", offset_minutes=181)
