"""Display power's vocabulary (Power and the week; frame "Display power: Night off and holds").

Night off (#80-#83, #90) is one stored rule for the house: off and on times, the nights it starts
on, which Frames follow it, how a display is dark, and how long a hold lasts. A hold (All off, All
on: F5) is pressed now and lasts until Night off's next on or off time, the same moment for every
Frame, or until resumed (#90). The newest hold on a Frame wins; Night off is underneath; a Frame
that follows neither is on.

Invariants the validators enforce:
- `NightOffDraft.days` unique in WEEKDAYS order; empty is the problem "no-days"
  (central/house_time/calculator.py `when_problems`, through `NightOffDraft.when`).
- `ChosenFrames.frame_ids` unique and sorted.
- `Hold.frame_ids` non-empty, unique and sorted; `ends_at` is None exactly when it lasts until
  resumed, else `ends_at > started_at`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from central.house_time.model import (
    WEEKDAYS,
    ClockAt,
    HouseModel,
    TimeOfDay,
    Weekday,
    When,
    canonical_days,
)
from contracts.models import Identifier
from contracts.node_output import Darkness, Power

HoldLasts = Literal["next-scheduled-change", "until-resumed"]       # #90, stored; one rule for every hold
PowerBecause = Literal["night-off", "hold", "test", "on-underneath"]  # why a display is wanted on or off

NIGHT_OFF_OFF: Final = ClockAt(time="23:00")    # #80 defaults (owner's roadmap E2)
NIGHT_OFF_ON: Final = ClockAt(time="07:00")
EVERY_DAY: Final[tuple[Weekday, ...]] = WEEKDAYS


class ChosenFrames(HouseModel):
    """Frames chosen one by one (#82; the default: every Frame there was at deploy)."""
    kind: Literal["frames"] = "frames"
    frame_ids: tuple[Identifier, ...]

    @field_validator("frame_ids")
    @classmethod
    def _sorted(cls, frame_ids: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_sorted(frame_ids, "frame_ids")


class AllFrames(HouseModel):
    """The "All Frames" group: a Frame added later follows Night off too (R11: chosen explicitly)."""
    kind: Literal["all-frames"] = "all-frames"


NightOffFrames = Annotated[ChosenFrames | AllFrames, Field(discriminator="kind")]


class NightOffDraft(HouseModel):
    """What the Power lane's Night off editor saves."""
    off: TimeOfDay
    on: TimeOfDay                                   # at or before `off`: the next day
    days: tuple[Weekday, ...]                       # the nights it starts on
    frames: NightOffFrames
    darkness: Darkness
    hold_lasts: HoldLasts

    @field_validator("days")
    @classmethod
    def _canonical(cls, days: tuple[Weekday, ...]) -> tuple[Weekday, ...]:
        return canonical_days(days)

    @property
    def when(self) -> When:
        """Night off as a When: When(days, start=off, end=on). The calculator's one shape."""
        return When(days=self.days, start=self.off, end=self.on)


class NightOff(NightOffDraft):
    """Night off as stored; `revision` rises by one per save."""
    revision: Annotated[int, Field(ge=1)]


class HoldSource(HouseModel):
    """Who set a hold (#91). The console has one operator; roadmap 3 adds Home Assistant."""
    kind: Literal["console"] = "console"


class Hold(HouseModel):
    """A hold on some Frames. `hold_id` is the console's request id, so a double tap makes one hold.
    `started_at` and `ends_at` are Central's clock, compared only with Central's clock: Central
    drops the hold when its own clock passes `ends_at`; the Pi follows the timetable's set time."""
    hold_id: UUID
    power: Power
    frame_ids: tuple[Identifier, ...]
    source: HoldSource
    started_at: Annotated[float, Field(allow_inf_nan=False)]
    ends_at: Annotated[float, Field(allow_inf_nan=False)] | None

    @field_validator("frame_ids")
    @classmethod
    def _frames(cls, frame_ids: tuple[str, ...]) -> tuple[str, ...]:
        if not frame_ids:
            raise ValueError("frame_ids")
        return _unique_sorted(frame_ids, "frame_ids")

    @model_validator(mode="after")
    def _ends_after_start(self) -> Hold:
        if self.ends_at is not None and not self.ends_at > self.started_at:
            raise ValueError("ends_at")
        return self


def _unique_sorted(values: tuple[str, ...], code: str) -> tuple[str, ...]:
    if list(values) != sorted(set(values)):
        raise ValueError(code)
    return values


@dataclass(frozen=True, slots=True)
class PowerPolicy:
    """Everything the timetable is worked out from, read in one transaction: Night off, the holds
    live at the read (each Hold's `ends_at` after it), and whether the operator has confirmed the
    house's zone (Settings › House `zone_confirmed`)."""
    night_off: NightOff
    holds: tuple[Hold, ...]
    zone_confirmed: bool


__all__ = ["EVERY_DAY", "NIGHT_OFF_OFF", "NIGHT_OFF_ON", "AllFrames", "ChosenFrames", "Hold", "HoldLasts",
           "HoldSource", "NightOff", "NightOffDraft", "NightOffFrames", "PowerBecause", "PowerPolicy"]
