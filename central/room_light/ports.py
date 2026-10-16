"""What Room lights' edges depend on: one plain method per question, never a generic apply.
central/infra/room_light_store.py `PgRoomLights` implements them over PostgreSQL.

Every report is kept by its reference whether or not a room light or Sensor names it yet, and is
resolved when the document is built, so a light or Sensor set up after its source last spoke shows
that word at once. Every write that changes the room-lights document raises its `change` in the same
transaction and wakes the worker's WALL writer, so a House > Lights edit in the API process reaches
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
    Sensor,
    SensorRef,
    SetupRefusal,
    Source,
)
from central.room_light.views import RoomLightsView
from contracts.room_light import ColourDimmer, Dimmer, Measured, RoomLightsDocument, Unavailable


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
        the room light that Sensor reports takes it when the document is built. "recorded" when the
        document changed, else "unchanged"."""
        ...

    def record_reading_within(self, tx: Transaction, sensor: SensorRef, reading: Measured | Unavailable) -> Recorded:
        """`record_reading` inside the caller's open transaction, under a savepoint of its own: any
        failure here rolls back only this reading and is raised, never the caller's work (the host
        line's record)."""
        ...

    def record_refused(self, source: Source, address: str, reason: ReportRefusal) -> None:
        """Keep a report `source` could not record, with what it was about (`address`, the source's
        own text, cut to ADDRESS_MAX) and why, in the one refusal log; the newest REFUSALS_KEPT stay."""
        ...


class RoomLightSetup(Protocol):
    """Injected into the House > Lights routes."""

    def lights(self) -> RoomLightsView:
        """Every room light and Sensor with its last state, and the link (views.RoomLightsView)."""
        ...

    def save_room_light(self, light: RoomLight) -> SetupRefusal | None:
        """Create or replace `light` (policy.check_room_light); a new fixed light's state is FULL_ON,
        any other new light's NotReported. A changed reporter resets the state to NotReported."""
        ...

    def remove_room_light(self, light_id: str) -> SetupRefusal | None:
        """Remove it (policy.check_room_light_removal)."""
        ...

    def save_sensor(self, sensor: Sensor) -> SetupRefusal | None:
        """Create or replace `sensor` (policy.check_sensor)."""
        ...

    def remove_sensor(self, sensor_id: str) -> SetupRefusal | None:
        """Remove it (policy.check_sensor_removal)."""
        ...


class RoomLightsDocuments(Protocol):
    """Injected into the worker's WALL writer."""

    def current(self) -> RoomLightsDocument:
        """The document now (policy.room_lights_document over the stored lights, states and change);
        the same for every Pi."""
        ...


__all__ = ["RoomLightReports", "RoomLightSetup", "RoomLightsDocuments"]
