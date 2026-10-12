"""Settings › House over PostgreSQL (Power and the week: slice T reads and seeds, slice S3 saves;
migration 088's `house_settings`, one row).

`house_time_in` is the one read every caller of house time uses inside its own transaction (the
Runtime store, the Output document source, Why, the Planner's On this day). With no row it answers
the seed value, Central's own zone (`server_zone`), the same value `seed_house` writes at Central's
start, so correctness holds before the row exists and a later change of the server's zone never
moves a house that has a row. A row that no longer validates (a hand edit) is logged and read as
the seed value, never a crash.
"""
from __future__ import annotations

from typing import Any

from central.db import Database, TransactionClock
from central.house_time.model import HouseSettings, HouseSettingsDraft, HouseTime


def server_zone() -> str:
    """Central's own IANA zone: the TZ environment variable when it names a known zone, else the
    zone /etc/localtime links to, else "UTC". Read once per call; never cached."""
    raise NotImplementedError


def seed_settings(zone: str, updated_at: float) -> HouseSettings:
    """The value a house with no row has: `zone`, no place, "cm", "24h"."""
    raise NotImplementedError


def house_settings_in(conn: Any) -> HouseSettings:
    """The stored Settings › House, or `seed_settings(server_zone(), 0.0)` with no row."""
    raise NotImplementedError


def house_time_in(conn: Any) -> HouseTime:
    """`house_settings_in(conn).time`."""
    raise NotImplementedError


def seed_house(db: Database, times: TransactionClock) -> None:
    """At Central's start: write the seed row when none exists (INSERT ... ON CONFLICT DO NOTHING),
    so Settings › House shows the zone the house started with."""
    raise NotImplementedError


class PgHouse:
    """central.house_time.ports HouseQueries and HouseCommands over PostgreSQL."""

    def __init__(self, db: Database, *, times: TransactionClock) -> None:
        """`times` stamps `updated_at` (the database's clock in production, G11)."""
        self._db, self._times = db, times

    def house_settings(self) -> HouseSettings:
        raise NotImplementedError

    def save_house_settings(self, draft: HouseSettingsDraft) -> HouseSettings:
        raise NotImplementedError


__all__ = ["PgHouse", "house_settings_in", "house_time_in", "seed_house", "seed_settings", "server_zone"]
