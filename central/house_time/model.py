"""House time's vocabulary (Power and the week, slice T; frame "House time and When").

Prior art: iCalendar (RFC 5545) weekly recurrence. A When is the weekly subset as structured data:
BYDAY is `days`, DTSTART's time of day is `start`, DTEND's is `end`; its daylight-saving rule is
the calculator's (central/house_time/calculator.py). Sun times are astral's.

Invariants each model's validator enforces:
- `When.days`: unique, in WEEKDAYS order (its canonical form); empty is allowed at parse time and
  is the problem "no-days" (`calculator.when_problems`, the one home of When problems).
- `HouseTime.zone`: a zone the IANA database knows (`calculator.known_zone`); else
  ValueError("unknown-zone").
- `ClockTime`: "HH:MM", 00:00..23:59.
- `SunAt.offset_minutes`: -180..180.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: Final[tuple[Weekday, ...]] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # date.weekday() order

IanaZone = Annotated[str, Field(min_length=1, max_length=64)]                    # "Europe/London"
ClockTime = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]     # "23:00", house clock
LocalDateTime = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}$")]  # house time, display only
OffsetMinutes = Annotated[int, Field(ge=-180, le=180)]

WhenProblem = Literal["no-days", "needs-place"]
Units = Literal["cm", "in"]                       # setting #4
ClockFormat = Literal["12h", "24h"]               # setting #5


class HouseModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Place(HouseModel):
    """Where the house is, for sunrise and sunset (setting #3)."""
    latitude: Annotated[float, Field(ge=-90, le=90)]
    longitude: Annotated[float, Field(ge=-180, le=180)]


class HouseTime(HouseModel):
    """The house's timezone (always set: seeded from Central's own zone, corrected in Settings ›
    House, #2) and location (None: sunrise and sunset cannot be chosen, #3)."""
    zone: IanaZone
    place: Place | None = None

    @field_validator("zone")
    @classmethod
    def _known(cls, zone: str) -> str:
        from central.house_time.calculator import known_zone  # the calculator imports this module
        if not known_zone(zone):
            raise ValueError("unknown-zone")
        return zone


class ClockAt(HouseModel):
    """A clock time on the house clock (#74)."""
    at: Literal["clock"] = "clock"
    time: ClockTime


class SunAt(HouseModel):
    """Sunrise or sunset at the house's Place, plus `offset_minutes` (#75, #81)."""
    at: Literal["sunrise", "sunset"]
    offset_minutes: OffsetMinutes = 0


TimeOfDay = Annotated[ClockAt | SunAt, Field(discriminator="at")]


class When(HouseModel):
    """A repeating weekly window: the days it starts on, its start and its end (#73-#76). An end at
    or before the start (on the start's day) is on the next day (#76: crossing midnight is shown,
    never asked). A day with no sunrise or sunset at the Place has no window."""
    days: tuple[Weekday, ...]
    start: TimeOfDay
    end: TimeOfDay

    @field_validator("days")
    @classmethod
    def _canonical(cls, days: tuple[Weekday, ...]) -> tuple[Weekday, ...]:
        return canonical_days(days)


def canonical_days(days: tuple[Weekday, ...]) -> tuple[Weekday, ...]:
    """`days` when unique and in WEEKDAYS order (a When's and Night off's canonical form), else
    ValueError("days"). Empty passes: it is the problem "no-days" (calculator.when_problems)."""
    if list(days) != sorted(set(days), key=WEEKDAYS.index):
        raise ValueError("days")
    return days


class HouseSettingsDraft(HouseModel):
    """Settings › House (#2-#5): what the operator saves."""
    time: HouseTime
    units: Units
    clock: ClockFormat


class HouseSettings(HouseSettingsDraft):
    """Settings › House as stored, with the moment it was last saved (Central's clock)."""
    updated_at: Annotated[float, Field(allow_inf_nan=False)]


@dataclass(frozen=True, slots=True)
class Window:
    """One occurrence of a When: [start, end) in UTC seconds, start < end. Its identity is its
    `start` (iCalendar's RECURRENCE-ID): Runtime names a Program window
    `program:<program_id>:<revision>:<start>`."""
    start: float
    end: float


__all__ = [
    "WEEKDAYS", "ClockAt", "ClockFormat", "ClockTime", "HouseModel", "HouseSettings", "HouseSettingsDraft",
    "HouseTime", "IanaZone", "canonical_days", "LocalDateTime", "OffsetMinutes", "Place", "SunAt", "TimeOfDay", "Units",
    "Weekday", "When", "WhenProblem", "Window",
]
