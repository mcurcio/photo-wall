"""Sensors: Central's version; each room light as the wall draws it (on the Installation); a Pi's light
meter (only on a Pi whose host writes one); the light on each Frame."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import LIGHT_LUX, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .contract import RoomLightListing
from .coordinator import GalleriaConfigEntry, GalleriaRuntime
from .entity import (
    CentralEntity,
    FrameEntity,
    PiEntity,
    add_as_listed,
    central_device,
    installation_device,
)

ROOM_LIGHT_STATES = ["on", "off", "not_reported"]


async def async_setup_entry(hass: HomeAssistant, entry: GalleriaConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    runtime = entry.runtime_data

    def wanted() -> Iterable[tuple[str, Callable[[], Entity]]]:
        yield "central_version", lambda: CentralVersion(runtime)
        snapshot = runtime.central.data
        if snapshot is None:
            return
        for light in (snapshot.room_lights or {}).get("lights", ()):
            yield f"room_light:{light['id']}", lambda light_id=light["id"]: RoomLight(runtime, light_id)
        for frame_id in snapshot.frames():
            yield f"frame_light:{frame_id}", lambda frame_id=frame_id: FrameLight(runtime, frame_id)
        for serial, reader in runtime.pis.items():
            if reader.data is not None and isinstance(reader.data.light_meter, dict):
                yield f"light_meter:{serial}", lambda serial=serial: LightMeter(runtime, serial)

    add_as_listed(entry, async_add_entities, wanted)


class CentralVersion(CentralEntity, SensorEntity):
    _attr_translation_key = "central_version"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime: GalleriaRuntime) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"galleria_{runtime.origin}_central_version"
        self._attr_device_info = central_device(runtime.origin)

    @property
    def native_value(self) -> str | None:
        birth = self.coordinator.data.birth if self.coordinator.data else None
        return birth["version"] if birth else None


class RoomLight(CentralEntity, SensorEntity):
    """A room light as the wall draws it: on, off or not reported; its level and kelvin as drawn."""
    _attr_translation_key = "room_light"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ROOM_LIGHT_STATES

    def __init__(self, runtime: GalleriaRuntime, light_id: str) -> None:
        super().__init__(runtime)
        self.light_id = light_id
        self._attr_unique_id = f"galleria_{runtime.origin}_room_light_{light_id}"
        self._attr_device_info = installation_device(runtime.origin)
        listing = self._listing()
        self._attr_name = listing["name"] if listing else light_id

    def _listing(self) -> RoomLightListing | None:
        snapshot = self.coordinator.data
        lights = (snapshot.room_lights or {}).get("lights", ()) if snapshot else ()
        return next((light for light in lights if light["id"] == self.light_id), None)

    @property
    def available(self) -> bool:
        return super().available and self._listing() is not None

    def _drawn(self) -> tuple[str, float | None, dict[str, Any]]:
        """(state, level, extra attributes) of the light's entry as drawn."""
        entry = self._listing()["entry"]
        state = entry.get("state")
        if entry["kind"] == "fixed":
            return "on", 1.0, {}
        if state["kind"] in ("not-reported", "unavailable"):
            return "not_reported", None, {}
        if state["kind"] == "measured":
            return ("on" if state["lux"] > 0 else "off"), None, {"lux": state["lux"]}
        extra = {"rgb": state["rgb"]} if state["kind"] == "colour-dimmer" else {}
        return ("on" if state["on"] else "off"), (state["brightness"] if state["on"] else 0.0), extra

    @property
    def native_value(self) -> str | None:
        return self._drawn()[0] if self._listing() else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._listing():
            return None
        _, level, extra = self._drawn()
        colour = self._listing()["entry"]["colour"]
        return {"level": level, "kelvin": colour.get("kelvin"), **extra}


class LightMeter(PiEntity, SensorEntity):
    """The Pi's wired light meter, in lux."""
    _attr_translation_key = "light_meter"
    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, runtime: GalleriaRuntime, serial: str) -> None:
        super().__init__(runtime, serial)
        self._attr_unique_id = f"galleria_player_{serial}_light_meter"

    def _reading(self) -> dict | None:
        meter = self.coordinator.data.light_meter if self.coordinator.data else None
        return meter["reading"] if isinstance(meter, dict) else None

    @property
    def available(self) -> bool:
        reading = self._reading()
        return super().available and reading is not None and reading["kind"] == "measured"

    @property
    def native_value(self) -> float | None:
        reading = self._reading()
        return reading["lux"] if reading and reading["kind"] == "measured" else None


class FrameLight(FrameEntity, SensorEntity):
    """The light on the Frame at its centre, as its Player last drew it."""
    _attr_translation_key = "frame_light"
    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, runtime: GalleriaRuntime, frame_id: str) -> None:
        super().__init__(runtime, frame_id)
        self._attr_unique_id = f"galleria_frame_{frame_id}_light"

    @property
    def native_value(self) -> float | None:
        report = self.report()
        return report["light_lux"] if report else None


