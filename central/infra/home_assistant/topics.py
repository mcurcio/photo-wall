"""The Home Assistant link's topics and references: the one home of the topic layout the shipped
automation publishes (integrations/home-assistant/photo-wall-room-lights.yaml, held to these
constants by a test) and of the entity id's form.

A room light or Sensor Home Assistant reports is referenced as (SOURCE, entity id): the room-light
domain compares the reference whole and never reads the entity id.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

from central.room_light.model import ReporterRef, SensorRef

SOURCE: Final = "home-assistant"                            # the references' source
LIGHT_TOPIC_PREFIX: Final = "photowall/light/"              # + entity id: a light's full state, retained
ILLUMINANCE_TOPIC_PREFIX: Final = "photowall/illuminance/"  # + entity id: an illuminance sensor's, retained
SUBSCRIPTION: Final = "photowall/#"                         # the worker's one subscription
MESSAGE_BYTES: Final = 1024                                 # a larger payload is refused unread
# Home Assistant's entity id: <domain>.<object_id>, lower-case ASCII letters, digits and underscores.
ENTITY_PATTERN: Final = r"[a-z0-9_]+\.[a-z0-9_]+"
ENTITY_MAX: Final = 255
_ENTITY: Final = re.compile(ENTITY_PATTERN)


@dataclass(frozen=True)
class Topic:
    """What a topic names: a light's state or an illuminance sensor's reading, of `entity`."""
    kind: Literal["light", "illuminance"]
    entity: str


def topic_of(topic: str) -> Topic | None:
    """The Topic LIGHT_TOPIC_PREFIX + entity or ILLUMINANCE_TOPIC_PREFIX + entity names, where entity
    matches ENTITY_PATTERN whole and is at most ENTITY_MAX characters; None for any other topic. Pure."""
    for kind, prefix in (("light", LIGHT_TOPIC_PREFIX), ("illuminance", ILLUMINANCE_TOPIC_PREFIX)):
        if topic.startswith(prefix):
            entity = topic[len(prefix):]
            return Topic(kind, entity) if is_entity(entity) else None
    return None


def is_entity(address: str) -> bool:
    """Whether `address` is an entity id (ENTITY_PATTERN whole, at most ENTITY_MAX): the check a
    reference with SOURCE takes before it is saved on a room light or Sensor. Pure."""
    return len(address) <= ENTITY_MAX and _ENTITY.fullmatch(address) is not None


def light_reporter(entity: str) -> ReporterRef:
    """ReporterRef(SOURCE, entity). Pure."""
    return ReporterRef(source=SOURCE, address=entity)


def illuminance_sensor(entity: str) -> SensorRef:
    """SensorRef(SOURCE, entity). Pure."""
    return SensorRef(source=SOURCE, address=entity)


__all__ = ["ENTITY_MAX", "ENTITY_PATTERN", "ILLUMINANCE_TOPIC_PREFIX", "LIGHT_TOPIC_PREFIX", "MESSAGE_BYTES",
           "SOURCE", "SUBSCRIPTION", "Topic", "illuminance_sensor", "is_entity", "light_reporter", "topic_of"]
