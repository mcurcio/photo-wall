"""The devices and the entity bases every platform shares.

Devices (the design's table): Central (`central:<origin>`), the Installation (`installation:<origin>`),
a Player per Pi (`player:<serial>`, via the Installation) and a Frame per Frame (`frame:<id>`, via the
Installation). Entities appear as Central lists their Pis and Frames; each follows the reader that owns
its fact (one owner per fact: Central for Frames and releases, the Pi for what it runs and draws).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .contract import FrameReport
from .coordinator import (
    CentralCoordinator,
    GalleriaConfigEntry,
    GalleriaRuntime,
    PiCoordinator,
    pi_signal,
)
from .pi import READ_ERRORS


def central_device(origin: str) -> DeviceInfo:
    return DeviceInfo(identifiers={(DOMAIN, f"central:{origin}")}, name="Central", manufacturer="Photo Wall",
                      model="Central", configuration_url=origin)


def installation_device(origin: str) -> DeviceInfo:
    return DeviceInfo(identifiers={(DOMAIN, f"installation:{origin}")}, name="Installation",
                      manufacturer="Photo Wall", model="Installation")


def player_device(runtime: GalleriaRuntime, serial: str) -> DeviceInfo:
    player = runtime.central.data.players().get(serial) if runtime.central.data else None
    return DeviceInfo(identifiers={(DOMAIN, f"player:{serial}")}, name=player["name"] if player else serial,
                      manufacturer="Photo Wall", model="Player", serial_number=serial,
                      via_device=(DOMAIN, f"installation:{runtime.origin}"))


def frame_device(runtime: GalleriaRuntime, frame_id: str) -> DeviceInfo:
    frame = runtime.central.data.frames().get(frame_id) if runtime.central.data else None
    return DeviceInfo(identifiers={(DOMAIN, f"frame:{frame_id}")}, name=frame["name"] if frame else frame_id,
                      manufacturer="Photo Wall", model="Frame",
                      via_device=(DOMAIN, f"installation:{runtime.origin}"))


def add_as_listed(entry: GalleriaConfigEntry, add_entities: AddConfigEntryEntitiesCallback,
                  wanted: Callable[[], Iterable[tuple[str, Callable[[], Entity]]]]) -> None:
    """Add each entity `wanted` names (by its key) once, now and whenever Central or a Pi is read."""
    added: set[str] = set()
    runtime = entry.runtime_data

    @callback
    def add() -> None:
        new = [(key, build) for key, build in wanted() if key not in added]
        added.update(key for key, _ in new)
        if new:
            add_entities([build() for _, build in new])

    @callback
    def pi_read(_serial: str) -> None:
        add()

    add()
    entry.async_on_unload(runtime.central.async_add_listener(add))
    entry.async_on_unload(async_dispatcher_connect(runtime.central.hass, pi_signal(entry.entry_id), pi_read))


class CentralEntity(CoordinatorEntity[CentralCoordinator]):
    """An entity whose fact is Central's."""
    _attr_has_entity_name = True

    def __init__(self, runtime: GalleriaRuntime) -> None:
        super().__init__(runtime.central)
        self.runtime = runtime


class PiEntity(CoordinatorEntity[PiCoordinator]):
    """An entity of one Pi's Player device; unavailable while the Pi cannot be read."""
    _attr_has_entity_name = True

    def __init__(self, runtime: GalleriaRuntime, serial: str) -> None:
        super().__init__(runtime.pis[serial])
        self.runtime = runtime
        self.serial = serial
        self._attr_device_info = player_device(runtime, serial)


class FrameEntity(CentralEntity):
    """An entity of a Frame, read from the Pi Central names in the Frame's `driven_by`, and only
    while that Pi's report for the Frame carries the binding's generation."""

    def __init__(self, runtime: GalleriaRuntime, frame_id: str) -> None:
        super().__init__(runtime)
        self.frame_id = frame_id
        self._attr_device_info = frame_device(runtime, frame_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, pi_signal(self.coordinator.config_entry.entry_id), self._pi_read))

    @callback
    def _pi_read(self, serial: str) -> None:
        self.async_write_ha_state()

    def report(self) -> FrameReport | None:
        """This Frame's report from the Pi that drives it, at the binding's generation; None else.
        With Central away the Pi is the one Central last named (only Central changes a binding): the
        coordinator keeps its last successful listing while Home Assistant runs, never across a
        restart."""
        snapshot = self.coordinator.data
        frame = snapshot.frames().get(self.frame_id) if snapshot else None
        driven_by = frame["driven_by"] if frame else None
        reader = self.runtime.pis.get(driven_by["serial"]) if driven_by else None
        if reader is None or not reader.last_update_success or reader.data is None:
            return None
        frames = reader.data.frames
        if isinstance(frames, str) and frames in READ_ERRORS:
            return None
        return next((report for report in frames if report["frame_id"] == self.frame_id
                     and report["generation"] == driven_by["generation"]), None)

    @property
    def available(self) -> bool:
        return self.report() is not None
