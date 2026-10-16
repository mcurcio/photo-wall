"""House > Lights' operator API shapes (decision 0028): one plain model per body and answer. The
console codes against these (central/console); Central's routes (central/room_light_routes.py)
answer with them. Instants are Central's clock (seconds since the epoch).

Routes (all admin; a refusal is 409 {"error": SetupRefusal value}, a body outside its shape 422):
- GET    /v1/operator/house/lights                  -> RoomLightsView
- PUT    /v1/operator/house/lights/{light_id}       RoomLightSave -> RoomLightView   (create or replace)
- DELETE /v1/operator/house/lights/{light_id}       -> 204
- PUT    /v1/operator/house/sensors/{sensor_id}     SensorSave -> SensorView         (create or replace)
- DELETE /v1/operator/house/sensors/{sensor_id}     -> 204
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from central.room_light.model import Colour, Name, Reporter, RoomLightId, SensorId, SensorPlace
from contracts.models import Identifier, Instant, Model
from contracts.room_light import Dimmer, Measured, NotReported, Unavailable

StateView = Annotated[Dimmer | Measured | Unavailable | NotReported, Field(discriminator="kind")]


class RoomLightSave(Model):
    """PUT /v1/operator/house/lights/{light_id}: the light's name, reporter and colour."""
    name: Name
    reporter: Reporter
    colour: Colour


class SensorSave(Model):
    """PUT /v1/operator/house/sensors/{sensor_id}."""
    name: Name
    at: SensorPlace


class RoomLightView(Model):
    """One room light: its setup, its last state and when Central recorded it (None: never; a fixed
    light is always FULL_ON with heard_at None), and the Frames whose lighting names it."""
    id: RoomLightId
    name: Name
    reporter: Reporter
    colour: Colour
    state: StateView
    heard_at: Instant | None
    named_by: tuple[Identifier, ...]


class SensorView(Model):
    """One Sensor: its setup, its last reading (lux; "unavailable"; None: never heard), when Central
    recorded it, and the room light it reports (None: none)."""
    id: SensorId
    name: Name
    at: SensorPlace
    reading: float | Literal["unavailable"] | None
    heard_at: Instant | None
    reports: RoomLightId | None


class HomeAssistantLinkView(Model):
    """The worker's broker link: "not-configured" (no PHOTO_WALL_MQTT_URL), "connected", or "away"
    (configured, not connected now) since `since` (Central's clock; None before the worker's first
    word)."""
    state: Literal["not-configured", "connected", "away"]
    since: Instant | None


class RefusedMessageView(Model):
    """A message the Home Assistant link did not record, kept with the reason (central/room_light/
    model.py `Refusal`'s values, or the parse's reason): its topic, why, and when Central refused it."""
    topic: str
    reason: str
    at: Instant


class RoomLightsView(Model):
    """GET /v1/operator/house/lights: every room light and Sensor, the Home Assistant link, and its
    newest refused messages (at most REFUSALS_KEPT, newest first)."""
    lights: tuple[RoomLightView, ...]
    sensors: tuple[SensorView, ...]
    home_assistant: HomeAssistantLinkView
    refused: tuple[RefusedMessageView, ...]


__all__ = ["HomeAssistantLinkView", "RefusedMessageView", "RoomLightSave", "RoomLightView", "RoomLightsView", "SensorSave", "SensorView",
           "StateView"]
