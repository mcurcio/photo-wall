"""What the house routes depend on (Power and the week, slice T declares, slice S3 serves): one
plain method per question. central/infra/house_store.py `PgHouse` implements both over
PostgreSQL; central/house_routes.py depends only on these.
"""
from __future__ import annotations

from typing import Literal, Protocol

from central.house_time.model import HouseSettings, HouseSettingsDraft

HouseRefusal = Literal["unknown-zone", "place-in-use"]


class HouseRefused(Exception):
    """A refusal the routes answer as {"error": code} with `status`: unknown-zone 422,
    place-in-use 409 (the location is cleared while Night off or a Schedule uses sunrise or
    sunset)."""

    def __init__(self, code: HouseRefusal, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class HouseQueries(Protocol):
    def house_settings(self) -> HouseSettings:
        """Settings › House. With no row stored: Central's own zone, no place, "cm", "24h"
        (central/infra/house_store.py `seed_settings`), the same value Central seeds at start."""
        ...


class HouseCommands(Protocol):
    def save_house_settings(self, draft: HouseSettingsDraft) -> HouseSettings:
        """Store Settings › House and wake every Node's Output document source (the timetable's set
        times follow the zone and place). HouseRefused("unknown-zone", 422) for a zone the IANA
        database does not know; HouseRefused("place-in-use", 409) for no location while Night off
        or a Schedule uses sunrise or sunset (`when_problems` "needs-place", its one home), which
        would otherwise silently stop. Last write wins (one operator)."""
        ...
