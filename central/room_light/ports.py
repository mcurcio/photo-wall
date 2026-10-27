"""What room lights' edges depend on: one plain method per question, never a generic apply.
central/infra/room_light_store.py `PgRoomLights` implements them over PostgreSQL.

Every report is kept by its reference whether or not a room light, Sensor or colour sensor names it
yet, and is resolved when the document is built, so one set up after its source last spoke shows
that word at once. What a report keeps is policy.py's (`light_facts_after`, `reading_facts_after`,
`colour_facts_after`: a stated value replaces the last, "can't tell" leaves it standing and marks
since when). A named reference's facts are never trimmed. Every write that changes the room-lights document raises its `change` in the same
transaction and wakes the worker's WALL writer, so a House › Lights edit in the API process reaches
every Pi without waiting.
"""
from __future__ import annotations

from typing import Protocol

from central.kernel.transactions import Transaction
from central.room_light.model import (
    ColourReading,
    ColourSensor,
    ColourSensorId,
    ColourSensorRef,
    LightReport,
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
    ColourSensorView,
    DrawnView,
    RefusedReportsView,
    RoomLightsView,
    RoomLightView,
    SensorView,
)
from contracts.room_light import (
    Reading,
    RoomLightsDocument,
    RoomLightsDrawn,
)


class RoomLightReports(Protocol):
    """Injected into every reporting source's adapter: the Home Assistant listener on Central's hub
    (central/infra/home_assistant/) and the Pi light sensor's judge of the host line. No source's
    names, scales or shapes cross it."""

    def record_light_state(self, reporter: ReporterRef, report: LightReport) -> Recorded:
        """Keep what `report` states as `reporter`'s facts (policy.light_facts_after), heard now
        (Central's clock), whether or not a room light names `reporter` yet. "recorded" when the
        document changed (its change rises and the WALL writer wakes at commit), else "unchanged" (a
        repeat, "can't tell" over a kept value, or no light names it)."""
        ...

    def record_reading(self, sensor: SensorRef, reading: Reading) -> Recorded:
        """Keep what `reading` states as `sensor`'s facts (policy.reading_facts_after), heard now,
        whether or not a Sensor is there yet; the room light that Sensor measures takes it when the
        document is built. "recorded" when the document changed, else "unchanged"."""
        ...

    def record_colour_reading(self, sensor: ColourSensorRef, reading: ColourReading) -> Recorded:
        """Keep what `reading` states as `sensor`'s facts (policy.colour_facts_after), heard now,
        whether or not a colour sensor is there yet; every room light following that colour sensor
        takes it when the document is built. "recorded" when the document changed, else "unchanged"."""
        ...

    def record_reading_within(self, tx: Transaction, sensor: SensorRef, reading: Reading) -> Recorded:
        """`record_reading` inside the caller's open transaction, under a savepoint of its own: any
        failure here rolls back only this reading and is raised, never the caller's work."""
        ...

    def record_refused(self, source: Source, address: str, reason: ReportRefusal) -> None:
        """Keep a report `source` could not record, with what it was about (`address`, the source's
        own text, cut to ADDRESS_MAX) and why, in the one refusal log; the newest REFUSALS_KEPT stay.
        Of the refusals, only UNREADABLE also sets a doubt (DoubtReason.CANT_READ, policy.doubt_after)
        on every reference at (`source`, `address`); a doubt set or changed raises the document's
        change and wakes its writer at commit, as a new word does. A store failure is raised."""
        ...

    def record_link_closed(self, source: Source) -> Recorded:
        """`source`'s connection to Central's hub closed (its adapter judged it is the connection its
        latest birth named, never an older one): every reference `source` reports takes a doubt
        (DoubtReason.LINK_CLOSED, policy.doubt_after), its last word standing. Each clears when its
        reference states a report again (Galleria restates every state when it reconnects).
        "recorded" when the document changed, else "unchanged". A store failure is raised."""
        ...

    def record_refused_within(self, tx: Transaction, source: Source, address: str, reason: ReportRefusal) -> None:
        """`record_refused` inside the caller's open transaction, under a savepoint of its own: any
        failure here rolls back only this refusal and is raised, never the caller's work."""
        ...


class RoomLightSetup(Protocol):
    """Injected into the House › Lights routes. The six writers raise RoomLightRefused."""

    def lights(self) -> RoomLightsView:
        """Every room light, Sensor and colour sensor with its reference's kept facts and colour line."""
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

    def save_colour_sensor(self, sensor_id: ColourSensorId, sensor: ColourSensor) -> ColourSensorView:
        """Create or replace colour sensor `sensor_id` (policy.check_colour_sensor; its reference's
        source and form: ReportSources.colour_address_refusal, checked by the route)."""
        ...

    def remove_colour_sensor(self, sensor_id: ColourSensorId) -> None:
        """Remove it (policy.check_colour_sensor_removal: refused while a room light follows it)."""
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

    def colour_address_refusal(self, source: Source, address: str) -> RoomLightRefusal | None:
        """UNKNOWN_ADDRESS unless `source` is one Central runs that reports colour temperatures (Home
        Assistant; never a Pi's light meter) and `address` has that source's form."""
        ...


class DrawnReports(Protocol):
    """Injected into the Player line's judge (central/infra/drawn_report_judge.py)."""

    def record_drawn_within(self, tx: Transaction, player_id: str, report: RoomLightsDrawn) -> None:
        """Keep `report` as the Player's latest, stamped with Central's clock, inside the caller's open
        transaction under a savepoint of its own: a failure rolls back only this report and is raised."""
        ...


class RoomLightsDocuments(Protocol):
    """Injected into the worker's WALL writer."""

    def current(self) -> RoomLightsDocument:
        """The document now (policy.room_lights_document over the stored lights, words and change);
        the same for every Pi. A reading a source recorded and its word did not take (a failed
        savepoint) is taken first, so a failed derivation never outlives the next build."""
        ...


__all__ = ["DrawnReports", "ReportSources", "RoomLightReports", "RoomLightSetup", "RoomLightsDocuments"]
