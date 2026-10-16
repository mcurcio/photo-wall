"""The Home Assistant settings page's answer (GET /v1/operator/integrations/home-assistant,
`HomeAssistantStatesView`): when Central last heard Home Assistant's states, every entity Home
Assistant published or a room light or Sensor names, and the states Central refused. Every entity
here is an address of SOURCE.

The MQTT link's views above it (`HomeAssistantView`, `EntityView`, `LinkHealthView`) answer no route
any more; they stay exported only while the console still names them.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from central.room_light.model import RoomLightId, SensorId
from central.room_light.views import RefusedReportView
from contracts.home_assistant import EntityId, EntityName
from contracts.models import Instant, Model

LinkState = Literal["not-configured", "connecting", "connected", "away", "never-run"]


class LinkHealthView(Model):
    """The link as the Home Assistant settings page shows it: its state and since when it has been
    in it (Central's clock; None for never-run)."""
    state: LinkState
    since: Instant | None


class LightEntityView(Model):
    """A light entity Home Assistant has reported or a room light names: the room light it reports
    (None: none yet) and when Central last heard it (None: never)."""
    kind: Literal["light"] = "light"
    entity: str
    reports: RoomLightId | None
    heard_at: Instant | None


class SensorEntityView(Model):
    """An illuminance entity Home Assistant has reported or a Sensor names: the Sensor whose readings
    it reports (None: none yet) and when Central last heard it (None: never)."""
    kind: Literal["illuminance"] = "illuminance"
    entity: str
    reports: SensorId | None
    heard_at: Instant | None


# One entity, by what it reports: a light entity can only report a room light, an illuminance one a Sensor.
EntityView = Annotated[LightEntityView | SensorEntityView, Field(discriminator="kind", title="EntityView")]


class HomeAssistantView(Model):
    """The settings page: the link, every entity, and this source's entries in the one refusal log
    (newest first; each address is the message's topic)."""
    link: LinkHealthView
    entities: tuple[EntityView, ...]
    refused: tuple[RefusedReportView, ...]


# ---- Home Assistant's states on the hub (the Home Assistant system design, 2026-10-16). These replace
# the views above when the MQTT link goes (central lane): the page reads when Central last heard a
# state, and the House › Lights picker reads every entity Home Assistant published, by its name. ----


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


__all__ = ["EntityView", "HeardEntityView", "HeardLightView", "HeardSensorView", "HomeAssistantStatesView",
           "HomeAssistantView", "LightEntityView", "LinkHealthView", "SensorEntityView"]
