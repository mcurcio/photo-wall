"""What Room lights' edges depend on (decision 0028): one plain method per question, never a generic
apply. central/infra/room_light_store.py `PgRoomLights` implements all four over PostgreSQL.

Every write that changes the room-lights document raises its `change` in the same transaction and
wakes the worker's WALL writer (a LISTEN/NOTIFY channel, as central/infra/display_store.py
`DisplayWakes` does for Output documents), so a House > Lights edit in the API process reaches every
Pi without waiting.
"""
from __future__ import annotations

from typing import Literal, Protocol

from central.room_light.model import (
    IlluminanceMessage,
    LightStateMessage,
    Recorded,
    RoomLight,
    Sensor,
    SetupRefusal,
)
from central.room_light.views import RoomLightsView
from contracts.room_light import RoomLightsDocument


class RoomLightReports(Protocol):
    """Injected into the Home Assistant adapter (the worker's central/infra/home_assistant.py)."""

    def record_light_state(self, entity: str, message: LightStateMessage) -> Recorded:
        """Record the state of the room light `entity` reports (policy.light_state): "recorded" when
        it changed (the document's change rises), "unchanged" for a repeat, Refused(UNKNOWN_ENTITY)
        when no room light's reporter is that entity. Records Central's time as heard_at either way."""
        ...

    def record_illuminance(self, entity: str, message: IlluminanceMessage) -> Recorded:
        """Record the reading of the Sensor at HomeAssistantSensor(entity), and the state of the room
        light that Sensor reports (policy.illuminance_state). Refused(UNKNOWN_ENTITY) when no Sensor
        is at that entity."""
        ...

    def record_refusal(self, topic: str, reason: str) -> None:
        """Keep a refused message's topic and reason (the House page shows the latest few)."""
        ...

    def record_link(self, state: Literal["connected", "away"]) -> None:
        """The broker link's state now (HomeAssistantLinkView), stamped with Central's time."""
        ...


class SensorFacts(Protocol):
    """Injected into the Node bus projection of HostCore's line (the host state stream's judge)."""

    def record_sensor_reading(self, player_id: str, lux: float | None) -> Recorded:
        """Record the reading of the Sensor at OnPlayer(player_id) (None: its reader failed ->
        Unavailable), and the state of the room light it reports. Refused(NOT_ITS_REPORTER) when no
        Sensor is on that Pi."""
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


__all__ = ["RoomLightReports", "RoomLightSetup", "RoomLightsDocuments", "SensorFacts"]
