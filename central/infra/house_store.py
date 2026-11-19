"""Settings › House over PostgreSQL (Power and the week: slice T reads and seeds, slice S3 saves;
`singletons` 'house', decision 0034's migration 116).

`house_time_in` is the one read every caller of house time uses inside its own transaction (the
Runtime store, the Output document source, Why, the Planner's On this day). With no row it answers
the seed value, Central's own zone (`server_zone`), the same value `seed_house` writes at Central's
start, so correctness holds before the row exists and a later change of the server's zone never
moves a house that has a row. A row that no longer validates (a hand edit) is logged and read as
the seed value, never a crash (`Singleton.read_in`).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final, Protocol

from central.actor import Actor
from central.db import Database, TransactionClock
from central.house_time.calculator import known_zone, when_problems
from central.house_time.model import HouseSettings, HouseSettingsDraft, HouseTime
from central.house_time.ports import HouseRefused
from central.infra.output_wakes import wake_every_node_in
from central.infra.singletons import Singleton, Stored
from central.infra.transactions import PgTransaction
from central.room_light.ports import HouseClockChanges

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
    """The value a house with no row has: `zone`, no place, "cm", "24h", the zone not confirmed."""
    return HouseSettings(time=HouseTime(zone=zone), units="cm", clock="24h", updated_at=updated_at,
                         zone_confirmed=False)


class _HouseDocument(HouseSettingsDraft):
    """Settings › House as `singletons` 'house' holds it: the draft plus `zone_confirmed`.
    `HouseSettings.updated_at` is the row's `changed_at`, never stored twice."""
    zone_confirmed: bool


HOUSE: Final[Singleton[_HouseDocument]] = Singleton("house", _HouseDocument)


class ProcessedInstant(Protocol):
    """What the house save needs of the Runtime store (RuntimeStore satisfies it)."""

    def save_processed_in(self, conn: Any, *, by: Actor) -> None: ...


def house_settings_in(conn: Any) -> HouseSettings:
    """The stored Settings › House (`updated_at` its row's `changed_at`), or
    `seed_settings(server_zone(), 0.0)` with no row or one that does not validate."""
    stored = HOUSE.read_in(conn)
    if stored is None:
        return seed_settings(server_zone(), 0.0)
    return HouseSettings(**stored.value.model_dump(), updated_at=stored.changed_at)


def house_time_in(conn: Any) -> HouseTime:
    """`house_settings_in(conn).time`."""
    return house_settings_in(conn).time


def _document(settings: HouseSettings) -> _HouseDocument:
    return _HouseDocument(**settings.model_dump(exclude={"updated_at"}))


def seed_house(db: Database, times: TransactionClock) -> None:
    """At Central's start: store the seed document when none exists, so Settings › House shows the
    zone the house started with."""
    with db.transaction() as conn:
        HOUSE.seed_in(conn, _document(seed_settings(server_zone(), 0.0)), by="central", times=times)


class PgHouse:
    """central.house_time.ports HouseQueries and HouseCommands over PostgreSQL."""

    def __init__(self, db: Database, *, times: TransactionClock, room_lights: HouseClockChanges,
                 runtime: ProcessedInstant) -> None:
        """`times` stamps the row's audit times (the database's clock in production, G11);
        `room_lights` raises the room-lights document's change when a save moves its words (they
        are dated on the house's clock); `runtime` saves the Runtime's processed instant before a
        save moves the house's time."""
        self._db, self._times, self._room_lights, self._runtime = db, times, room_lights, runtime

    def house_settings(self) -> HouseSettings:
        with self._db.transaction() as conn:
            return house_settings_in(conn)

    def save_house_settings(self, draft: HouseSettingsDraft, *, by: Actor) -> HouseSettings:
        # (An unknown zone never reaches here: HouseTime refuses it when the request is parsed.)
        # The power store and the Runtime store import this module
        # (house_time_in): they are read at call time.
        from central.infra.power_store import night_off_in
        from central.runtime_store import RuntimeStore
        with self._db.transaction() as conn:
            if house_time_in(conn) != draft.time:
                # Windows move with the zone and the place: the Runtime is first advanced to now
                # under the house still stored, so it never admits a window the change moved
                # before its processed instant (R10).
                self._runtime.save_processed_in(conn, by=by)
            if draft.time.place is None:
                whens = (night_off_in(conn).when, *RuntimeStore.read_in(conn).program_whens())
                if any("needs-place" in when_problems(when, draft.time) for when in whens):
                    raise HouseRefused("place-in-use", 409)
            document = _HouseDocument(**draft.model_dump(), zone_confirmed=True)
            written: list[Stored[_HouseDocument]] = []
            self._room_lights.house_clock_changing_within(PgTransaction(conn), lambda: written.append(
                HOUSE.write_in(conn, document, by=by, times=self._times)))
            stored = written[0]
            # Every Node's set times follow the zone and the place.
            wake_every_node_in(conn)
        return HouseSettings(**stored.value.model_dump(), updated_at=stored.changed_at)


__all__ = ["HOUSE", "PgHouse", "ProcessedInstant", "house_settings_in", "house_time_in", "seed_house",
           "seed_settings", "server_zone"]
