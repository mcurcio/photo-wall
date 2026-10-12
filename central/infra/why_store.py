"""Why over PostgreSQL (Power and the week: slice T `frame_why`, slice S3 `week`).

One transaction per answer, one clock: Runtime is projected (never advanced or saved) at
max(the clock's now, the stored Runtime's now), and the same instant is the timetable's `now`, so
content and power are explained at one moment.
"""
from __future__ import annotations

from datetime import date

from central.db import Database, TransactionClock
from central.why.model import FrameWhy, Week


class PgWhy:
    """central.why.ports WhyQueries over PostgreSQL."""

    def __init__(self, db: Database, *, times: TransactionClock) -> None:
        self._db, self._times = db, times

    def frame_why(self, frame_id: str) -> FrameWhy:
        raise NotImplementedError

    def week(self, first_day: date) -> Week:
        raise NotImplementedError


__all__ = ["PgWhy"]
