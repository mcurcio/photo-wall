"""The Home Assistant settings page's answer (GET /v1/operator/integrations/home-assistant): the
link's health, which entity reports which room light or Sensor, what each entity last said, and the
messages the link refused. Every entity here is an address of SOURCE; House › Lights shows none.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from central.infra.home_assistant.health import LinkHealthView
from central.room_light.model import RoomLightId, SensorId
from central.room_light.views import RefusedReportView
from contracts.models import Instant, Model


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


__all__ = ["EntityView", "HomeAssistantView", "LightEntityView", "SensorEntityView"]
