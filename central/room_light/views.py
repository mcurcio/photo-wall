"""House › Lights' operator API shapes: one plain model per answer. Instants are Central's clock
(seconds since the epoch). The routes are central/room_light_routes.py's."""
from __future__ import annotations

from typing import Literal

from central.room_light.model import (
    ColourSensor,
    ColourSensorId,
    DoubtReason,
    ReportRefusal,
    RoomLight,
    Sensor,
    SensorId,
    Source,
)
from contracts.models import Identifier, Instant, Model
from contracts.room_light import KelvinColour, LightColour, Measured, RoomLightEntry, RoomLightId

DrawnState = Literal["drawn", "behind", "not-following", "not-reported"]
# Where a light's colour comes from now: the typed kelvin by choice; its source's last stated colour;
# the typed kelvin standing in because its source never stated one.
ColourFrom = Literal["typed", "following", "never-stated"]


class DoubtView(Model):
    """Why Central can't stand behind a reference's last word now, and since when (Central's clock).
    Every House › Lights view carries it (None: Central stands behind the last word)."""
    reason: DoubtReason
    since: Instant


class ColourLineView(Model):
    """A room light's colour line (House › Lights and a Frame's Lighting tab): the colour its entry
    carries (as stated, real kelvin or rgb; the console says "outside what the screen draws" against
    contracts/kelvin.py's table), where it comes from (`source`: the light's colour source's kind;
    `sensor`: the colour sensor it follows, None for any other source), why (`colour_from`), when its
    source stated it (None: typed, or never stated) and its source's doubt (None: Central
    stands behind it, or the colour is typed)."""
    colour: LightColour
    source: Literal["typed", "its-bulb", "colour-sensor"]
    sensor: ColourSensorId | None
    colour_from: ColourFrom
    stated_at: Instant | None
    doubt: DoubtView | None


class RoomLightView(Model):
    """One room light: its setup, its entry in the document now, when Central last heard its
    reporter (None: never, or nobody reports it), its brightness reference's doubt (None: Central
    stands behind it, or nobody reports it; the entry keeps the last stated brightness), its colour
    line, and the Frames whose lighting names it."""
    id: RoomLightId
    light: RoomLight
    entry: RoomLightEntry
    heard_at: Instant | None
    doubt: DoubtView | None
    colour: ColourLineView
    named_by: tuple[Identifier, ...]


class SensorView(Model):
    """One Sensor: its setup, its reference's last stated reading (None: never stated), when Central
    last heard it, its doubt (None: Central stands behind it), and the room light it measures
    (None: none)."""
    id: SensorId
    sensor: Sensor
    reading: Measured | None
    heard_at: Instant | None
    doubt: DoubtView | None
    reports: RoomLightId | None


class ColourSensorView(Model):
    """One colour sensor: its setup, its reference's last stated colour (None: never stated), when
    Central last heard it, its doubt (None: Central stands behind it), and the room lights that
    follow it."""
    id: ColourSensorId
    sensor: ColourSensor
    colour: KelvinColour | None
    heard_at: Instant | None
    doubt: DoubtView | None
    followed_by: tuple[RoomLightId, ...]


class RoomLightsView(Model):
    """GET /v1/operator/house/lights: every room light, in the house's order, every Sensor and every
    colour sensor."""
    lights: tuple[RoomLightView, ...]
    sensors: tuple[SensorView, ...]
    colour_sensors: tuple[ColourSensorView, ...]


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


__all__ = ["ColourFrom", "ColourLineView", "ColourSensorView", "DoubtView", "DrawnState", "DrawnView", "PlayerDrawnView",
           "RefusedReportView", "RefusedReportsView", "RoomLightView", "RoomLightsView", "SensorView"]
