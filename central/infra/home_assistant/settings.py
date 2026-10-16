"""The Home Assistant settings page's read: the link's health, every entity Home Assistant reported or
a room light or Sensor names, and the messages the link refused (the one refusal log, this source's
entries)."""
from __future__ import annotations

from central.db import Database
from central.infra.home_assistant.health import PgLinkHealth
from central.infra.home_assistant.topics import SOURCE
from central.infra.home_assistant.views import EntityView, HomeAssistantView
from central.infra.room_light_store import references_in, refused_in


class PgHomeAssistantSettings:
    """GET /v1/operator/integrations/home-assistant over one Database."""

    def __init__(self, db: Database, health: PgLinkHealth) -> None:
        self._db = db
        self._health = health

    def view(self) -> HomeAssistantView:
        with self._db.transaction() as conn:
            entities = references_in(conn, SOURCE)
            refused = refused_in(conn, SOURCE)
        return HomeAssistantView(
            link=self._health.health(),
            entities=tuple(EntityView(entity=entity, reports_light=light, reports_sensor=sensor, heard_at=heard_at)
                           for entity, (light, sensor, heard_at) in entities.items()),
            refused=refused)


__all__ = ["PgHomeAssistantSettings"]
