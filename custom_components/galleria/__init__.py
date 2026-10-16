"""Galleria for Home Assistant: one config entry per Central (`{origin}`), with devices Central,
Installation, Player (per Pi) and Frame, speaking NATS only (owner, 2026-10-16: "we should build a
HACS plugin into the repo so that home assistant can install photo-wall and users get a native
experience"; the integration's name: "the HACS integration should be called "galleria" instead of
photo-wall, because that is going to be the future name of this project").

It reaches Central's hub through Central's door (door.py) and each Pi through its read-only door
(pi.py). It never imports a package of this repository at runtime: every name, key and shape it uses
comes from its generated copy, `contract.py` (written by scripts/galleria_contract.py; a check test
fails when it drifts).
"""
from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import CONF_ORIGIN, DOMAIN
from .coordinator import CentralCoordinator, GalleriaConfigEntry, GalleriaRuntime
from .door import CentralDoor, DoorUnavailable
from .entity import central_device, installation_device
from .states import StatePublisher

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.UPDATE]

__all__ = ["DOMAIN", "async_setup_entry", "async_unload_entry"]


async def async_setup_entry(hass: HomeAssistant, entry: GalleriaConfigEntry) -> bool:
    try:
        door = await CentralDoor.connect(entry.data[CONF_ORIGIN])
    except DoorUnavailable as error:
        raise ConfigEntryNotReady(f"Central's door does not answer: {error}") from error
    publisher = StatePublisher(hass, door)
    runtime = GalleriaRuntime(origin=entry.data[CONF_ORIGIN], door=door, publisher=publisher,
                              central=CentralCoordinator(hass, entry, door, publisher))
    try:
        await publisher.start()
        await runtime.central.async_config_entry_first_refresh()
    except BaseException:
        await publisher.stop()
        await door.close()
        raise
    entry.runtime_data = runtime
    devices = dr.async_get(hass)
    for device in (central_device(runtime.origin), installation_device(runtime.origin)):
        # before any Player or Frame names the Installation as its via_device
        devices.async_get_or_create(config_entry_id=entry.entry_id, **device)
    runtime.sync_pis(hass, entry)
    entry.async_on_unload(runtime.central.async_add_listener(lambda: runtime.sync_pis(hass, entry)))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GalleriaConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.publisher.stop()
        await entry.runtime_data.door.close()
    return unloaded
