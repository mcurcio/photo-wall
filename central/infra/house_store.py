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

import logging
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from central.db import Database, TransactionClock
from central.house_time.calculator import known_zone
from central.house_time.model import HouseSettings, HouseSettingsDraft, HouseTime
from central.house_time.ports import HouseRefused

_LOG = logging.getLogger(__name__)
_LOCALTIME = Path("/etc/localtime")


def server_zone() -> str:
    """Central's own IANA zone: the TZ environment variable when it names a known zone, else the
    zone /etc/localtime links to, else "UTC". Read once per call; never cached."""
    named = os.environ.get("TZ", "").removeprefix(":")
    if known_zone(named):
        return named
    try:
        target = str(_LOCALTIME.resolve(strict=True))
    except OSError:
        target = ""
    marker = "zoneinfo/"
    linked = target.split(marker, 1)[1] if marker in target else ""
    return linked if known_zone(linked) else "UTC"


def seed_settings(zone: str, updated_at: float) -> HouseSettings:
    """The value a house with no row has: `zone`, no place, "cm", "24h"."""
    return HouseSettings(time=HouseTime(zone=zone), units="cm", clock="24h", updated_at=updated_at)


def house_settings_in(conn: Any) -> HouseSettings:
    """The stored Settings › House, or `seed_settings(server_zone(), 0.0)` with no row."""
    row = conn.execute(
        "SELECT zone, latitude, longitude, units, clock_format, updated_at FROM house_settings").fetchone()
    if row is None:
        return seed_settings(server_zone(), 0.0)
    place = None if row["latitude"] is None else {"latitude": row["latitude"], "longitude": row["longitude"]}
    try:
        return HouseSettings(time=HouseTime(zone=row["zone"], place=place), units=row["units"],
                             clock=row["clock_format"], updated_at=row["updated_at"])
    except ValidationError:
        _LOG.warning("house_settings row does not validate; reading the seed value instead")
        return seed_settings(server_zone(), 0.0)


def house_time_in(conn: Any) -> HouseTime:
    """`house_settings_in(conn).time`."""
    return house_settings_in(conn).time


def seed_house(db: Database, times: TransactionClock) -> None:
    """At Central's start: write the seed row when none exists (INSERT ... ON CONFLICT DO NOTHING),
    so Settings › House shows the zone the house started with."""
    with db.transaction() as conn:
        seed = seed_settings(server_zone(), times.now_in(conn))
        conn.execute(
            "INSERT INTO house_settings (singleton, zone, latitude, longitude, units, clock_format, updated_at)"
            " VALUES (TRUE, %s, NULL, NULL, %s, %s, %s) ON CONFLICT (singleton) DO NOTHING",
            (seed.time.zone, seed.units, seed.clock, seed.updated_at))


class PgHouse:
    """central.house_time.ports HouseQueries and HouseCommands over PostgreSQL."""

    def __init__(self, db: Database, *, times: TransactionClock) -> None:
        """`times` stamps `updated_at` (the database's clock in production, G11)."""
        self._db, self._times = db, times

    def house_settings(self) -> HouseSettings:
        with self._db.transaction() as conn:
            return house_settings_in(conn)

    def save_house_settings(self, draft: HouseSettingsDraft) -> HouseSettings:
        # The document source imports this module (house_time_in): its channel is read at call time.
        from central.infra.display_store import OUTPUT_DOCUMENTS_CHANNEL
        if not known_zone(draft.time.zone):
            raise HouseRefused("unknown-zone", 422)
        place = draft.time.place
        with self._db.transaction() as conn:
            saved = HouseSettings(**draft.model_dump(), updated_at=self._times.now_in(conn))
            conn.execute(
                "INSERT INTO house_settings (singleton, zone, latitude, longitude, units, clock_format, updated_at)"
                " VALUES (TRUE, %s, %s, %s, %s, %s, %s) ON CONFLICT (singleton) DO UPDATE SET zone=EXCLUDED.zone,"
                " latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude, units=EXCLUDED.units,"
                " clock_format=EXCLUDED.clock_format, updated_at=EXCLUDED.updated_at",
                (saved.time.zone, None if place is None else place.latitude,
                 None if place is None else place.longitude, saved.units, saved.clock, saved.updated_at))
            # Every Node's set times follow the zone and the place.
            conn.execute("SELECT pg_notify(%s, device_id) FROM players WHERE retired_at IS NULL",
                         (OUTPUT_DOCUMENTS_CHANNEL,))
        return saved


__all__ = ["PgHouse", "house_settings_in", "house_time_in", "seed_house", "seed_settings", "server_zone"]
