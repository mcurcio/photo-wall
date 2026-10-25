"""The Home Assistant settings page's read: Galleria's version beside Central's, when Central last
heard Home Assistant's states, every entity Home Assistant published or a room light, Sensor or
colour sensor names, and the states Central refused (the one refusal log, this source's entries)."""
from __future__ import annotations

from central.db import Database
from central.infra.home_assistant.references import SOURCE
from central.infra.home_assistant.views import (
    GalleriaView,
    HeardColourSensorView,
    HeardEntityView,
    HeardLightView,
    HeardSensorView,
    HomeAssistantStatesView,
)
from central.infra.room_light_store import (
    HeardEntity,
    galleria_birth_in,
    heard_entities_in,
    last_heard_in,
    refused_in,
)
from contracts.home_assistant import GalleriaBirth


class PgHomeAssistantSettings:
    """GET /v1/operator/integrations/home-assistant over one Database."""

    def __init__(self, db: Database, central_version: str) -> None:
        """`central_version`: Central's running version, as its hub states it (CentralBirth)."""
        self._db = db
        self._central_version = central_version

    def view(self) -> HomeAssistantStatesView:
        """One read-only snapshot; `heard_at` is the newest heard_at of home_assistant_entities."""
        with self._db.read_snapshot() as conn:
            birth = galleria_birth_in(conn)
            return HomeAssistantStatesView(
                galleria=galleria_view(birth, self._central_version),
                heard_at=last_heard_in(conn),
                entities=tuple(_entity(each) for each in heard_entities_in(conn, SOURCE)),
                refused=refused_in(conn, SOURCE))


def galleria_view(birth: tuple[GalleriaBirth, float] | None, central_version: str) -> GalleriaView:
    """Galleria's line: its last birth (and when heard) beside `central_version`; `update` while it
    cannot send colour sensors (no birth, or "colour-temperature" not among its kinds). Pure."""
    if birth is None:
        return GalleriaView(version=None, kinds=(), heard_at=None, central_version=central_version, update=True)
    stated, heard_at = birth
    return GalleriaView(version=stated.version, kinds=stated.kinds, heard_at=heard_at,
                        central_version=central_version, update="colour-temperature" not in stated.kinds)


def _entity(each: HeardEntity) -> HeardEntityView:
    if each.kind == "light":
        return HeardLightView(entity=each.entity, name=each.name, reports=each.reports, heard_at=each.heard_at)
    if each.kind == "illuminance":
        return HeardSensorView(entity=each.entity, name=each.name, reports=each.reports, heard_at=each.heard_at)
    return HeardColourSensorView(entity=each.entity, name=each.name, reports=each.reports, heard_at=each.heard_at)


__all__ = ["PgHomeAssistantSettings", "galleria_view"]
