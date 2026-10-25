"""Home Assistant's states: its lights, light sensors and colour-temperature sensors as the Galleria
integration publishes them on Central's hub, in Home Assistant's own words (0028; the Home Assistant
system design, Q1-Q3 A, 2026-10-16; colour, 2026-10-25: "There should be existing home assistant
entities for outside brightness and color temp. Get those wired into the frame"). Contracts and
pydantic only.

One value per entity id, the latest wins; the integration publishes every light, illuminance and
colour-temperature entity except the integration's own, on each change and all again whenever it
(re)connects or Central's hub names a new states epoch (contracts/central_api.py STATES_EPOCH_KEY).
The key of a state is its entity id (`HOME_ASSISTANT_STATES_PREFIX + entity id`,
contracts/central_api.py). One more key, GALLERIA_BIRTH_KEY (not an entity id: it has no dot), holds
`GalleriaBirth`: Galleria's version and the entity kinds it publishes, put on start and with every
publish of all states, so Central's Home Assistant page can say "update Galleria".

Which kind an entity is, and which colour a light states, are Galleria's to judge from Home
Assistant's own fields (custom_components/galleria/states.py): a light by its domain; a lux sensor by
its illuminance device class; a colour-temperature sensor by its unit "K" with a device class other
than temperature; a light's colour by its `color_mode` (color_temp: its kelvin; hs, xy, rgb, rgbw,
rgbww: its rgb; any other mode, or off: none). Galleria never bounds, rounds or keeps a value: a
kelvin or lux is any finite number Home Assistant states, and Central judges its range.

Rules this module owns (one home each): the entity id's form (ENTITY_ID_PATTERN, ENTITY_ID_MAX), the
entity kinds, a state's shape and its scales as Home Assistant states them (brightness 0..LEVEL_MAX,
rgb each 0..LEVEL_MAX, kelvin, lux), Galleria's birth, the per-value cap and how many entities the
bucket keeps, the codecs.

Rules it does NOT own: what a state means for a room light (Central's Home Assistant adapter,
central/infra/home_assistant/, alone turns these into room-light facts: the 0..255 scales and the
stated kelvin's range, contracts/kelvin.py, are judged there) and which entities Central listens to
(its room-light setup, `Listening` in contracts/central_api.py).

Decoding is strict (contracts.wire_model.decoded): no bool for a number, exactly each shape's fields.
"""
from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field

from contracts.wire_codec import encode
from contracts.wire_model import Model, decoded

# Home Assistant's entity id: <domain>.<object_id>, lower-case ASCII letters, digits and underscores.
ENTITY_ID_PATTERN: Final = r"[a-z0-9_]+\.[a-z0-9_]+"
ENTITY_ID_MAX: Final = 255
LEVEL_MAX: Final = 255                     # Home Assistant's brightness and each rgb channel: 0..LEVEL_MAX
NAME_MAX: Final = 255                      # a friendly name, for Central's setup picker
VERSION_MAX: Final = 64                    # Galleria's version, as its manifest states it
HOME_ASSISTANT_STATE_BYTES: Final = 512    # one state's largest encoding
GALLERIA_BIRTH_BYTES: Final = 256          # GalleriaBirth's largest encoding
HOME_ASSISTANT_ENTITIES_MAX: Final = 1024  # the bucket keeps the newest this many entities' states
GALLERIA_BIRTH_KEY: Final = "galleria"     # the bucket's one key that is not an entity id

EntityId = Annotated[str, Field(pattern=rf"^{ENTITY_ID_PATTERN}$", max_length=ENTITY_ID_MAX, title="EntityId")]
EntityName = Annotated[str, Field(min_length=1, max_length=NAME_MAX)]
Level = Annotated[int, Field(ge=0, le=LEVEL_MAX)]
KelvinNumber = Annotated[float, Field(gt=0, allow_inf_nan=False)]   # as Home Assistant states it; Central judges the range
LuxNumber = Annotated[float, Field(ge=0, allow_inf_nan=False)]
# What an entity reports: a light's state, a lux reading, a colour temperature reading.
EntityKind = Literal["light", "illuminance", "colour-temperature"]


class HomeAssistantKelvin(Model):
    """A light in Home Assistant's color_temp mode: its colour temperature, kelvin."""
    kind: Literal["kelvin"] = "kelvin"
    kelvin: KelvinNumber


