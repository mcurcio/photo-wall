"""The Home Assistant settings page's read: when Central last heard Home Assistant's states, every
entity Home Assistant published or a room light or Sensor names, and the states Central refused (the
one refusal log, this source's entries)."""
from __future__ import annotations

from central.db import Database
from central.infra.home_assistant.references import SOURCE
from central.infra.home_assistant.views import (
    HeardEntityView,
    HeardLightView,
    HeardSensorView,
    HomeAssistantStatesView,
)
from central.infra.room_light_store import HeardEntity, heard_entities_in, last_heard_in, refused_in


class PgHomeAssistantSettings:
    """GET /v1/operator/integrations/home-assistant over one Database."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def view(self) -> HomeAssistantStatesView:
        """One read-only snapshot; `heard_at` is the newest heard_at of home_assistant_entities."""
        with self._db.read_snapshot() as conn:
            return HomeAssistantStatesView(
                heard_at=last_heard_in(conn),
                entities=tuple(_entity(each) for each in heard_entities_in(conn, SOURCE)),
                refused=refused_in(conn, SOURCE))


def _entity(each: HeardEntity) -> HeardEntityView:
    if each.kind == "light":
        return HeardLightView(entity=each.entity, name=each.name, reports=each.reports, heard_at=each.heard_at)
    return HeardSensorView(entity=each.entity, name=each.name, reports=each.reports, heard_at=each.heard_at)


__all__ = ["PgHomeAssistantSettings"]
