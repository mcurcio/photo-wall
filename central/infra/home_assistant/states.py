"""Central listening to Home Assistant on its hub (the Home Assistant system design, 2026-10-16: "photo-
wall components must listen to home assistant to trail light state"): the adapter's one transport,
replacing the MQTT link. nodeapi.hub `run_hub_session` hands it each newest state of the bucket
Home Assistant publishes into (contracts/home_assistant.py); it turns each into a room-light fact
through `RoomLightReports` (the 0..255 scales are messages.py's) and keeps each entity's published
name and when Central last heard it, for the House › Lights picker and "states last heard".

A light entity records its LightWord against `ReporterRef(SOURCE, entity)`, an illuminance one its
Reading against `SensorRef(SOURCE, entity)`; Unavailable for Home Assistant's own unavailable; a key
that is not an entity id, or a value the contract's decoder refuses, goes to the one refusal log
(UNKNOWN_ADDRESS, UNREADABLE). A state of the other kind than its entity's domain says (a light
state for an entity outside the `light` domain, an illuminance one for a `light.` entity) is
UNREADABLE too, so a light never records under a SensorRef nor a reading under a ReporterRef. A key
Home Assistant removed (a value of None) records nothing: the state Central heard last stands. A
repeat records nothing new ("unchanged"). A database failure is raised, so the hub session reads the
same items again.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Final, Protocol

from central.infra.home_assistant.messages import MessageRefused, light_word, reading
from central.infra.home_assistant.references import (
    SOURCE,
    illuminance_sensor,
    is_entity,
    light_reporter,
)
from central.room_light.model import ReportRefusal
from central.room_light.ports import RoomLightReports
from contracts.home_assistant import (
    EntityId,
    EntityKind,
    EntityName,
    HomeAssistantIlluminance,
    HomeAssistantLight,
    HomeAssistantState,
    decode_home_assistant_state,
)
from nodeapi.hub import Heard

LIGHT_DOMAIN: Final = "light"   # Home Assistant's domain of light entities: every other entity reports lux


class HeardEntities(Protocol):
    """Each entity Home Assistant published: its kind, its friendly name, and when Central last heard
    it (central/infra/room_light_store.py implements it beside the words)."""

    def record_heard(self, entity: EntityId, kind: EntityKind, name: EntityName) -> None:
        """Keep `entity`'s kind and name, heard now (Central's clock)."""
        ...


class HomeAssistantListener:
    """nodeapi.hub ListenedStates over room lights' reports and the heard entities."""

    def __init__(self, reports: RoomLightReports, heard: HeardEntities) -> None:
        self._reports = reports
        self._heard = heard

    async def heard(self, epoch: str, items: Sequence[Heard]) -> None:
        """Record each item, in order, in a thread (the stores block); see the module's rules."""
        await asyncio.to_thread(self._record_all, items)

    def _record_all(self, items: Sequence[Heard]) -> None:
        for item in items:
            self.record(item.entity, item.value)

    def record(self, entity: str, value: bytes | None) -> None:
        """One newest value of Home Assistant's states bucket under the key `entity`."""
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
        if kind != ("light" if entity.split(".", 1)[0] == LIGHT_DOMAIN else "illuminance"):
            self._reports.record_refused(SOURCE, entity, ReportRefusal.UNREADABLE)
            return
        try:
            if kind == "light":
                assert not isinstance(state, HomeAssistantIlluminance)
                self._reports.record_light_state(light_reporter(entity), light_word(state))
            else:
                assert not isinstance(state, HomeAssistantLight)
                self._reports.record_reading(illuminance_sensor(entity), reading(state))
        except MessageRefused as refused:
            self._reports.record_refused(SOURCE, entity, refused.reason)
        self._heard.record_heard(entity, kind, state.name)


def _kind_of(state: HomeAssistantState) -> EntityKind:
    if isinstance(state, HomeAssistantLight):
        return "light"
    if isinstance(state, HomeAssistantIlluminance):
        return "illuminance"
    return state.entity_kind


__all__ = ["HeardEntities", "HomeAssistantListener"]
