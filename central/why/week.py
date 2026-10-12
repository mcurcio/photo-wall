"""The week page's data, pure (Power and the week, slice S3): every Program's windows and every
Frame's power spans over seven house days, worked out once in Central (the console's repeat helper,
a second evaluator in the browser's timezone, is deleted in slice T).
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Final

from central.house_time import calculator
from central.house_time.model import HouseTime
from central.power.model import PowerPolicy
from central.power.timetable import spans
from central.runtime import Program
from central.why.model import PowerBand, Week, WeekWindow

DAYS: Final = 7


def week(*, programs: Mapping[str, Program], default_scene_id: str | None, policy: PowerPolicy,
         frame_ids: tuple[str, ...], house: HouseTime, first_day: date) -> Week:
    """Seven house days from `first_day`'s start (calculator `day_start`) to the start of the day
    seven days later: each Program's windows overlapping them (calculator `windows`), in start
    order; each Frame's power spans (power.timetable `spans`), by Frame then start. A Program
    whose When the house can no longer place (its Place removed) has no windows."""
    start = calculator.day_start(house, first_day)
    end = calculator.day_start(house, first_day + timedelta(days=DAYS))

    def local(at: float) -> str:
        return calculator.local_date_time(house, at)

    windows = []
    for program in programs.values():
        try:
            found = calculator.windows(program.when, house, start, end)
        except ValueError:
            found = ()
        windows.extend(WeekWindow(program_id=program.program_id, name=program.name, scene_id=program.scene_id,
                                  priority=program.priority, start_at=window.start, end_at=window.end,
                                  start=local(window.start), end=local(window.end)) for window in found)
    windows.sort(key=lambda window: (window.start_at, window.program_id))
    power = tuple(PowerBand(frame_id=frame_id, power=span.power, because=span.because, start_at=span.start,
                            end_at=span.end, start=local(span.start), end=local(span.end), hold_id=span.hold_id,
                            source=span.source)
                  for frame_id in sorted(frame_ids) for span in spans(frame_id, policy, house, start, end))
    return Week(first_day=first_day, zone=house.zone, default_scene_id=default_scene_id, windows=tuple(windows),
                power=power)


__all__ = ["week"]
