"""What room lights' edges depend on: one plain method per question, never a generic apply.
central/infra/room_light_store.py `PgRoomLights` implements them over PostgreSQL.

Every report is kept by its reference whether or not a room light or Sensor names it yet, and is
resolved when the document is built, so a light or Sensor set up after its source last spoke shows
that word at once. Every write that changes the room-lights document raises its `change` in the same
transaction and wakes the worker's WALL writer, so a House › Lights edit in the API process reaches
every Pi without waiting.
"""
from __future__ import annotations

from typing import Protocol

from central.kernel.transactions import Transaction
from central.room_light.model import (
    Recorded,
    ReporterRef,
    ReportRefusal,
    RoomLight,
    RoomLightId,
    RoomLightRefusal,
    Sensor,
    SensorId,
    SensorRef,
    Source,
)
from central.room_light.views import (
    DrawnView,
    RefusedReportsView,
    RoomLightsView,
    RoomLightView,
    SensorView,
)
from contracts.room_light import (
    ColourDimmer,
    Dimmer,
    Measured,
    RoomLightsDocument,
    RoomLightsDrawn,
    Unavailable,
)


class RoomLightReports(Protocol):
    """Injected into every reporting source's adapter: the worker's Home Assistant link
    (central/infra/home_assistant/) and the Pi light sensor's judge of the host line. No source's
    names, scales or shapes cross it."""

    def record_light_state(self, reporter: ReporterRef, state: Dimmer | ColourDimmer | Unavailable) -> Recorded:
        """Keep `state` as `reporter`'s last word, and Central's time as when it was heard, whether or
        not a room light names `reporter` yet. "recorded" when the document changed (its change
        rises and the WALL writer wakes at commit), else "unchanged" (a repeat, or no light names it)."""
        ...

    def record_reading(self, sensor: SensorRef, reading: Measured | Unavailable) -> Recorded:
        """Keep `reading` as `sensor`'s last word, heard now, whether or not a Sensor is there yet;
        the room light that Sensor measures takes it when the document is built. "recorded" when the
        document changed, else "unchanged"."""
        ...

    def record_reading_within(self, tx: Transaction, sensor: SensorRef, reading: Measured | Unavailable) -> Recorded:
        """`record_reading` inside the caller's open transaction, under a savepoint of its own: any
        failure here rolls back only this reading and is raised, never the caller's work."""
        ...

    def record_refused(self, source: Source, address: str, reason: ReportRefusal) -> None:
        """Keep a report `source` could not record, with what it was about (`address`, the source's
        own text, cut to ADDRESS_MAX) and why, in the one refusal log; the newest REFUSALS_KEPT stay."""
        ...


class RoomLightSetup(Protocol):
    """Injected into the House › Lights routes. The four writers raise RoomLightRefused."""

    def lights(self) -> RoomLightsView:
        """Every room light and Sensor with its reference's last word."""
        ...

    def save_room_light(self, light_id: RoomLightId, light: RoomLight) -> RoomLightView:
        """Create or replace room light `light_id` (policy.check_room_light)."""
        ...

    def remove_room_light(self, light_id: RoomLightId) -> None:
        """Remove it (policy.check_room_light_removal)."""
        ...

    def save_sensor(self, sensor_id: SensorId, sensor: Sensor) -> SensorView:
        """Create or replace Sensor `sensor_id` (policy.check_sensor)."""
        ...

    def remove_sensor(self, sensor_id: SensorId) -> None:
        """Remove it (policy.check_sensor_removal)."""
        ...

    def drawn(self) -> DrawnView:
        """What each Player last said it drew with, judged against the document now (policy.drawn_state)."""
        ...

    def refused(self) -> RefusedReportsView:
        """The newest refused reports from every source, newest first."""
        ...


class ReportSources(Protocol):
    """The reporting sources Central runs, each judging its own addresses' form. Composed in
    central/app.py from each adapter's own check."""

    def address_refusal(self, source: Source, address: str) -> RoomLightRefusal | None:
        """UNKNOWN_ADDRESS unless `source` is one Central runs and `address` has that source's form."""
        ...


class DrawnReports(Protocol):
    """Injected into the Player's drawn-report route."""

    def record_drawn(self, player_id: str, report: RoomLightsDrawn) -> None:
        """Keep `report` as the Player's latest, stamped with Central's clock."""
        ...


class RoomLightsDocuments(Protocol):
    """Injected into the worker's WALL writer."""

    def current(self) -> RoomLightsDocument:
        """The document now (policy.room_lights_document over the stored lights, words and change);
        the same for every Pi."""
        ...


__all__ = ["DrawnReports", "ReportSources", "RoomLightReports", "RoomLightSetup", "RoomLightsDocuments"]
