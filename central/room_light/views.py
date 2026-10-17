"""House › Lights' operator API shapes: one plain model per answer. Instants are Central's clock
(seconds since the epoch). The routes are central/room_light_routes.py's."""
from __future__ import annotations

from typing import Literal

from central.room_light.model import ReportRefusal, RoomLight, Sensor, SensorId, Source
from contracts.models import Identifier, Instant, Model
from contracts.room_light import Reading, RoomLightEntry, RoomLightId

DrawnState = Literal["drawn", "behind", "not-following", "not-reported"]


class RoomLightView(Model):
    """One room light: its setup, its entry in the document now, when Central last heard its
    reporter (None: never, or nobody reports it), and the Frames whose lighting names it."""
    id: RoomLightId
    light: RoomLight
    entry: RoomLightEntry
    heard_at: Instant | None
    named_by: tuple[Identifier, ...]


class SensorView(Model):
    """One Sensor: its setup, its reference's last reading (None: never heard), when Central heard it,
    and the room light it measures (None: none)."""
    id: SensorId
    sensor: Sensor
    reading: Reading | None
    heard_at: Instant | None
    reports: RoomLightId | None


class RoomLightsView(Model):
    """GET /v1/operator/house/lights: every room light, in the house's order, and every Sensor."""
    lights: tuple[RoomLightView, ...]
    sensors: tuple[SensorView, ...]


class PlayerDrawnView(Model):
    """What one Player last said it drew its Frames with, judged against the document now."""
    player_id: Identifier
    state: DrawnState
    reported_at: Instant | None


class DrawnView(Model):
    """GET /v1/operator/house/lights/drawn: every Player that reported."""
    players: tuple[PlayerDrawnView, ...]


class RefusedReportView(Model):
    """A report a source could not record: what it was about, why and when."""
    source: Source
    address: str
    reason: ReportRefusal
    at: Instant


class RefusedReportsView(Model):
    """GET /v1/operator/house/refused-reports: the newest refused reports, newest first."""
    refused: tuple[RefusedReportView, ...]


__all__ = ["DrawnState", "DrawnView", "PlayerDrawnView", "RefusedReportView", "RefusedReportsView", "RoomLightView",
           "RoomLightsView", "SensorView"]
