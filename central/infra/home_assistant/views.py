"""The Home Assistant settings page's answer (GET /v1/operator/integrations/home-assistant,
`HomeAssistantStatesView`): when Central last heard Home Assistant's states, every entity Home
Assistant published or a room light or Sensor names, and the states Central refused. Every entity
here is an address of SOURCE.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from central.room_light.model import RoomLightId, SensorId
from central.room_light.views import RefusedReportView
from contracts.home_assistant import EntityId, EntityName
from contracts.models import Instant, Model


class HeardLightView(Model):
    """A light entity Home Assistant published or a room light names: its friendly name as last
    published (None: never published), the room light it reports (None: none yet) and when Central
    last heard it (None: never)."""
    kind: Literal["light"] = "light"
    entity: EntityId
    name: EntityName | None
    reports: RoomLightId | None
    heard_at: Instant | None


class HeardSensorView(Model):
    """An illuminance entity Home Assistant published or a Sensor names: its friendly name as last
    published (None: never published), the Sensor whose readings it reports (None: none yet) and when
    Central last heard it (None: never)."""
    kind: Literal["illuminance"] = "illuminance"
    entity: EntityId
    name: EntityName | None
    reports: SensorId | None
    heard_at: Instant | None


HeardEntityView = Annotated[HeardLightView | HeardSensorView, Field(discriminator="kind", title="HeardEntityView")]


class HomeAssistantStatesView(Model):
    """GET /v1/operator/integrations/home-assistant: when Central last heard any state from Home
    Assistant ("states last heard"; None: never), every entity (newest heard first), and this
    source's entries in the one refusal log (newest first; each address is the entity id)."""
    heard_at: Instant | None
    entities: tuple[HeardEntityView, ...]
    refused: tuple[RefusedReportView, ...]


__all__ = ["HeardEntityView", "HeardLightView", "HeardSensorView", "HomeAssistantStatesView"]
