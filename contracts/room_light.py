"""Room lights on the wall: the wire facts every side reads. Contracts and pydantic only: the Pi runs
it.

A **room light** is a real light in the house (daylight, the overheads, the bookshelf spotlight).
Photo Wall only observes it: Central keeps which room lights exist and who reports each
(central/room_light/), and every Frame names the room lights it sees (contracts/frame_lighting.py).

Three wire shapes:

- The **room-lights document** (Central -> every Pi, the hub's WALL stream, key ROOM_LIGHTS_KEY;
  every Node keeps a mirror of WALL, so a Pi keeps drawing with the last states while Central is
  away): `change`, which rises with any change below it; `lights`, every room light by id, each an
  entry of its reporter's kind, so a colour and a state that do not belong together cannot be built
  (a reported colour only on a colour light, a reading only on a measured light). It never rides
  the Player's control State: a light switching ten times changes this document ten times and the
  control State's delivery not at all.
- A **light sensor reading** (HostCore -> Central, the host line's state bucket, key
  LIGHT_SENSOR_KEY): what the Pi's one wired light sensor reads, Measured(lux) or Unavailable. A Pi
  with no sensor writes no key.
- **What the Player drew with** (the Player -> Central, on its control link, ROOM_LIGHTS_DRAWN_PATH):
  the digest of the room-lights document its Frames were last drawn with and whether its reader is
  following WALL. Central judges it against the document it wrote.

Rules this module owns (one home each): the room light id (RoomLightId), the lux reading's range
(Lux, LUX_MAX), a light's word and a reading (LightWord, Reading), the colour, state and entry
vocabularies with their ranges, the shapes above, their
caps, keys, codecs and the document's digest. A state is total: absent, `NotReported` and
`Unavailable` all add no light, so arrival order needs no invariant.

Rules it does NOT own: which room lights and Sensors exist and who reports each, and how an
integration's message becomes a state (central/); how a state becomes light on a Frame
(player/geometry.py `light_on_frame`, the scalar reference); a Frame's view of a room light
(contracts/frame_lighting.py).

Decoding is strict (pydantic's strict mode over contracts.strict_json's rules: no duplicate keys,
no NaN, the byte cap before parsing): no bool for a number, no string for a number, exactly each
shape's fields.
"""
from __future__ import annotations

import hashlib
from typing import Annotated, Final, Literal

from pydantic import Field, model_validator

from contracts.kelvin import KELVIN_TABLE_MAX, KELVIN_TABLE_MIN, KELVIN_TABLE_STEP
from contracts.wire_codec import encode
from contracts.wire_model import Model, decoded

ROOM_LIGHT_ID_PATTERN: Final = r"[a-z0-9][a-z0-9-]{0,31}"   # "bookshelf-spot", "overheads", "daylight"
ROOM_LIGHTS_MAX: Final = 32                     # room lights in one house
LUX_MAX: Final = 200_000.0                      # above direct sun (about 120,000 lx)

ROOM_LIGHTS_KEY: Final = "room-lights"          # the WALL key of the room-lights document
ROOM_LIGHTS_BYTES: Final = 16384                # its largest encoding (ROOM_LIGHTS_MAX lights at their longest fit)
LIGHT_SENSOR_KEY: Final = "light-sensor"        # the host line's state key of LightSensorReading
LIGHT_SENSOR_BYTES: Final = 64
ROOM_LIGHTS_DRAWN_PATH: Final = "/v1/player/room-lights-drawn"   # PUT RoomLightsDrawn, the Player's control link
ROOM_LIGHTS_DRAWN_BYTES: Final = 256
ROOM_LIGHTS_DRAWN_REPEAT_SECONDS: Final = 30.0  # the Player repeats its report at least this often, and on change
ROOM_LIGHTS_DRAWN_LEASE_SECONDS: Final = 3 * ROOM_LIGHTS_DRAWN_REPEAT_SECONDS   # older on Central's clock: not reported

RoomLightId = Annotated[str, Field(pattern=rf"^{ROOM_LIGHT_ID_PATTERN}$", title="RoomLightId")]
Lux = Annotated[float, Field(ge=0, le=LUX_MAX, allow_inf_nan=False, title="Lux")]   # a reading: 0..LUX_MAX
Share = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Rgb = tuple[Share, Share, Share]
DocumentDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", title="DocumentDigest")]


# ---- Colour: one writer each, configured (a fixed kelvin) or reported by the light itself ----

class FixedColour(Model):
    """A white light of one colour temperature (a white bulb, daylight): contracts/kelvin.py's row."""
    kind: Literal["fixed"] = "fixed"
    kelvin: int = Field(ge=KELVIN_TABLE_MIN, le=KELVIN_TABLE_MAX, multiple_of=KELVIN_TABLE_STEP)


class ReportedColour(Model):
    """A light that reports its colour: each screen channel's share of its white lux, read once."""
    kind: Literal["reported"] = "reported"
    channel_shares: Rgb

    @model_validator(mode="after")
    def lit(self) -> ReportedColour:
        """ValueError unless the shares sum above 0."""
        if sum(self.channel_shares) <= 0:
            raise ValueError("a reported colour's shares sum above 0")
        return self


# ---- States: the last thing a room light's reporter said; full state, never a delta ----

class Dimmer(Model):
    """A white dimmable light: on or off and its brightness, the share of its full light output
    (linear light; an integration's own scale is its adapter's to convert)."""
    kind: Literal["dimmer"] = "dimmer"
    on: bool
    brightness: Share


class ColourDimmer(Model):
    """A colour light: on or off, its brightness, and its drive per channel (each 0..1, linear)."""
    kind: Literal["colour-dimmer"] = "colour-dimmer"
    on: bool
    brightness: Share
    rgb: Rgb


