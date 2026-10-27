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
  (an rgb colour only on a reported light, a reading only on a measured light). Each entry carries
  the light's colour as stated (real kelvin, or a bulb's rgb) and its brightness as last stated:
  Central chose the source and kept the last stated values (central/room_light/policy.py), the Pi
  bounds and rounds a kelvin to what it draws (player/geometry.py). A reported or measured entry
  also carries `doubt`: Central's own two lines (`CentralWords`) when it cannot stand behind the
  light's brightness now, else None. Only the words travel: why (can't read, can't tell, link
  closed), since when, and colour doubts stay on Central (central/room_light/model.py `Doubt`), so a
  new reason needs no Pi install. The Pi never reads the words; its Player judges whether a doubt
  stops its Frame's picture and passes the words on verbatim (contracts/node_impeded.py). It never rides the Player's
  control State: a light switching ten times changes this document ten times and the control
  State's delivery not at all.
- A **light sensor reading** (HostCore -> Central, the host line's state bucket, key
  LIGHT_SENSOR_KEY): what the Pi's one wired light sensor reads, Measured(lux) or Unavailable. A Pi
  with no sensor writes no key.
- **What the Player drew with** (the Player -> Central, on its control link, ROOM_LIGHTS_DRAWN_PATH):
  the digest of the room-lights document its Frames were last drawn with and whether its reader is
  following WALL. Central judges it against the document it wrote.

