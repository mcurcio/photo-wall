"""Room lights on the wall: the wire facts every side reads (decision 0028). Stdlib and contracts
only: the Pi runs it.

A **room light** is a real light in the house (daylight, the overheads, the bookshelf spotlight).
Photo Wall only observes it: Central keeps which room lights exist and each one's one reporter
(central/room_light/), and every Frame names the room lights it sees (contracts/frame_lighting.py).

Two wire shapes:

- The **room-lights document** (Central -> every Pi, the hub's WALL stream, key ROOM_LIGHTS_KEY;
  every Node keeps a mirror of WALL, so a Pi keeps drawing with the last states while Central is
  away): `change`, which rises with any change below it; `lights`, every room light by id, each
  with its colour (a fixed kelvin, or the channel shares of a bulb that reports its colour) and its
  last state. It never rides the Player's control State (design rule 2): a light switching ten
  times changes this document ten times and the control State's delivery not at all.
- A **light sensor reading** (HostCore -> Central, the host line's state bucket, key
  LIGHT_SENSOR_KEY): the lux the Pi's one wired meter reads, or None when the reader fails. A Pi
  with no meter writes no key.

Rules this module owns (one home each): the room light id's form (ROOM_LIGHT_ID_PATTERN), the
lux bound (LUX_MAX), the state and colour vocabularies with their ranges, the two wire shapes,
their caps, keys and codecs. A state is total: absent, `NotReported` and `Unavailable` all add no
light (design rule 2), so arrival order needs no invariant.

Rules it does NOT own: which room lights and Sensors exist and who reports each, and turning a
Home Assistant message into a state (central/room_light/); how a state becomes light on a Frame
(player/geometry.py `light_on_frame`, the scalar reference); a Frame's view of a room light
(contracts/frame_lighting.py).
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from contracts.kelvin import KELVIN_TABLE_MAX, KELVIN_TABLE_MIN, KELVIN_TABLE_STEP
from contracts.wire_codec import decode, encode, fields_of, list_of, object_of, require, whole

ROOM_LIGHT_ID_PATTERN: Final = r"[a-z0-9][a-z0-9-]{0,31}"   # "bookshelf-spot", "overheads", "daylight"
ROOM_LIGHTS_MAX: Final = 32                     # room lights in one house
LUX_MAX: Final = 200_000.0                      # above direct sun (about 120,000 lx)

ROOM_LIGHTS_KEY: Final = "room-lights"          # the WALL key of the room-lights document
ROOM_LIGHTS_BYTES: Final = 16384                # its largest encoding (ROOM_LIGHTS_MAX lights at their longest fit)
LIGHT_SENSOR_KEY: Final = "light-sensor"        # the host line's state key of LightSensorReading
LIGHT_SENSOR_BYTES: Final = 64

_ROOM_LIGHT_ID: Final = re.compile(ROOM_LIGHT_ID_PATTERN)

Rgb = tuple[float, float, float]


def room_light_id(value: object) -> bool:
    """Whether `value` is a room light id: a str matching ROOM_LIGHT_ID_PATTERN whole."""
    return isinstance(value, str) and _ROOM_LIGHT_ID.fullmatch(value) is not None


def _number(value: object, minimum: float, maximum: float) -> bool:
    """A finite float or int (never a bool) in minimum..maximum."""
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def _shares(value: object) -> bool:
    """A 3-tuple of numbers in 0..1."""
    return isinstance(value, tuple) and len(value) == 3 and all(_number(item, 0, 1) for item in value)


# ---- Colour: one writer each (design rule 1): configured, or reported by the light itself ----

@dataclass(frozen=True)
class FixedColour:
    """A white light of one colour temperature (a white bulb, daylight): contracts/kelvin.py's row."""
    kelvin: int
    kind: Literal["fixed"] = "fixed"

    def __post_init__(self) -> None:
        """ValueError("room_light_colour") unless kelvin is an int in KELVIN_TABLE_MIN..KELVIN_TABLE_MAX
        in KELVIN_TABLE_STEP and kind is "fixed"."""
        require(self.kind == "fixed" and whole(self.kelvin, KELVIN_TABLE_MIN, KELVIN_TABLE_MAX)
                and (self.kelvin - KELVIN_TABLE_MIN) % KELVIN_TABLE_STEP == 0, "room_light_colour")


