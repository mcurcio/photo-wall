"""House times for tests that build a Runtime or a Program (Power and the week): UTC keeps a test's
instants plain; Europe/London is the house the acceptance tests use (daylight saving)."""
from __future__ import annotations

from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

from central.house_time.model import ClockAt, HouseTime, When

UTC_HOUSE: Final = HouseTime(zone="UTC")
LONDON: Final = HouseTime(zone="Europe/London")
EVERY_DAY: Final = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def daily(start: str, end: str, days: tuple[str, ...] = EVERY_DAY) -> When:
    """A When of clock times, "HH:MM" each."""
    return When(days=days, start=ClockAt(time=start), end=ClockAt(time=end))


def at(house: HouseTime, text: str) -> float:
    """The UTC instant of house time `text` ("2026-10-12T17:00", fold 0)."""
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(house.zone)).timestamp()


def clock_time(seconds: float) -> str:
    """The UTC house clock's "HH:MM" at `seconds` (a whole minute)."""
    if seconds % 60:
        raise ValueError(f"{seconds} is not on a minute")
    minutes = int(seconds // 60) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def window_program(program_id: str, scene_id: str, start: float, end: float, *, priority: str = "normal",
                   name: str | None = None):
    """A Program "every day, from `start` to `end`" on the UTC house (both on a minute): its first
    window is [start, end) for a test clock near the epoch. Its name is its id unless given."""
    from central.runtime import Program
    return Program(program_id=program_id, name=program_id if name is None else name, scene_id=scene_id,
                   when=daily(clock_time(start), clock_time(end)), priority=priority)


def set_house(db, house: HouseTime) -> None:
    """Store Settings › House as `house`, its zone confirmed as an operator's save confirms it (a DB
    test's house; with no row Central would read its own server zone, which differs between a laptop
    and CI, and Night off would switch nothing)."""
    place = house.place
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO house_settings (singleton, zone, latitude, longitude, units, clock_format, updated_at, "
            "zone_confirmed) VALUES (TRUE, %s, %s, %s, 'cm', '24h', 0, TRUE) ON CONFLICT (singleton) DO UPDATE SET "
            "zone=EXCLUDED.zone, latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude, zone_confirmed=TRUE",
            (house.zone, None if place is None else place.latitude, None if place is None else place.longitude))
