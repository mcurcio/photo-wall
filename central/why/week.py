"""The week page's data, pure (Power and the week, slice S3): every Program's windows and every
Frame's power spans over seven house days, worked out once in Central (the console's repeat helper,
a second evaluator in the browser's timezone, is deleted in slice T).
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from central.house_time.model import HouseTime
from central.power.model import PowerPolicy
from central.runtime import Program
from central.why.model import Week


def week(*, programs: Mapping[str, Program], default_scene_id: str | None, policy: PowerPolicy,
         frame_ids: tuple[str, ...], house: HouseTime, first_day: date) -> Week:
    """Seven house days from `first_day`'s start (calculator `day_start`) to the start of the day
    seven days later: each Program's windows overlapping them (calculator `windows`), in start
    order; each Frame's power spans (power.timetable `spans`), by Frame then start."""
    raise NotImplementedError


__all__ = ["week"]