@dataclass(frozen=True)
class ReportedColour:
    """An RGB bulb that reports its colour: each screen channel's share of the bulb's white lux, read
    once (its drive in a state is Home Assistant's rgb; design "How a state becomes light")."""
    channel_shares: Rgb
    kind: Literal["reported"] = "reported"

    def __post_init__(self) -> None:
        """ValueError("room_light_colour") unless channel_shares is a 3-tuple of numbers in 0..1 with
        a sum above 0 and kind is "reported"."""
        require(self.kind == "reported" and _shares(self.channel_shares) and sum(self.channel_shares) > 0,
                "room_light_colour")


RoomLightColour = FixedColour | ReportedColour


# ---- State: the last thing a room light's one reporter said; full state, never a delta ----

@dataclass(frozen=True)
class Dimmer:
    """A light Home Assistant reports (or a fixed light, always on at full): on or off, its brightness
    (Home Assistant's 0..255 / 255), and for a bulb that reports colour its rgb drive (0..255 / 255
    each; None for a white bulb)."""
    on: bool
    brightness: float
    rgb: Rgb | None = None
    kind: Literal["dimmer"] = "dimmer"

    def __post_init__(self) -> None:
        """ValueError("room_light_state") unless on is a bool, brightness a number in 0..1, rgb None
        or a 3-tuple of numbers in 0..1, and kind "dimmer"."""
        require(self.kind == "dimmer" and type(self.on) is bool and _number(self.brightness, 0, 1)
                and (self.rgb is None or _shares(self.rgb)), "room_light_state")


@dataclass(frozen=True)
class Measured:
    """A light a Sensor reports: the Sensor's lux reading, in its own place."""
    lux: float
    kind: Literal["measured"] = "measured"

    def __post_init__(self) -> None:
        """ValueError("room_light_state") unless lux is a number in 0..LUX_MAX and kind "measured"."""
        require(self.kind == "measured" and _number(self.lux, 0, LUX_MAX), "room_light_state")


@dataclass(frozen=True)
class Unavailable:
    """Its reporter said it cannot tell (Home Assistant's unavailable or unknown, a failing meter).
    Adds no light."""
    kind: Literal["unavailable"] = "unavailable"

    def __post_init__(self) -> None:
        """ValueError("room_light_state") unless kind is "unavailable"."""
        require(self.kind == "unavailable", "room_light_state")


@dataclass(frozen=True)
class NotReported:
    """Nothing heard from its reporter yet. Adds no light."""
    kind: Literal["not-reported"] = "not-reported"

    def __post_init__(self) -> None:
        """ValueError("room_light_state") unless kind is "not-reported"."""
        require(self.kind == "not-reported", "room_light_state")


RoomLightState = Dimmer | Measured | Unavailable | NotReported
FULL_ON: Final = Dimmer(on=True, brightness=1.0)   # a fixed room light's state: always on, at full


@dataclass(frozen=True)
class RoomLightEntry:
    """One room light as every Pi sees it: its colour and its last state."""
    colour: RoomLightColour
    state: RoomLightState

    def __post_init__(self) -> None:
        """ValueError("room_light_entry") unless colour is a FixedColour or ReportedColour and state
        one of the four states."""
        require(isinstance(self.colour, FixedColour | ReportedColour)
                and isinstance(self.state, Dimmer | Measured | Unavailable | NotReported), "room_light_entry")


@dataclass(frozen=True)
class RoomLightsDocument:
    """Every room light's colour and last state (Central -> every Pi, WALL key ROOM_LIGHTS_KEY).
    `change` rises with any change to `lights`. A Frame light whose room light is absent here adds
    no light. Treat `lights` as read-only."""
    change: int
    lights: Mapping[str, RoomLightEntry]

    def __post_init__(self) -> None:
        """ValueError("room_lights_document") unless change >= 1, lights is a Mapping of at most
        ROOM_LIGHTS_MAX room light ids to RoomLightEntry values."""
        require(whole(self.change, 1) and isinstance(self.lights, Mapping) and len(self.lights) <= ROOM_LIGHTS_MAX
                and all(room_light_id(key) and isinstance(entry, RoomLightEntry)
                        for key, entry in self.lights.items()), "room_lights_document")


