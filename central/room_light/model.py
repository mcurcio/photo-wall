"""Room lights' vocabulary: the real lights of the house and who reports each, Sensors, colour
sensors, each light's colour source, the facts each reference last stated, and the answers of
recording and setting up. Pure; frozen values only.

A room light's id is the key of its mapping, never a field of the light. Who reports a light, a
Sensor or a colour sensor is an opaque reference, `(source, address)`: this package compares
references whole and never reads an address; each source's adapter owns its address's form.

A reference's facts stand until the reference states another (owner, 2026-10-25, Q1 and Q3: keep
the last stated colour and brightness through "unavailable"): per reference, its last stated
brightness (or lux reading, or colour), when it was stated, and separately whether it can't tell now
and since when. Colour is its own fact: a report that states no colour leaves the last stated one,
and no colour ever costs a light its on/off or brightness.

Colours, states and entries are the wire's (contracts/room_light.py): House › Lights, the store and
the room-lights document share one vocabulary.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field

from contracts.kelvin import KELVIN_TABLE_MAX, KELVIN_TABLE_MIN, KELVIN_TABLE_STEP
from contracts.models import Model
from contracts.room_light import (
    ROOM_LIGHT_ID_PATTERN,
    Dimmer,
    KelvinColour,
    LightColour,
    Measured,
    Reading,
    RoomLightId,
    Unavailable,
)

ROOM_LIGHT_NAME_MAX: Final = 64
SENSORS_MAX: Final = 32
COLOUR_SENSORS_MAX: Final = 32
SOURCE_PATTERN: Final = r"[a-z][a-z0-9-]{0,31}"     # a reporting source's name
ADDRESS_MAX: Final = 255

SensorId = Annotated[str, Field(pattern=rf"^{ROOM_LIGHT_ID_PATTERN}$", title="SensorId")]
ColourSensorId = Annotated[str, Field(pattern=rf"^{ROOM_LIGHT_ID_PATTERN}$", title="ColourSensorId")]
# A typed colour temperature: a row of contracts/kelvin.py's table, what the console's field steps by.
TypedKelvin = Annotated[int, Field(ge=KELVIN_TABLE_MIN, le=KELVIN_TABLE_MAX, multiple_of=KELVIN_TABLE_STEP,
                                   title="TypedKelvin")]
Name = Annotated[str, Field(min_length=1, max_length=ROOM_LIGHT_NAME_MAX)]
Source = Annotated[str, Field(pattern=rf"^{SOURCE_PATTERN}$", title="Source")]
Address = Annotated[str, Field(min_length=1, max_length=ADDRESS_MAX, title="Address")]


# ---- Who reports: opaque references ----

class ReporterRef(Model):
    """Who reports a room light's state: a source and that source's own name for the light."""
    source: Source
    address: Address


class SensorRef(Model):
    """Where a Sensor's readings come from: a source and that source's own name for the sensor."""
    source: Source
    address: Address


class ColourSensorRef(Model):
    """Where a colour sensor's colour temperatures come from: a source and that source's own name."""
    source: Source
    address: Address


# ---- Where a light's colour comes from: one choice per light kind (no light follows another's) ----

class TypedColour(Model):
    """The colour temperature the operator typed."""
    kind: Literal["typed"] = "typed"
    kelvin: TypedKelvin


class BulbColour(Model):
    """The bulb's own colour as it last stated it (kelvin in its white mode, rgb in a colour mode);
    `typed_kelvin` until it states one. A reported light's default (owner, 2026-10-25, Q2)."""
    kind: Literal["its-bulb"] = "its-bulb"
    typed_kelvin: TypedKelvin


class SensedColour(Model):
    """A colour sensor's last stated colour temperature; `typed_kelvin` until it states one."""
    kind: Literal["colour-sensor"] = "colour-sensor"
    sensor: ColourSensorId
    typed_kelvin: TypedKelvin


ReportedColourSource = Annotated[TypedColour | BulbColour, Field(discriminator="kind", title="ReportedColourSource")]
SensedColourSource = Annotated[TypedColour | SensedColour, Field(discriminator="kind", title="SensedColourSource")]


# ---- Room lights: one kind per reporter, so a colour source and a reporter that do not belong
# together cannot be built ----

class ReportedLight(Model):
    """A light its reporter reports: its brightness, and its colour when it follows its bulb."""
    kind: Literal["reported"] = "reported"
    name: Name
    reporter: ReporterRef
    colour: ReportedColourSource


class MeasuredLight(Model):
    """A light one Sensor measures: its state is that Sensor's reading."""
    kind: Literal["measured"] = "measured"
    name: Name
    sensor: SensorId
    colour: SensedColourSource


class FixedLight(Model):
    """A light nobody reports: always on, at full."""
    kind: Literal["fixed"] = "fixed"
    name: Name
    colour: SensedColourSource


RoomLight = Annotated[ReportedLight | MeasuredLight | FixedLight, Field(discriminator="kind")]


class Sensor(Model):
    """A light meter at one point; it reads the sum of every light there."""
    name: Name
    at: SensorRef


