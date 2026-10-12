"""What the Why routes depend on (Power and the week: slice T `frame_why`, slice S3 `week`).
central/infra/why_store.py `PgWhy` implements it over PostgreSQL; central/why_routes.py depends
only on this.
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Protocol

from central.why.model import FrameWhy, Week


class WhyRefused(Exception):
    """A refusal the routes answer as {"error": code} with `status`: unknown_frame 404."""

    def __init__(self, code: Literal["unknown_frame"], status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class WhyQueries(Protocol):
    def frame_why(self, frame_id: str) -> FrameWhy:
        """The Frame's Why at Central's now, read in one transaction (Runtime, house time, power
        policy, the 1b test, the Output's latest document and report). WhyRefused unknown_frame."""
        ...

    def week(self, first_day: date) -> Week:
        """The week page from `first_day`, read in one transaction."""
        ...