class Measured(Model):
    """A Sensor's reading, in lux at its own place."""
    kind: Literal["measured"] = "measured"
    lux: Lux


class Unavailable(Model):
    """Its reporter said it cannot tell (an integration's unavailable, a failing sensor). Adds no light."""
    kind: Literal["unavailable"] = "unavailable"


class NotReported(Model):
    """Nothing heard from its reporter yet. Adds no light."""
    kind: Literal["not-reported"] = "not-reported"


# What a reporter last said, by kind: a light's word (a white or colour light's state, or that it cannot
# tell) and a reading (a sensor's lux, or that it cannot tell).
LightWord = Annotated[Dimmer | ColourDimmer | Unavailable, Field(discriminator="kind", title="LightWord")]
Reading = Annotated[Measured | Unavailable, Field(discriminator="kind", title="Reading")]


# ---- Entries: one per reporter kind, so mismatched colour and state pairs cannot be built ----

class FixedEntry(Model):
    """A light nobody reports: always on, at full."""
    kind: Literal["fixed"] = "fixed"
    colour: FixedColour


class WhiteEntry(Model):
    """A white light an integration reports as a dimmer."""
    kind: Literal["white"] = "white"
    colour: FixedColour
    state: Annotated[Dimmer | Unavailable | NotReported, Field(discriminator="kind")]


class ColourEntry(Model):
    """A colour light an integration reports with its drive; its colour is the light's own."""
    kind: Literal["colour"] = "colour"
    colour: ReportedColour
    state: Annotated[ColourDimmer | Unavailable | NotReported, Field(discriminator="kind")]


class MeasuredEntry(Model):
    """A light one Sensor reports: its state is that Sensor's reading."""
    kind: Literal["measured"] = "measured"
    colour: FixedColour
    state: Annotated[Measured | Unavailable | NotReported, Field(discriminator="kind")]


RoomLightEntry = Annotated[FixedEntry | WhiteEntry | ColourEntry | MeasuredEntry, Field(discriminator="kind")]


class RoomLightsDocument(Model):
    """Every room light's entry (Central -> every Pi, WALL key ROOM_LIGHTS_KEY). `change` rises with
    any change to `lights`. A Frame light whose room light is absent here adds no light. Treat
    `lights` as read-only."""
    change: int = Field(ge=1)
    lights: dict[RoomLightId, RoomLightEntry] = Field(max_length=ROOM_LIGHTS_MAX)


class LightSensorReading(Model):
    """What the Pi's one wired light sensor reads (HostCore -> Central)."""
    reading: Reading


class RoomLightsDrawn(Model):
    """What the Player last drew its Frames with (the Player -> Central, PUT ROOM_LIGHTS_DRAWN_PATH,
    level state, repeated): `drawn`, the room_lights_digest of that document (None: none drawn yet
    since the Player started); `following`, whether its reader is following the Node's WALL mirror
    now (False: the bus is up and the mirror cannot be read, or the bus is away)."""
    drawn: DocumentDigest | None
    following: bool


# ---- Codecs: canonical JSON (contracts.wire_codec), strict decoding ----

def encode_room_lights(document: RoomLightsDocument) -> bytes:
    """Canonical JSON of at most ROOM_LIGHTS_BYTES; ValueError("room_lights_too_large") past it
    (never for a valid document)."""
    return encode(document.model_dump(mode="json"), ROOM_LIGHTS_BYTES, "room_lights_too_large")


def decode_room_lights(raw: bytes) -> RoomLightsDocument:
    """The document `raw` encodes; ValueError("room_lights_document") for anything else."""
    return decoded(RoomLightsDocument, raw, ROOM_LIGHTS_BYTES, "room_lights_document")


def room_lights_digest(document: RoomLightsDocument) -> str:
    """The sha256 hex of `encode_room_lights(document)`: what the Player reports it drew with and
    what Central judges it against. Content-keyed: equal documents, equal digests, whatever wrote
    them."""
    return hashlib.sha256(encode_room_lights(document)).hexdigest()


def encode_light_sensor(reading: LightSensorReading) -> bytes:
    """Canonical JSON of at most LIGHT_SENSOR_BYTES."""
    return encode(reading.model_dump(mode="json"), LIGHT_SENSOR_BYTES, "light_sensor_reading_too_large")


def decode_light_sensor(raw: bytes) -> LightSensorReading:
    """The reading `raw` encodes; ValueError("light_sensor_reading") for anything else."""
    return decoded(LightSensorReading, raw, LIGHT_SENSOR_BYTES, "light_sensor_reading")


__all__ = [
    "LIGHT_SENSOR_BYTES", "LIGHT_SENSOR_KEY", "LUX_MAX", "ROOM_LIGHTS_BYTES", "ROOM_LIGHTS_DRAWN_BYTES",
    "ROOM_LIGHTS_DRAWN_LEASE_SECONDS", "ROOM_LIGHTS_DRAWN_REPEAT_SECONDS",
    "ROOM_LIGHTS_DRAWN_PATH", "ROOM_LIGHTS_KEY", "ROOM_LIGHTS_MAX", "ROOM_LIGHT_ID_PATTERN", "ColourDimmer",
    "ColourEntry", "Dimmer", "DocumentDigest", "FixedColour", "FixedEntry", "LightSensorReading", "LightWord", "Lux",
    "Measured", "MeasuredEntry", "NotReported", "Reading", "ReportedColour", "Rgb", "RoomLightEntry", "RoomLightId",
    "RoomLightsDocument", "RoomLightsDrawn", "Share", "Unavailable", "WhiteEntry", "decode_light_sensor",
    "decode_room_lights", "encode_light_sensor", "encode_room_lights", "room_lights_digest",
]
