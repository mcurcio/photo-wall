"""The Home Assistant settings page's answer (GET /v1/operator/integrations/home-assistant): the
link's health, which entity reports which room light or Sensor, what each entity last said, and the
messages the link refused. Every entity here is an address of SOURCE; House › Lights shows none.
"""
from __future__ import annotations

from central.infra.home_assistant.health import LinkHealthView
from central.room_light.model import RoomLightId, SensorId
from central.room_light.views import RefusedReportView
from contracts.models import Instant, Model


class EntityView(Model):
    """One entity Home Assistant has reported or a room light or Sensor names: the room light or
    Sensor it reports (None: none yet), and when Central last heard it (None: never)."""
    entity: str
    reports_light: RoomLightId | None
    reports_sensor: SensorId | None
    heard_at: Instant | None


class HomeAssistantView(Model):
    """The settings page: the link, every entity, and this source's entries in the one refusal log
    (newest first; each address is the message's topic)."""
    link: LinkHealthView
    entities: tuple[EntityView, ...]
    refused: tuple[RefusedReportView, ...]


__all__ = ["EntityView", "HomeAssistantView"]
