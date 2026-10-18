"""Home Assistant's references: the source a room light's or Sensor's reference names when Home
Assistant reports it, and the entity id as its address (contracts/home_assistant.py owns the entity
id's form).

A room light or Sensor Home Assistant reports is referenced as (SOURCE, entity id): the room-light
domain compares the reference whole and never reads the entity id.
"""
from __future__ import annotations

import re
from typing import Final

from central.room_light.model import ReporterRef, SensorRef
from contracts.home_assistant import ENTITY_ID_MAX, ENTITY_ID_PATTERN

SOURCE: Final = "home-assistant"   # the references' source
_ENTITY: Final = re.compile(ENTITY_ID_PATTERN)


def is_entity(address: str) -> bool:
    """Whether `address` is an entity id (ENTITY_ID_PATTERN whole, at most ENTITY_ID_MAX): the check a
    reference with SOURCE takes before it is saved on a room light or Sensor, and a key of Home
    Assistant's states before it is read. Pure."""
    return len(address) <= ENTITY_ID_MAX and _ENTITY.fullmatch(address) is not None


def light_reporter(entity: str) -> ReporterRef:
    """ReporterRef(SOURCE, entity). Pure."""
    return ReporterRef(source=SOURCE, address=entity)


def illuminance_sensor(entity: str) -> SensorRef:
    """SensorRef(SOURCE, entity). Pure."""
    return SensorRef(source=SOURCE, address=entity)


__all__ = ["SOURCE", "illuminance_sensor", "is_entity", "light_reporter"]
