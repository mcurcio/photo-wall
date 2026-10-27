"""The Home Assistant settings page's answer (GET /v1/operator/integrations/home-assistant,
`HomeAssistantStatesView`): Galleria's version beside Central's, when Central last heard Home
Assistant's states, every entity Home Assistant published or a room light, Sensor or colour sensor
names, and the states Central refused. Every entity here is an address of SOURCE.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from central.room_light.model import ColourSensorId, RoomLightId, SensorId
from central.room_light.views import RefusedReportView
from contracts.home_assistant import EntityId, EntityKind, EntityName
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


class HeardColourSensorView(Model):
    """A colour-temperature entity Home Assistant published or a colour sensor names: its friendly
    name as last published (None: never published), the colour sensor it reports (None: none yet)
    and when Central last heard it (None: never)."""
    kind: Literal["colour-temperature"] = "colour-temperature"
    entity: EntityId
    name: EntityName | None
    reports: ColourSensorId | None
    heard_at: Instant | None


HeardEntityView = Annotated[HeardLightView | HeardSensorView | HeardColourSensorView,
                            Field(discriminator="kind", title="HeardEntityView")]


class GalleriaView(Model):
    """Galleria's version beside Central's: `version` and `kinds` as its last birth stated them (None
    and (): no birth heard, a Galleria older than colour sensors), when Central heard it, Central's
    own version, and whether the page asks to update Galleria (`update`: Galleria is older than
    Central, or stated no birth Central reads; the rule is central/infra/home_assistant/advice.py
    `galleria_advice`)."""
    version: str | None
    kinds: tuple[EntityKind, ...]
    heard_at: Instant | None
    central_version: str
    update: bool


class HomeAssistantStatesView(Model):
    """GET /v1/operator/integrations/home-assistant: Galleria's version line, when Central last heard
    any state from Home Assistant ("states last heard"; None: never), every entity (newest heard
    first), and this source's entries in the one refusal log (newest first; each address is the
    entity id)."""
    galleria: GalleriaView
    heard_at: Instant | None
    entities: tuple[HeardEntityView, ...]
    refused: tuple[RefusedReportView, ...]


__all__ = ["GalleriaView", "HeardColourSensorView", "HeardEntityView", "HeardLightView", "HeardSensorView",
           "HomeAssistantStatesView"]
