"""What the power routes depend on (Power and the week: slice T reads Night off, slice S2 serves
the rest): one plain method per question. central/infra/power_store.py `PgPower` implements both
over PostgreSQL; central/power_routes.py depends only on these. Every write wakes the worker's
Output document source of every Node in its own transaction (central/infra/display_store.py
`OUTPUT_DOCUMENTS_CHANNEL`), as the 1b power commands do.
"""
from __future__ import annotations

from typing import Literal, Protocol
from uuid import UUID

from central.power.model import HoldLasts
from central.power.views import HoldLastsView, HoldView, NightOffSave, NightOffView

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
        """Night off as stored (migration 089 seeds it) and its next on and off times."""
        ...

    def holds(self) -> tuple[HoldView, ...]:
        """The live holds (ends_at after Central's now, or until resumed), newest first; a hold's
        Frames that end at different times as one view per end, the earliest end first."""
        ...

    def hold_lasts(self) -> HoldLastsView:
        """How long a hold pressed now lasts (#90; migration 090 seeds it)."""
        ...


class PowerCommands(Protocol):
    def save_night_off(self, save: NightOffSave) -> NightOffView:
        """Store the draft as revision expected_revision + 1. PowerRefused "changed" when the stored
        revision differs; "no-days" / "needs-place" from `when_problems`; "unknown-frame"."""
        ...

    def set_hold_lasts(self, hold_lasts: HoldLasts) -> HoldLastsView:
        """Store how long a hold pressed from now on lasts (#90); a hold in force keeps its end."""
        ...

    def all_off(self, request_id: UUID) -> tuple[HoldView, ...]:
        """Hold every current Frame off, each until its own `hold_end` (#90), and answer the hold
        as `holds` reads it (one view per end; none with no Frames). The newest hold on a Frame
        replaces an older one; the same request id again answers the same hold."""
        ...

    def all_on(self, request_id: UUID) -> tuple[HoldView, ...]:
        """As `all_off`, on."""
        ...

    def resume_schedule(self, frame_ids: tuple[str, ...]) -> None:
        """End the holds on `frame_ids` at once (#91); a Frame with none is no error."""
        ...