class ColourSensor(Model):
    """A colour-temperature sensor (a smart-home entity, never a Pi's light meter); several lights
    may follow it."""
    name: Name
    at: ColourSensorRef


# ---- What reporters said, and the facts kept by reference ----

class LightStated(Model):
    """What a light's reporter stated: on or off and its brightness, and its colour when its mode
    states one (None: off, or a brightness or on/off mode)."""
    kind: Literal["stated"] = "stated"
    state: Dimmer
    colour: LightColour | None


# What a reporter said, by kind: a light's report, a Sensor's reading (contracts/room_light.py
# Reading), a colour sensor's reading; each either a stated value or that it cannot tell.
LightReport = Annotated[LightStated | Unavailable, Field(discriminator="kind", title="LightReport")]
ColourReading = Annotated[KelvinColour | Unavailable, Field(discriminator="kind", title="ColourReading")]


@dataclass(frozen=True)
class Kept[T]:
    """A value a reference stated, and when Central heard it stated (Central's clock, epoch seconds)."""
    value: T
    at: float


@dataclass(frozen=True)
class LightFacts:
    """A light reference's facts: its last stated brightness and colour (None: never stated), since
    when it can't tell (None: it tells now) and when Central last heard it at all."""
    state: Kept[Dimmer] | None
    colour: Kept[LightColour] | None
    cant_tell_since: float | None
    heard_at: float


@dataclass(frozen=True)
class ReadingFacts:
    """A Sensor reference's facts: its last stated reading, since when it can't tell, last heard."""
    reading: Kept[Measured] | None
    cant_tell_since: float | None
    heard_at: float


@dataclass(frozen=True)
class ColourFacts:
    """A colour sensor reference's facts: its last stated colour, since when it can't tell, last heard."""
    colour: Kept[KelvinColour] | None
    cant_tell_since: float | None
    heard_at: float


@dataclass(frozen=True)
class Facts:
    """Every reference's kept facts, whether or not a room light, Sensor or colour sensor names it."""
    lights: Mapping[ReporterRef, LightFacts]
    readings: Mapping[SensorRef, ReadingFacts]
    colours: Mapping[ColourSensorRef, ColourFacts]


# ---- Answers ----

class ReportRefusal(StrEnum):
    """Why a source's report was not recorded (the one refusal log)."""
    UNKNOWN_ADDRESS = "unknown_address"     # not a light or sensor address the source names
    UNREADABLE = "unreadable"               # not the report's shape
    OUT_OF_RANGE = "out_of_range"           # a value outside its range


Recorded = Literal["recorded", "unchanged"]     # the document changed, or it did not (kept all the same)


class RoomLightRefusal(StrEnum):
    """Why a House › Lights change was refused."""
    UNKNOWN_ROOM_LIGHT = "unknown_room_light"
    UNKNOWN_SENSOR = "unknown_sensor"
    UNKNOWN_COLOUR_SENSOR = "unknown_colour_sensor"
    UNKNOWN_ADDRESS = "unknown_address"                 # a reference no source Central runs names
    REFERENCE_TAKEN = "reference_taken"                 # one reference reports one room light or one Sensor
    SENSOR_REPORTS_A_LIGHT = "sensor_reports_a_light"   # a Sensor measures at most one room light, and stays while it does
    COLOUR_SENSOR_FOLLOWED = "colour_sensor_followed"   # a colour sensor stays while a room light follows it
    NAMED_BY_A_FRAME = "named_by_a_frame"               # a Frame's lighting names it
    TOO_MANY = "too_many"                               # past ROOM_LIGHTS_MAX, SENSORS_MAX or COLOUR_SENSORS_MAX


class DrawnReportRefusal(StrEnum):
    """Why the Player's drawn report was refused."""
    TOO_LARGE = "room_lights_drawn_too_large"
    INVALID = "room_lights_drawn_invalid"


class RoomLightRefused(Exception):
    """A refusal the routes answer as {"error": code}, at the status the routes give that code."""

    def __init__(self, code: RoomLightRefusal) -> None:
        super().__init__(code)
        self.code = code


__all__ = [
    "ADDRESS_MAX", "COLOUR_SENSORS_MAX", "ROOM_LIGHT_NAME_MAX", "SENSORS_MAX", "SOURCE_PATTERN", "Address",
    "BulbColour", "ColourFacts", "ColourReading", "ColourSensor", "ColourSensorId", "ColourSensorRef",
    "DrawnReportRefusal", "Facts", "FixedLight", "Kept", "LightFacts", "LightReport", "LightStated", "MeasuredLight",
    "Name", "Reading", "ReadingFacts", "Recorded", "ReportRefusal", "ReportedColourSource", "ReportedLight",
    "ReporterRef", "RoomLight", "RoomLightId", "RoomLightRefusal", "RoomLightRefused", "SensedColour",
    "SensedColourSource", "Sensor", "SensorId", "SensorRef", "Source", "TypedColour", "TypedKelvin",
]