class HomeAssistantRgb(Model):
    """A light in a colour mode (hs, xy, rgb, rgbw, rgbww): Home Assistant's rgb_color, each 0..LEVEL_MAX."""
    kind: Literal["rgb"] = "rgb"
    rgb: tuple[Level, Level, Level]


HomeAssistantColour = Annotated[HomeAssistantKelvin | HomeAssistantRgb,
                                Field(discriminator="kind", title="HomeAssistantColour")]


class HomeAssistantLight(Model):
    """A light entity's state: on or off, Home Assistant's brightness (0..LEVEL_MAX; None when it
    sent none), and the colour its mode states (None: off, or a brightness or on/off mode)."""
    kind: Literal["light"] = "light"
    name: EntityName
    on: bool
    brightness: Level | None
    colour: HomeAssistantColour | None


class HomeAssistantIlluminance(Model):
    """An illuminance sensor's state, in lux (None: it reports no number now)."""
    kind: Literal["illuminance"] = "illuminance"
    name: EntityName
    lux: LuxNumber | None


class HomeAssistantColourTemperature(Model):
    """A colour-temperature sensor's state, in kelvin (None: it reports no number now)."""
    kind: Literal["colour-temperature"] = "colour-temperature"
    name: EntityName
    kelvin: KelvinNumber | None


class HomeAssistantUnavailable(Model):
    """Home Assistant's own "unavailable" (or "unknown") for an entity of `entity_kind`. Central
    records that it cannot tell; the last stated value stands."""
    kind: Literal["unavailable"] = "unavailable"
    entity_kind: EntityKind
    name: EntityName


HomeAssistantState = Annotated[
    HomeAssistantLight | HomeAssistantIlluminance | HomeAssistantColourTemperature | HomeAssistantUnavailable,
    Field(discriminator="kind", title="HomeAssistantState")]


class GalleriaBirth(Model):
    """Galleria as a component on Central's hub (GALLERIA_BIRTH_KEY): its version (its manifest's,
    the release tag it came from) and every entity kind it publishes."""
    component: Literal["galleria"] = "galleria"
    version: Annotated[str, Field(min_length=1, max_length=VERSION_MAX)]
    kinds: tuple[EntityKind, ...] = Field(max_length=8)


class _StateValue(Model):
    """The bucket's value: one state (a model, so the strict decoder reads the union whole)."""
    state: HomeAssistantState


def encode_home_assistant_state(state: HomeAssistantState) -> bytes:
    """Canonical JSON {"state": <state>} of at most HOME_ASSISTANT_STATE_BYTES;
    ValueError("home_assistant_state_too_large") past it."""
    return encode(_StateValue(state=state).model_dump(mode="json"), HOME_ASSISTANT_STATE_BYTES,
                  "home_assistant_state_too_large")


def decode_home_assistant_state(raw: bytes) -> HomeAssistantState:
    """The state `raw` encodes; ValueError("home_assistant_state") for anything else."""
    return decoded(_StateValue, raw, HOME_ASSISTANT_STATE_BYTES, "home_assistant_state").state


def encode_galleria_birth(birth: GalleriaBirth) -> bytes:
    """Canonical JSON of at most GALLERIA_BIRTH_BYTES; ValueError("galleria_birth_too_large") past it."""
    return encode(birth.model_dump(mode="json"), GALLERIA_BIRTH_BYTES, "galleria_birth_too_large")


def decode_galleria_birth(raw: bytes) -> GalleriaBirth:
    """The birth `raw` encodes; ValueError("galleria_birth") for anything else."""
    return decoded(GalleriaBirth, raw, GALLERIA_BIRTH_BYTES, "galleria_birth")


__all__ = [
    "ENTITY_ID_MAX", "ENTITY_ID_PATTERN", "GALLERIA_BIRTH_BYTES", "GALLERIA_BIRTH_KEY", "HOME_ASSISTANT_ENTITIES_MAX",
    "HOME_ASSISTANT_STATE_BYTES", "LEVEL_MAX", "NAME_MAX", "VERSION_MAX", "EntityId", "EntityKind", "EntityName",
    "GalleriaBirth", "HomeAssistantColour", "HomeAssistantColourTemperature", "HomeAssistantIlluminance",
    "HomeAssistantKelvin", "HomeAssistantLight", "HomeAssistantRgb", "HomeAssistantState", "HomeAssistantUnavailable",
    "KelvinNumber", "Level", "LuxNumber",
    "decode_galleria_birth", "decode_home_assistant_state", "encode_galleria_birth", "encode_home_assistant_state",
]
