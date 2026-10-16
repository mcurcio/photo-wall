"""Central listening to Home Assistant on its hub (the Home Assistant system design, 2026-10-16: "photo-
wall components must listen to home assistant to trail light state"): the adapter's one transport,
replacing the MQTT link. nodeapi.hub `run_hub_session` hands it each newest state of the bucket
Home Assistant publishes into (contracts/home_assistant.py); it turns each into a room-light fact
through `RoomLightReports` (the 0..255 scales are messages.py's) and keeps each entity's published
name and when Central last heard it, for the House › Lights picker and "states last heard".

A light entity records its LightWord against `ReporterRef(SOURCE, entity)`, an illuminance one its
Reading against `SensorRef(SOURCE, entity)`; Unavailable for Home Assistant's own unavailable; a key
that is not an entity id, or a value the contract's decoder refuses, goes to the one refusal log
(UNKNOWN_ADDRESS, UNREADABLE). A repeat records nothing new ("unchanged"). A database failure is
raised, so the hub session reads the same items again.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from central.room_light.ports import RoomLightReports
from contracts.home_assistant import EntityId, EntityKind, EntityName
from nodeapi.hub import Heard


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
        raise NotImplementedError("central lane: central/infra/home_assistant/states.py")


__all__ = ["HeardEntities", "HomeAssistantListener"]
