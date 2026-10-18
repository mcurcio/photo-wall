"""Binary sensors: whether Central's hub is connected; whether each Frame is showing."""
from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import GalleriaConfigEntry, GalleriaRuntime
from .entity import CentralEntity, FrameEntity, add_as_listed, central_device


async def async_setup_entry(hass: HomeAssistant, entry: GalleriaConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    runtime = entry.runtime_data

    def wanted() -> Iterable[tuple[str, Callable[[], Entity]]]:
        yield "hub_connected", lambda: HubConnected(runtime)
        if runtime.central.data is not None:
            for frame_id in runtime.central.data.frames():
                yield f"frame_showing:{frame_id}", lambda frame_id=frame_id: FrameShowing(runtime, frame_id)

    add_as_listed(entry, async_add_entities, wanted)


class HubConnected(CentralEntity, BinarySensorEntity):
    """Whether Galleria's connection to Central's hub is up (always available: it is the answer)."""
    _attr_translation_key = "hub_connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime: GalleriaRuntime) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"galleria_{runtime.origin}_hub_connected"
        self._attr_device_info = central_device(runtime.origin)

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.runtime.door.connected


class FrameShowing(FrameEntity, BinarySensorEntity):
    """Whether the Frame's picture is on screen, as the Pi that drives it reports."""
    _attr_translation_key = "showing"

    def __init__(self, runtime: GalleriaRuntime, frame_id: str) -> None:
        super().__init__(runtime, frame_id)
        self._attr_unique_id = f"galleria_frame_{frame_id}_showing"

    @property
    def is_on(self) -> bool | None:
        report = self.report()
        return report["showing"] if report else None
