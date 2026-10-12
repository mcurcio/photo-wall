"""Display power policy over PostgreSQL (Power and the week: slice T for Night off, slice S2 for
holds; migrations 088 `night_off` and 089 `power_holds`).

`power_policy_in` is the one read of the policy: the Output document source
(central/infra/display_store.py), Why and the shown-history filter all call it in their own
transaction. Each command wakes every Node's Output document source in its own transaction
(`NOTIFY OUTPUT_DOCUMENTS_CHANNEL`, from app code; no trigger), and the source also wakes at the
timetable's next set time, its next horizon move and the next hold end, so a timetable rolls
forward with no other write.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from central.db import Database, TransactionClock
from central.power.model import NightOff, PowerPolicy
from central.power.views import HoldView, NightOffSave, NightOffView


def night_off_in(conn: Any) -> NightOff:
    """The stored Night off (migration 088 writes the row; it always exists)."""
    raise NotImplementedError


def power_policy_in(conn: Any, now: float) -> PowerPolicy:
    """Night off and the holds live at Central's `now` (ends_at after it, or until resumed). Slice T
    answers no holds; slice S2 reads `power_holds`."""
    raise NotImplementedError


def wanted_off_in(conn: Any, frame_id: str, start: float, end: float) -> bool:
    """Slice S2 (F3): whether `frame_id`'s wanted power is off for the whole of [start, end) by the
    policy live now (power.timetable `spans`); shown history ignores such a layer."""
    raise NotImplementedError


class PgPower:
    """central.power.ports PowerQueries and PowerCommands over PostgreSQL."""

    def __init__(self, db: Database, *, times: TransactionClock) -> None:
        """`times` is Central's clock for hold starts and ends (the database's in production, G11)."""
        self._db, self._times = db, times

    def night_off(self) -> NightOffView:
        raise NotImplementedError

    def holds(self) -> tuple[HoldView, ...]:
        raise NotImplementedError

    def save_night_off(self, save: NightOffSave) -> NightOffView:
        raise NotImplementedError

    def all_off(self, request_id: UUID) -> HoldView:
        raise NotImplementedError

    def all_on(self, request_id: UUID) -> HoldView:
        raise NotImplementedError

    def resume_schedule(self, frame_ids: tuple[str, ...]) -> None:
        raise NotImplementedError


__all__ = ["PgPower", "night_off_in", "power_policy_in", "wanted_off_in"]
