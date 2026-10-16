"""Home Assistant's states: its lights and light sensors as the Galleria integration publishes them on
Central's hub, in Home Assistant's own words (0028; the Home Assistant system design, Q1-Q3 A,
2026-10-16). Contracts and pydantic only.

One value per entity id, the latest wins; the integration publishes every light and illuminance
entity except the integration's own, on each change and all again whenever it (re)connects or
Central's hub names a new states epoch (contracts/central_api.py STATES_EPOCH_KEY). The key of a
state is its entity id (`HOME_ASSISTANT_STATES_PREFIX + entity id`, contracts/central_api.py).

Rules this module owns (one home each): the entity id's form (ENTITY_ID_PATTERN, ENTITY_ID_MAX), a
state's shape and its scales as Home Assistant states them (brightness 0..LEVEL_MAX, rgb each
0..LEVEL_MAX, kelvin, lux), the per-value cap and how many entities the bucket keeps, the codec.

Rules it does NOT own: what a state means for a room light (Central's Home Assistant adapter,
central/infra/home_assistant/, alone turns these into room-light facts: the 0..255 scales stay there)
and which entities Central listens to (its room-light setup, `Listening` in contracts/central_api.py).

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
KELVIN_MIN: Final = 1000                   # the colour temperatures a state may carry
KELVIN_MAX: Final = 40000
NAME_MAX: Final = 255                      # a friendly name, for Central's setup picker
HOME_ASSISTANT_STATE_BYTES: Final = 512    # one state's largest encoding
HOME_ASSISTANT_ENTITIES_MAX: Final = 1024  # the bucket keeps the newest this many entities' states

EntityId = Annotated[str, Field(pattern=rf"^{ENTITY_ID_PATTERN}$", max_length=ENTITY_ID_MAX, title="EntityId")]
EntityName = Annotated[str, Field(min_length=1, max_length=NAME_MAX)]
Level = Annotated[int, Field(ge=0, le=LEVEL_MAX)]
Kelvin = Annotated[int, Field(ge=KELVIN_MIN, le=KELVIN_MAX)]
LuxNumber = Annotated[float, Field(ge=0, allow_inf_nan=False)]
EntityKind = Literal["light", "illuminance"]   # what an entity reports: a light's state, a lux reading


class HomeAssistantLight(Model):
    """A light entity's state: on or off, Home Assistant's brightness (0..LEVEL_MAX; None when it
    sent none), its colour temperature in kelvin and its rgb colour when it sent them."""
    kind: Literal["light"] = "light"
    name: EntityName
    on: bool
    brightness: Level | None
    kelvin: Kelvin | None
    rgb: tuple[Level, Level, Level] | None


class HomeAssistantIlluminance(Model):
    """An illuminance sensor's state, in lux (None: it reports no number now)."""
    kind: Literal["illuminance"] = "illuminance"
    name: EntityName
    lux: LuxNumber | None


class HomeAssistantUnavailable(Model):
    """Home Assistant's own "unavailable" (or "unknown") for an entity of `entity_kind`. Central
    records it as such: it adds no light."""
    kind: Literal["unavailable"] = "unavailable"
    entity_kind: EntityKind
    name: EntityName


HomeAssistantState = Annotated[HomeAssistantLight | HomeAssistantIlluminance | HomeAssistantUnavailable,
                               Field(discriminator="kind", title="HomeAssistantState")]


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


__all__ = [
    "ENTITY_ID_MAX", "ENTITY_ID_PATTERN", "HOME_ASSISTANT_ENTITIES_MAX", "HOME_ASSISTANT_STATE_BYTES",
    "KELVIN_MAX", "KELVIN_MIN", "LEVEL_MAX", "NAME_MAX", "EntityId", "EntityKind", "EntityName",
    "HomeAssistantIlluminance", "HomeAssistantLight", "HomeAssistantState", "HomeAssistantUnavailable", "Kelvin",
    "Level", "LuxNumber",
    "decode_home_assistant_state", "encode_home_assistant_state",
]
