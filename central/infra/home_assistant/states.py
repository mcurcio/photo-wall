"""Central listening to Home Assistant on its hub (the Home Assistant system design, 2026-10-16: "photo-
wall components must listen to home assistant to trail light state"): the adapter's one transport,
replacing the MQTT link. nodeapi.hub `run_hub_session` hands it each newest state of the bucket
Home Assistant publishes into (contracts/home_assistant.py); it turns each into a room-light fact
through `RoomLightReports` (the 0..255 scales are messages.py's) and keeps each entity's published
name and when Central last heard it, for the House › Lights picker and "states last heard".

A light entity records its LightReport against `ReporterRef(SOURCE, entity)`, an illuminance one its
Reading against `SensorRef(SOURCE, entity)`, a colour-temperature one its ColourReading against
`ColourSensorRef(SOURCE, entity)`; Unavailable for Home Assistant's own unavailable (the last stated
value stands: policy.py). A key that is not an entity id, or a value the contract's decoder refuses,
goes to the one refusal log (UNKNOWN_ADDRESS, UNREADABLE); GALLERIA_BIRTH_KEY is Galleria's birth,
kept for the settings page. A state of a light kind for an entity outside the `light` domain, or a
sensor kind for a `light.` entity, is UNREADABLE too, so a light never records under a sensor's
reference nor a reading under a ReporterRef. A light's colour outside the stated range is refused
on its own (OUT_OF_RANGE) and its brightness still recorded. A key
Home Assistant removed (a value of None) records nothing: the state Central heard last stands. A
repeat records nothing new ("unchanged"). A database failure is raised, so the hub session reads the
same items again.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Final, Protocol

from central.infra.home_assistant.messages import (
    MessageRefused,
    colour_reading,
    light_report,
    reading,
)
from central.infra.home_assistant.references import (
    SOURCE,
    colour_temperature_sensor,
    illuminance_sensor,
    is_entity,
    light_reporter,
)
from central.room_light.model import ReportRefusal
from central.room_light.ports import RoomLightReports
from contracts.home_assistant import (
    GALLERIA_BIRTH_KEY,
    EntityId,
    EntityKind,
    EntityName,
    GalleriaBirth,
    GalleriaConnection,
    HomeAssistantColourTemperature,
    HomeAssistantIlluminance,
    HomeAssistantLight,
    HomeAssistantState,
    decode_galleria_birth,
    decode_home_assistant_state,
)
from nodeapi.hub import Heard

LIGHT_DOMAIN: Final = "light"   # Home Assistant's domain of light entities: every other entity reports lux or kelvin


class HeardEntities(Protocol):
    """Each entity Home Assistant published: its kind, its friendly name, and when Central last heard
    it (central/infra/room_light_store.py implements it beside the words)."""

    def record_heard(self, entity: EntityId, kind: EntityKind, name: EntityName) -> None:
        """Keep `entity`'s kind and name, heard now (Central's clock)."""
        ...


class GalleriaBirths(Protocol):
    """Galleria's birth as Central last heard it (central/infra/room_light_store.py implements it)."""

    def record_galleria_birth(self, birth: GalleriaBirth) -> None:
        """Keep `birth` as Galleria's, heard now (Central's clock), replacing the last."""
        ...


class HomeAssistantListener:
    """nodeapi.hub ListenedStates over room lights' reports, the heard entities and Galleria's births."""

    def __init__(self, reports: RoomLightReports, heard: HeardEntities, births: GalleriaBirths) -> None:
        self._reports = reports
        self._heard = heard
        self._births = births

    async def heard(self, epoch: str, items: Sequence[Heard]) -> None:
        """Record each item, in order, in a thread (the stores block); see the module's rules."""
        await asyncio.to_thread(self._record_all, items)

    async def closed(self, connection: GalleriaConnection) -> None:
        """A door connection closed (nodeapi.hub ListenedStates): when it is the connection Galleria's
        latest birth names, every reference Home Assistant reports takes the "link closed" doubt
        (RoomLightReports.record_link_closed(SOURCE)); a close of any other connection (an older
        one, closed late) changes nothing. Cleared when Galleria reconnects and restates."""
        raise NotImplementedError("central edge lane")

    def _record_all(self, items: Sequence[Heard]) -> None:
        for item in items:
            self.record(item.entity, item.value)

    def record(self, entity: str, value: bytes | None) -> None:
        """One newest value of Home Assistant's states bucket under the key `entity`."""
        if entity == GALLERIA_BIRTH_KEY:
            self._birth(value)
            return
        if not is_entity(entity):
            self._reports.record_refused(SOURCE, entity, ReportRefusal.UNKNOWN_ADDRESS)
            return
        if value is None:   # removed by its publisher: the last word heard stands
            return
        try:
            state = decode_home_assistant_state(value)
        except ValueError:
            self._reports.record_refused(SOURCE, entity, ReportRefusal.UNREADABLE)
            return
        kind = _kind_of(state)
        if (kind == "light") != (entity.split(".", 1)[0] == LIGHT_DOMAIN):
            self._reports.record_refused(SOURCE, entity, ReportRefusal.UNREADABLE)
            return
        try:
            if kind == "light":
                assert not isinstance(state, HomeAssistantIlluminance | HomeAssistantColourTemperature)
                report, colour_refused = light_report(state)
                self._reports.record_light_state(light_reporter(entity), report)
                if colour_refused is not None:
                    self._reports.record_refused(SOURCE, entity, colour_refused.reason)
            elif kind == "illuminance":
                assert not isinstance(state, HomeAssistantLight | HomeAssistantColourTemperature)
                self._reports.record_reading(illuminance_sensor(entity), reading(state))
            else:
                assert not isinstance(state, HomeAssistantLight | HomeAssistantIlluminance)
                self._reports.record_colour_reading(colour_temperature_sensor(entity), colour_reading(state))
        except MessageRefused as refused:
            self._reports.record_refused(SOURCE, entity, refused.reason)
        self._heard.record_heard(entity, kind, state.name)


    def _birth(self, value: bytes | None) -> None:
        if value is None:   # removed by its publisher: the last birth heard stands
            return
        try:
            birth = decode_galleria_birth(value)
        except ValueError:
            self._reports.record_refused(SOURCE, GALLERIA_BIRTH_KEY, ReportRefusal.UNREADABLE)
            return
        self._births.record_galleria_birth(birth)


def _kind_of(state: HomeAssistantState) -> EntityKind:
    if isinstance(state, HomeAssistantLight):
        return "light"
    if isinstance(state, HomeAssistantIlluminance):
        return "illuminance"
    if isinstance(state, HomeAssistantColourTemperature):
        return "colour-temperature"
    return state.entity_kind


__all__ = ["GalleriaBirths", "HeardEntities", "HomeAssistantListener"]
