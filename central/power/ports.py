"""What the power routes depend on (Power and the week: slice T reads Night off, slice S2 serves
the rest): one plain method per question. central/infra/power_store.py `PgPower` implements both
over PostgreSQL; central/power_routes.py depends only on these. Every write wakes the worker's
Output document source of every Node in its own transaction (central/infra/display_store.py
`OUTPUT_DOCUMENTS_CHANNEL`), as the 1b power commands do.
"""
from __future__ import annotations

from typing import Literal, Protocol
from uuid import UUID

from central.power.views import HoldView, NightOffSave, NightOffView

PowerRefusal = Literal["changed", "no-days", "needs-place", "unknown-frame"]


class PowerRefused(Exception):
    """A refusal the routes answer as {"error": code} with `status`: changed 409 (someone saved
    first), no-days 422, needs-place 422, unknown-frame 422 (a chosen Frame does not exist)."""

    def __init__(self, code: PowerRefusal, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class PowerQueries(Protocol):
    def night_off(self) -> NightOffView:
        """Night off as stored (migration 088 seeds it) and its next on and off times."""
        ...

    def holds(self) -> tuple[HoldView, ...]:
        """The live holds (ends_at after Central's now, or until resumed), newest first."""
        ...


class PowerCommands(Protocol):
    def save_night_off(self, save: NightOffSave) -> NightOffView:
        """Store the draft as revision expected_revision + 1. PowerRefused "changed" when the stored
        revision differs; "no-days" / "needs-place" from `when_problems`; "unknown-frame"."""
        ...

    def all_off(self, request_id: UUID) -> HoldView:
        """Hold every current Frame off until `hold_end` (#90). The newest hold on a Frame replaces
        an older one; the same request id again answers the same hold."""
        ...

    def all_on(self, request_id: UUID) -> HoldView:
        """As `all_off`, on."""
        ...

    def resume_schedule(self, frame_ids: tuple[str, ...]) -> None:
        """End the holds on `frame_ids` at once (#91); a Frame with none is no error."""
        ...
