"""Room lights' vocabulary: the real lights of the house and who reports each, Sensors, the words
their reporters last said, and the answers of recording and setting up. Pure; frozen values only.

A room light's id is the key of its mapping, never a field of the light. Who reports a light or a
Sensor is an opaque reference, `(source, address)`: this package compares references whole and never
reads an address; each source's adapter owns its address's form.

Colours, states and entries are the wire's (contracts/room_light.py): House › Lights, the store and
the room-lights document share one vocabulary.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field

from contracts.models import Model
from contracts.room_light import (
    ROOM_LIGHT_ID_PATTERN,
    ColourDimmer,
    Dimmer,
    FixedColour,
    Measured,
    ReportedColour,
    RoomLightId,
    Unavailable,
)

ROOM_LIGHT_NAME_MAX: Final = 64
SENSORS_MAX: Final = 32
SOURCE_PATTERN: Final = r"[a-z][a-z0-9-]{0,31}"     # a reporting source's name
ADDRESS_MAX: Final = 255

SensorId = Annotated[str, Field(pattern=rf"^{ROOM_LIGHT_ID_PATTERN}$", title="SensorId")]
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


# ---- Room lights: one kind per reporter, so a colour and a reporter that do not belong together
# cannot be built ----

class ReportedLight(Model):
    """A light its reporter reports as a dimmer: a white light of a configured colour, or a colour
    light whose drive its reporter sends."""
    kind: Literal["reported"] = "reported"
    name: Name
    reporter: ReporterRef
    colour: Annotated[FixedColour | ReportedColour, Field(discriminator="kind")]


class MeasuredLight(Model):
    """A light one Sensor measures: its state is that Sensor's reading."""
    kind: Literal["measured"] = "measured"
    name: Name
    sensor: SensorId
    colour: FixedColour


class FixedLight(Model):
    """A light nobody reports: always on, at full."""
    kind: Literal["fixed"] = "fixed"
    name: Name
    colour: FixedColour


RoomLight = Annotated[ReportedLight | MeasuredLight | FixedLight, Field(discriminator="kind")]


class Sensor(Model):
    """A light meter at one point; it reads the sum of every light there."""
    name: Name
    at: SensorRef


# ---- What reporters last said, by reference ----

LightWord = Dimmer | ColourDimmer | Unavailable
ReadingWord = Measured | Unavailable


@dataclass(frozen=True)
class Words:
    """Every reference's last word, whether or not a room light or Sensor names it."""
    lights: Mapping[ReporterRef, LightWord]
    readings: Mapping[SensorRef, ReadingWord]


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
    UNKNOWN_ADDRESS = "unknown_address"                 # a reference no source Central runs names
    REFERENCE_TAKEN = "reference_taken"                 # one reference reports one room light or one Sensor
    SENSOR_REPORTS_A_LIGHT = "sensor_reports_a_light"   # a Sensor measures at most one room light, and stays while it does
    NAMED_BY_A_FRAME = "named_by_a_frame"               # a Frame's lighting names it
    TOO_MANY = "too_many"                               # past ROOM_LIGHTS_MAX or SENSORS_MAX


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
    "ADDRESS_MAX", "ROOM_LIGHT_NAME_MAX", "SENSORS_MAX", "SOURCE_PATTERN", "Address", "FixedLight", "LightWord",
    "MeasuredLight", "Name", "ReadingWord", "Recorded", "ReportRefusal", "ReportedLight", "ReporterRef", "RoomLight",
    "DrawnReportRefusal", "RoomLightId", "RoomLightRefusal", "RoomLightRefused", "Sensor", "SensorId", "SensorRef", "Source", "Words",
]
