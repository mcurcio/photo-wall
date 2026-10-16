"""Room lights' vocabulary (decision 0028): room lights and their one reporter, Sensors, what Home
Assistant sends, and the answers of recording and setting up. Pure; frozen values only.

A room light's colour and state are the wire's (contracts/room_light.py): the House > Lights page,
the store and the room-lights document share one vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field

from contracts.models import Identifier, Model
from contracts.room_light import ROOM_LIGHT_ID_PATTERN, FixedColour, ReportedColour, RoomLightState

# Home Assistant's entity id: <domain>.<object_id> (https://developers.home-assistant.io, entity
# ids are lower-case ASCII letters, digits and underscores).
HOME_ASSISTANT_ENTITY_PATTERN: Final = r"[a-z0-9_]+\.[a-z0-9_]+"
ROOM_LIGHT_NAME_MAX: Final = 64
SENSORS_MAX: Final = 32

# The Home Assistant link's topics (the shipped automation publishes each retained, full state).
LIGHT_TOPIC_PREFIX: Final = "photowall/light/"              # + entity id: a LightStateMessage
ILLUMINANCE_TOPIC_PREFIX: Final = "photowall/illuminance/"  # + entity id: an IlluminanceMessage
SUBSCRIPTION: Final = "photowall/#"                         # the worker's one subscription
HOME_ASSISTANT_MESSAGE_BYTES: Final = 1024                  # a larger payload is refused unread

RoomLightId = Annotated[str, Field(pattern=rf"^{ROOM_LIGHT_ID_PATTERN}$")]
SensorId = Annotated[str, Field(pattern=rf"^{ROOM_LIGHT_ID_PATTERN}$")]
EntityId = Annotated[str, Field(pattern=rf"^{HOME_ASSISTANT_ENTITY_PATTERN}$", max_length=255)]
Name = Annotated[str, Field(min_length=1, max_length=ROOM_LIGHT_NAME_MAX)]


# ---- Who writes a room light's state: exactly one reporter (design rule 1) ----

class HomeAssistantLight(Model):
    """Its Home Assistant light entity reports it: on, brightness and (for a bulb with reported
    colour) rgb."""
    kind: Literal["home-assistant-light"] = "home-assistant-light"
    entity: EntityId


class SensorReporter(Model):
    """One Sensor reports it: its state is that Sensor's lux reading."""
    kind: Literal["sensor"] = "sensor"
    sensor: SensorId


class FixedReporter(Model):
    """Nobody reports it: always on, at full (contracts/room_light.py FULL_ON)."""
    kind: Literal["fixed"] = "fixed"


Reporter = Annotated[HomeAssistantLight | SensorReporter | FixedReporter, Field(discriminator="kind")]
Colour = Annotated[FixedColour | ReportedColour, Field(discriminator="kind")]


class RoomLight(Model):
    """A real light in the house: its name, its one reporter and its colour (configured: a fixed
    kelvin; or reported: an RGB bulb's channel shares, only for a Home Assistant light)."""
    id: RoomLightId
    name: Name
    reporter: Reporter
    colour: Colour


# ---- Sensors (requirements' Sensor): a light meter at one point; it reads the SUM there ----

class OnPlayer(Model):
    """The one meter wired to a Pi; HostCore reads it (contracts/room_light.py LIGHT_SENSOR_KEY)."""
    kind: Literal["pi"] = "pi"
    player_id: Identifier


class HomeAssistantSensor(Model):
    """An illuminance entity Home Assistant publishes."""
    kind: Literal["home-assistant"] = "home-assistant"
    entity: EntityId


SensorPlace = Annotated[OnPlayer | HomeAssistantSensor, Field(discriminator="kind")]


class Sensor(Model):
    id: SensorId
    name: Name
    at: SensorPlace


# ---- What Home Assistant sends (retained, full state: a repeat changes nothing) ----

@dataclass(frozen=True)
class LightStateMessage:
    """topic LIGHT_TOPIC_PREFIX + entity. `state`: Home Assistant's on/off; its unavailable and
    unknown both arrive as "unavailable". `brightness` 0..255 (None: Home Assistant sent none, as
    for an off light); `rgb` Home Assistant's rgb_color, 0..255 each (None: none sent)."""
    state: Literal["on", "off", "unavailable"]
    brightness: int | None
    rgb: tuple[int, int, int] | None


@dataclass(frozen=True)
class IlluminanceMessage:
    """topic ILLUMINANCE_TOPIC_PREFIX + entity: the entity's lux, or None for unavailable/unknown."""
    lux: float | None


# ---- Answers ----

class Refusal(StrEnum):
    """Why a report was not recorded (kept with the reason, never applied)."""
    UNKNOWN_ENTITY = "unknown-entity"           # no room light or Sensor names this entity
    NOT_ITS_REPORTER = "not-its-reporter"       # e.g. a Pi's reading for a Sensor on another Pi, or none configured
    OUT_OF_RANGE = "out-of-range"               # a payload outside its shape (contracts' ranges, the topic's form)


@dataclass(frozen=True)
class Refused:
    reason: Refusal


Recorded = Literal["recorded", "unchanged"] | Refused


class SetupRefusal(StrEnum):
    """Why a House > Lights change was refused (the route answers 409 {"error": <value>})."""
    NAMED_BY_A_FRAME = "named-by-a-frame"                      # remove the Frame lights first
    SENSOR_REPORTS_A_LIGHT = "sensor-reports-a-light"          # a Sensor reports at most one room light, and stays while it does
    REPORTED_COLOUR_NEEDS_HOME_ASSISTANT = "reported-colour-needs-home-assistant"   # only a Home Assistant light reports a colour
    ENTITY_REPORTS_ANOTHER = "entity-reports-another"          # one entity reports one room light or Sensor
    UNKNOWN_SENSOR = "unknown-sensor"
    UNKNOWN_ROOM_LIGHT = "unknown-room-light"
    TOO_MANY = "too-many"                                      # past ROOM_LIGHTS_MAX or SENSORS_MAX


class FrameLightingRefusal(StrEnum):
    """Why a Frame's lighting was refused against the room lights (the trial answers 422)."""
    UNKNOWN_ROOM_LIGHT = "trial_lighting_unknown_room_light"   # a Frame light names no room light
    REFERENCE_MISMATCH = "trial_lighting_reference_mismatch"   # dimmer-at-full on a measured light, or lux on a dimmer


@dataclass(frozen=True)
class Heard:
    """A room light's last state and when Central recorded it (Central's clock; None: never)."""
    state: RoomLightState
    heard_at: float | None


__all__ = [
    "HOME_ASSISTANT_ENTITY_PATTERN", "HOME_ASSISTANT_MESSAGE_BYTES", "ILLUMINANCE_TOPIC_PREFIX", "LIGHT_TOPIC_PREFIX",
    "ROOM_LIGHT_NAME_MAX", "SENSORS_MAX", "SUBSCRIPTION", "Colour", "EntityId", "FixedReporter",
    "FrameLightingRefusal", "Heard", "HomeAssistantLight", "HomeAssistantSensor", "IlluminanceMessage",
    "LightStateMessage", "Name", "OnPlayer", "Recorded", "Refusal", "Refused", "Reporter", "RoomLight",
    "RoomLightId", "Sensor", "SensorId", "SensorPlace", "SensorReporter", "SetupRefusal",
]