Rules this module owns (one home each): the room light id (RoomLightId), the lux reading's range
(Lux, LUX_MAX), a reading (Reading: a Sensor's lux or that it cannot tell), the colour, state and
entry vocabularies with their ranges (a stated kelvin's is contracts/kelvin.py's), the shapes above,
their caps, keys, codecs and the document's digest. A document state is total: absent and
`NotReported` add no light, so arrival order needs no invariant. `Unavailable` is a reporter's word
only: the document carries the last stated value instead (Central keeps it), or NotReported when
none was ever stated.

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

from pydantic import AfterValidator, Field, model_validator

from contracts.kelvin import KELVIN_STATED_MAX, KELVIN_STATED_MIN
from contracts.wire_codec import encode
from contracts.wire_model import Model, decoded

ROOM_LIGHT_ID_PATTERN: Final = r"[a-z0-9][a-z0-9-]{0,31}"   # "bookshelf-spot", "overheads", "daylight"
ROOM_LIGHTS_MAX: Final = 32                     # room lights in one house
LUX_MAX: Final = 200_000.0                      # above direct sun (about 120,000 lx)

ROOM_LIGHTS_KEY: Final = "room-lights"          # the WALL key of the room-lights document
# Its largest encoding: ROOM_LIGHTS_MAX lights at their longest, each with CentralWords of
# CENTRAL_LINE_MAX four-byte characters a line (about 7 KiB without words, about 26 KiB of words).
ROOM_LIGHTS_BYTES: Final = 40960
CENTRAL_LINE_MAX: Final = 96                    # characters in one of Central's two lines
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


# ---- Colour: as stated, in real units; the Pi bounds a kelvin to what it draws ----

StatedKelvin = Annotated[float, Field(ge=KELVIN_STATED_MIN, le=KELVIN_STATED_MAX, allow_inf_nan=False,
                                      title="StatedKelvin")]


class KelvinColour(Model):
    """A white light's colour temperature, in kelvin as its source stated it (a typed kelvin, a
    colour-temperature sensor, a bulb in its white mode). Never bounded to the table here: the Pi
    draws it at player/geometry.py `drawn_kelvin`."""
    kind: Literal["kelvin"] = "kelvin"
    kelvin: StatedKelvin


class RgbColour(Model):
    """A colour bulb's colour as it stated it in a colour mode: each channel's level as a share of
    the brightest (sRGB-encoded, 0..1; Home Assistant's rgb_color / 255), its brightness apart."""
    kind: Literal["rgb"] = "rgb"
    rgb: Rgb

    @model_validator(mode="after")
    def lit(self) -> RgbColour:
        """ValueError unless the channels sum above 0."""
        if sum(self.rgb) <= 0:
            raise ValueError("an rgb colour's channels sum above 0")
        return self


LightColour = Annotated[KelvinColour | RgbColour, Field(discriminator="kind", title="LightColour")]


# ---- States: what a reporter stated about brightness; full state, never a delta ----

class Dimmer(Model):
    """A dimmable light: on or off and its brightness, the share of its full light output (linear
    light; an integration's own scale is its adapter's to convert). Off is a stated value: it adds no
    light."""
    kind: Literal["dimmer"] = "dimmer"
    on: bool
    brightness: Share


class Measured(Model):
    """A Sensor's reading, in lux at its own place."""
    kind: Literal["measured"] = "measured"
    lux: Lux


class Unavailable(Model):
    """Its reporter said it cannot tell (an integration's unavailable, a failing sensor). A word only:
    the last stated value stands (central/room_light/policy.py); never in the document."""
    kind: Literal["unavailable"] = "unavailable"


class NotReported(Model):
    """Its reporter never stated a value. Adds no light."""
    kind: Literal["not-reported"] = "not-reported"


# What a Sensor last said: its lux, or that it cannot tell (the Pi's light sensor's wire, and Central's word).
Reading = Annotated[Measured | Unavailable, Field(discriminator="kind", title="Reading")]


# ---- Central's words: why Central can't stand behind a light's brightness now ----

def _printable(line: str) -> str:
    """ValueError unless every character of `line` is printable (no newline, no control)."""
    if not line.isprintable():
        raise ValueError("a line of Central's words is printable")
    return line


CentralLine = Annotated[str, Field(min_length=1, max_length=CENTRAL_LINE_MAX, title="CentralLine"),
                        AfterValidator(_printable)]


class CentralWords(Model):
    """Central's two lines about a light it can't stand behind (central/room_light/policy.py words
    them): `line1` what is wrong, `line2` what the Frame is drawn with. They name the light, never an
    integration. Carried verbatim to the Frame (Central's card), never reworded on the Pi."""
    line1: CentralLine
    line2: CentralLine


# ---- Entries: one per reporter kind, so mismatched colour and state pairs cannot be built ----

class FixedEntry(Model):
    """A light nobody reports: always on, at full, at its colour (typed, or its colour sensor's)."""
    kind: Literal["fixed"] = "fixed"
    colour: KelvinColour


class ReportedEntry(Model):
    """A light an integration reports: its last stated brightness and its colour (typed, or its bulb's
    own as last stated: kelvin or rgb), and Central's words when it can't stand behind that
    brightness now (None: it can). The last stated value stands under a doubt."""
    kind: Literal["reported"] = "reported"
    colour: LightColour
    state: Annotated[Dimmer | NotReported, Field(discriminator="kind")]
    doubt: CentralWords | None


class MeasuredEntry(Model):
    """A light one Sensor reports: its state is that Sensor's last stated reading; its colour typed,
    or its colour sensor's; and Central's words when it can't stand behind that reading now (None:
    it can)."""
    kind: Literal["measured"] = "measured"
    colour: KelvinColour
    state: Annotated[Measured | NotReported, Field(discriminator="kind")]
    doubt: CentralWords | None


RoomLightEntry = Annotated[FixedEntry | ReportedEntry | MeasuredEntry, Field(discriminator="kind")]


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
    "CENTRAL_LINE_MAX", "CentralLine", "CentralWords", "LIGHT_SENSOR_BYTES", "LIGHT_SENSOR_KEY", "LUX_MAX", "ROOM_LIGHTS_BYTES", "ROOM_LIGHTS_DRAWN_BYTES",
    "ROOM_LIGHTS_DRAWN_LEASE_SECONDS", "ROOM_LIGHTS_DRAWN_REPEAT_SECONDS",
    "ROOM_LIGHTS_DRAWN_PATH", "ROOM_LIGHTS_KEY", "ROOM_LIGHTS_MAX", "ROOM_LIGHT_ID_PATTERN", "Dimmer",
    "DocumentDigest", "FixedEntry", "KelvinColour", "LightColour", "LightSensorReading", "Lux", "Measured",
    "MeasuredEntry", "NotReported", "Reading", "ReportedEntry", "Rgb", "RgbColour", "RoomLightEntry", "RoomLightId",
    "RoomLightsDocument", "RoomLightsDrawn", "Share", "StatedKelvin", "Unavailable", "decode_light_sensor",
    "decode_room_lights", "encode_light_sensor", "encode_room_lights", "room_lights_digest",
]
