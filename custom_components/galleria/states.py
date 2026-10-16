"""Home Assistant's states, published on Central's hub (contract.py HOME_ASSISTANT_STATES_PREFIX).

Every light and illuminance entity, except Galleria's own (no reading loops back from the wall), in
Home Assistant's own words: one value per entity id, the latest wins. The publisher puts a state on
every change, and every state again when it starts and whenever Central's hub names a states epoch
other than the one it last published all of them into (a hub restart, a lost connection: the
coordinator compares them at every read). Central's room-light setup picks the ones it listens to.
"""
from __future__ import annotations

import asyncio
import logging
import math

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    EVENT_STATE_CHANGED,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntry

from .const import DOMAIN
from .contract import (
    ENTITY_NAME_MAX,
    KELVIN_MAX,
    KELVIN_MIN,
    LEVEL_MAX,
    STATES_EPOCH_KEY,
    HomeAssistantState,
    decode_central_state,
)
from .door import CentralDoor, DoorUnavailable

_LOGGER = logging.getLogger(__name__)
_DOMAINS = ("light", "sensor")


def _level(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return min(LEVEL_MAX, max(0, round(value)))


def _kelvin(value: object) -> int | None:
    level = None if isinstance(value, bool) or not isinstance(value, int | float) else value
    return round(level) if level is not None and math.isfinite(level) and KELVIN_MIN <= level <= KELVIN_MAX else None


def _rgb(value: object) -> list[int] | None:
    if not isinstance(value, list | tuple) or len(value) != 3:
        return None
    levels = [_level(channel) for channel in value]
    return None if None in levels else levels


def _lux(text: str) -> float | None:
    try:
        lux = float(text)
    except ValueError:
        return None
    return lux if math.isfinite(lux) and lux >= 0 else None


def _name(state: State) -> str:
    return (state.name or state.entity_id)[:ENTITY_NAME_MAX] or state.entity_id


def home_assistant_state(state: State) -> HomeAssistantState | None:
    """`state` in Home Assistant's states' words: a light, an illuminance sensor, or unavailable (its
    "unavailable" and "unknown"); None for any other entity."""
    if state.domain == "light":
        kind = "light"
    elif state.domain == "sensor" and state.attributes.get(ATTR_DEVICE_CLASS) == SensorDeviceClass.ILLUMINANCE:
        kind = "illuminance"
    else:
        return None
    name = _name(state)
    if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return {"kind": "unavailable", "entity_kind": kind, "name": name}
    if kind == "illuminance":
        return {"kind": "illuminance", "name": name, "lux": _lux(state.state)}
    attributes = state.attributes
    return {"kind": "light", "name": name, "on": state.state == STATE_ON,
            "brightness": _level(attributes.get("brightness")),
            "kelvin": _kelvin(attributes.get("color_temp_kelvin")),
            "rgb": _rgb(attributes.get("rgb_color"))}


def is_published(entity_id: str, entry: RegistryEntry | None) -> bool:
    """A light or sensor entity that is not Galleria's own (an illuminance sensor of Galleria's would
    loop the wall's own reading back)."""
    return entity_id.split(".", 1)[0] in _DOMAINS and (entry is None or entry.platform != DOMAIN)


def _gone(old: State | None) -> HomeAssistantState | None:
    """A removed entity's last word: unavailable, of the kind it was."""
    prior = home_assistant_state(old) if old is not None else None
    if prior is None:
        return None
    kind = prior["entity_kind"] if prior["kind"] == "unavailable" else prior["kind"]
    return {"kind": "unavailable", "entity_kind": kind, "name": prior["name"]}


class StatePublisher:
    """Puts Home Assistant's states on Central's hub: each change (the latest per entity, in one
    queue), and all of them again on start and whenever `publish_all` runs."""

    def __init__(self, hass: HomeAssistant, door: CentralDoor) -> None:
        self._hass = hass
        self._door = door
        self._pending: dict[str, HomeAssistantState] = {}
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._unsubscribe: CALLBACK_TYPE | None = None
        self._task: asyncio.Task | None = None
        self.published_epoch: str | None = None   # the states epoch every state was last published into

    async def start(self) -> None:
        self._unsubscribe = self._hass.bus.async_listen(EVENT_STATE_CHANGED, self._changed)
        self._task = self._hass.async_create_background_task(self._run(), "galleria state publisher")
        await self.publish_all()

    @callback
    def _changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        if not is_published(entity_id, er.async_get(self._hass).async_get(entity_id)):
            return
        new = event.data["new_state"]
        value = home_assistant_state(new) if new is not None else _gone(event.data["old_state"])
        if value is not None:
            self._pending[entity_id] = value
            self._wake.set()

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            async with self._lock:
                while self._pending:
                    entity_id = next(iter(self._pending))
                    await self._put(entity_id, self._pending.pop(entity_id))

    async def _put(self, entity_id: str, value: HomeAssistantState) -> bool:
        try:
            await self._door.publish(entity_id, value)
        except DoorUnavailable as error:
            _LOGGER.debug("not published, every state goes again: %s", error)
            self.published_epoch = None   # the coordinator's next read publishes them all again
            self._pending.clear()
            return False
        except ValueError:
            _LOGGER.warning("%s's state is not one Central reads: %s", entity_id, value)
        return True

    async def publish_all(self) -> None:
        """Every published entity's state, into the states epoch Central's hub names now; the epoch is
        kept only when every one was taken."""
        async with self._lock:
            self._pending.clear()
            try:
                raw = await self._door.read(STATES_EPOCH_KEY)
                epoch = decode_central_state(STATES_EPOCH_KEY, raw)["epoch"] if raw is not None else None
            except (DoorUnavailable, ValueError) as error:
                _LOGGER.debug("no states epoch: %r", error)
                epoch = None
            if epoch is None:
                self.published_epoch = None
                return
            registry = er.async_get(self._hass)
            for state in self._hass.states.async_all(_DOMAINS):
                value = home_assistant_state(state)
                if (value is not None and is_published(state.entity_id, registry.async_get(state.entity_id))
                        and not await self._put(state.entity_id, value)):
                    return
            self.published_epoch = epoch

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._task is not None:
            self._task.cancel()
            self._task = None
