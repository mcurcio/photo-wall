"""Home Assistant's states, published on Central's hub (contract.py HOME_ASSISTANT_STATES_PREFIX).

Every light, illuminance and colour-temperature entity, except Galleria's own (no reading loops back
from the wall), in Home Assistant's own words: one value per entity id, the latest wins. A sensor is
an illuminance one by its device class, a colour-temperature one by its unit "K" with any device class
but temperature; each value goes as Home Assistant states it, never bounded, rounded or kept here
(Central and the Pi own those). Galleria's birth (its version, the kinds it publishes and the hub
connection it goes on) goes under GALLERIA_BIRTH_KEY first with every publish of all states. The
publisher puts a state on every change, and its birth and every state again when it starts, after
every reconnect to Central's hub (a new connection: Central marks Home Assistant's lights "link
closed" when the connection its birth names closes, and each clears as its state is restated) and
whenever Central's hub names a states epoch other than the one it last published all of them into
(a hub restart: the coordinator compares them at every read). Central's room-light setup picks the ones it listens to.
"""
from __future__ import annotations

import asyncio
import logging
import math

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    EVENT_STATE_CHANGED,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
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
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .contract import (
    ENTITY_NAME_MAX,
    LEVEL_MAX,
    STATES_EPOCH_KEY,
    GalleriaBirth,
    HomeAssistantKelvin,
    HomeAssistantRgb,
    HomeAssistantState,
    decode_central_state,
)
from .door import CentralDoor, DoorUnavailable

_LOGGER = logging.getLogger(__name__)
_DOMAINS = ("light", "sensor")
# Home Assistant's light colour modes, by the colour each states (homeassistant.components.light ColorMode).
_KELVIN_MODES = frozenset({"color_temp"})
_RGB_MODES = frozenset({"hs", "xy", "rgb", "rgbw", "rgbww"})
KINDS = ("light", "illuminance", "colour-temperature")   # every entity kind Galleria publishes (its birth)


def _level(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return min(LEVEL_MAX, max(0, round(value)))


def _kelvin(value: object) -> float | None:
    """Home Assistant's kelvin as it states it (any finite positive number; Central judges the range)."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _rgb(value: object) -> list[int] | None:
    if not isinstance(value, list | tuple) or len(value) != 3:
        return None
    levels = [_level(channel) for channel in value]
    return None if None in levels else levels


def _sensed_kelvin(text: str) -> float | None:
    """A colour-temperature sensor's state as it states it: any finite number above 0, else None."""
    try:
        return _kelvin(float(text))
    except ValueError:
        return None


def _lux(text: str) -> float | None:
    try:
        lux = float(text)
    except ValueError:
        return None
    return lux if math.isfinite(lux) and lux >= 0 else None


def _colour(state: State) -> HomeAssistantKelvin | HomeAssistantRgb | None:
    """The colour a light's `color_mode` states: color_temp, its kelvin; hs, xy, rgb, rgbw, rgbww,
    its rgb; any other mode (brightness, onoff, white, unknown), off or none, no colour. Never a
    colour worked out from another mode's attributes."""
    mode = state.attributes.get("color_mode")
    if state.state != STATE_ON or not isinstance(mode, str):
        return None
    if mode in _KELVIN_MODES:
        kelvin = _kelvin(state.attributes.get("color_temp_kelvin"))
        return None if kelvin is None else {"kind": "kelvin", "kelvin": kelvin}
    if mode in _RGB_MODES:
        rgb = _rgb(state.attributes.get("rgb_color"))
        return None if rgb is None else {"kind": "rgb", "rgb": rgb}   # as stated: Central judges its range
    return None


def _name(state: State) -> str:
    return (state.name or state.entity_id)[:ENTITY_NAME_MAX] or state.entity_id


def _sensor_kind(state: State) -> str | None:
    """A sensor's kind: illuminance by its device class; colour temperature by its unit "K" with any
    device class but temperature (Home Assistant has no colour-temperature class); else None."""
    device_class = state.attributes.get(ATTR_DEVICE_CLASS)
    if device_class == SensorDeviceClass.ILLUMINANCE:
        return "illuminance"
    if (state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) == UnitOfTemperature.KELVIN
            and device_class != SensorDeviceClass.TEMPERATURE):
        return "colour-temperature"
    return None


def home_assistant_state(state: State) -> HomeAssistantState | None:
    """`state` in Home Assistant's states' words: a light, an illuminance sensor, a colour-temperature
    sensor, or unavailable (its "unavailable" and "unknown"); None for any other entity."""
    if state.domain == "light":
        kind = "light"
    elif state.domain == "sensor":
        kind = _sensor_kind(state)
        if kind is None:
            return None
    else:
        return None
    name = _name(state)
    if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return {"kind": "unavailable", "entity_kind": kind, "name": name}
    if kind == "illuminance":
        return {"kind": "illuminance", "name": name, "lux": _lux(state.state)}
    if kind == "colour-temperature":
        return {"kind": "colour-temperature", "name": name, "kelvin": _sensed_kelvin(state.state)}
    attributes = state.attributes
    return {"kind": "light", "name": name, "on": state.state == STATE_ON,
            "brightness": _level(attributes.get("brightness")), "colour": _colour(state)}


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
        self._door.on_reconnected(self._reconnected)
        await self.publish_all()

    async def _reconnected(self) -> None:
        """A new connection to Central's hub: its birth and every state again, off the client's own
        reconnect path."""
        self._hass.async_create_background_task(self.publish_all(), "galleria publish after reconnect")

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

    async def _put_birth(self) -> bool:
        """Galleria's birth: its manifest's version, KINDS and the hub connection it goes on (False:
        the hub did not take it, or no connection is open). Put first with every publish of all
        states, so Central judges the close of the newest connection only."""
        integration = await async_get_integration(self._hass, DOMAIN)
        connection = self._door.connection
        if connection is None:
            self.published_epoch = None
            return False
        birth: GalleriaBirth = {"component": "galleria", "version": str(integration.version), "kinds": list(KINDS),
                                "connection": connection}
        try:
            await self._door.publish_birth(birth)
        except DoorUnavailable as error:
            _LOGGER.debug("birth not published, every state goes again: %s", error)
            self.published_epoch = None
            return False
        except ValueError:
            _LOGGER.warning("Galleria's birth is not one Central reads: %s", birth)
        return True

    async def publish_all(self) -> None:
        """Galleria's birth and every published entity's state, into the states epoch Central's hub
        names now; the epoch is kept only when every one was taken."""
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
            if not await self._put_birth():
                return
            registry = er.async_get(self._hass)
            for state in self._hass.states.async_all(_DOMAINS):
                value = home_assistant_state(state)
                if (value is not None and is_published(state.entity_id, registry.async_get(state.entity_id))
                        and not await self._put(state.entity_id, value)):
                    return
            self.published_epoch = epoch

    async def stop(self) -> None:
        self._door.on_reconnected(None)
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._task is not None:
            self._task.cancel()
            self._task = None