@dataclass(frozen=True)
class LightSensorReading:
    """What the Pi's one wired light meter reads (HostCore -> Central): lux, or None when the
    reader fails (the meter is there and cannot be read)."""
    lux: float | None

    def __post_init__(self) -> None:
        """ValueError("light_sensor_reading") unless lux is None or a number in 0..LUX_MAX."""
        require(self.lux is None or _number(self.lux, 0, LUX_MAX), "light_sensor_reading")


# ---- Codecs: contracts.wire_codec's canonical JSON, strict decoding ----

def encode_room_lights(document: RoomLightsDocument) -> bytes:
    """Canonical JSON of at most ROOM_LIGHTS_BYTES; ValueError("room_lights_too_large") past it
    (never for a valid document)."""
    return encode(document, ROOM_LIGHTS_BYTES, "room_lights_too_large")


def decode_room_lights(raw: bytes) -> RoomLightsDocument:
    """The document `raw` encodes; ValueError("room_lights_document") for anything else."""
    def build(value: dict[str, Any]) -> RoomLightsDocument:
        return RoomLightsDocument(change=value["change"], lights={
            key: _entry(item) for key, item in object_of(value["lights"]).items()})

    return decode(raw, ROOM_LIGHTS_BYTES, RoomLightsDocument, build, "room_lights_document")


def encode_light_sensor(reading: LightSensorReading) -> bytes:
    """Canonical JSON of at most LIGHT_SENSOR_BYTES."""
    return encode(reading, LIGHT_SENSOR_BYTES, "light_sensor_reading_too_large")


def decode_light_sensor(raw: bytes) -> LightSensorReading:
    """The reading `raw` encodes; ValueError("light_sensor_reading") for anything else."""
    return decode(raw, LIGHT_SENSOR_BYTES, LightSensorReading, lambda value: LightSensorReading(**value),
                  "light_sensor_reading")


_STATES: Final = {"dimmer": Dimmer, "measured": Measured, "unavailable": Unavailable, "not-reported": NotReported}
_COLOURS: Final = {"fixed": FixedColour, "reported": ReportedColour}


def _tuple3(value: object) -> Rgb:
    items = list_of(value)
    require(len(items) == 3, "rgb")
    return (items[0], items[1], items[2])


def _of_kind(kinds: Mapping[str, type], value: object) -> Any:
    """The dataclass `value`'s "kind" names, built from exactly its fields; tuples from lists."""
    require(isinstance(value, dict) and value.get("kind") in kinds, "kind")
    cls = kinds[value["kind"]]
    fields = fields_of(cls, value)
    return cls(**{key: (_tuple3(item) if isinstance(item, list) else item) for key, item in fields.items()})


def _entry(value: object) -> RoomLightEntry:
    entry = fields_of(RoomLightEntry, value)
    return RoomLightEntry(colour=_of_kind(_COLOURS, entry["colour"]), state=_of_kind(_STATES, entry["state"]))


__all__ = [
    "FULL_ON", "LIGHT_SENSOR_BYTES", "LIGHT_SENSOR_KEY", "LUX_MAX", "ROOM_LIGHTS_BYTES", "ROOM_LIGHTS_KEY",
    "ROOM_LIGHTS_MAX", "ROOM_LIGHT_ID_PATTERN", "Dimmer", "FixedColour", "LightSensorReading", "Measured",
    "NotReported", "ReportedColour", "Rgb", "RoomLightColour", "RoomLightEntry", "RoomLightState",
    "RoomLightsDocument", "Unavailable", "decode_light_sensor", "decode_room_lights", "encode_light_sensor",
    "encode_room_lights", "room_light_id",
]
