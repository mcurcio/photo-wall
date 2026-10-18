"""Each Player's update entity: installed is the release its Pi booted (its host's `base` tag), latest is
the newest stable release Central holds ready, and Install asks Central to put that release on this
Pi only (installPlayer; owner, 2026-10-16: "Updates can also be triggered by the Player device and
apply only to that device"). The design's separate "base version" sensor is not built: it would
always equal installed."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import GalleriaConfigEntry, GalleriaRuntime
from .door import DoorUnavailable
from .entity import PiEntity, add_as_listed
from .pi import READ_ERRORS


async def async_setup_entry(hass: HomeAssistant, entry: GalleriaConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    runtime = entry.runtime_data

    def wanted() -> Iterable[tuple[str, Callable[[], Entity]]]:
        for serial in runtime.pis:
            yield f"player:{serial}", lambda serial=serial: PlayerUpdate(runtime, serial)

    add_as_listed(entry, async_add_entities, wanted)


class PlayerUpdate(PiEntity, UpdateEntity):
    _attr_name = None   # the Player device's own name
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS

    def __init__(self, runtime: GalleriaRuntime, serial: str) -> None:
        super().__init__(runtime, serial)
        self._attr_unique_id = f"galleria_player_{serial}_update"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.runtime.central.async_add_listener(self._central_read))

    @callback
    def _central_read(self) -> None:
        self.async_write_ha_state()

    def _base_tag(self) -> str | None:
        tag = self.coordinator.data.base_tag if self.coordinator.data else None
        return None if tag is None or tag in READ_ERRORS else tag

    @property
    def available(self) -> bool:
        return super().available and self._base_tag() is not None

    @property
    def installed_version(self) -> str | None:
        return self._base_tag()

    @property
    def latest_version(self) -> str | None:
        snapshot = self.runtime.central.data
        return snapshot.latest() if snapshot and self.runtime.central.last_update_success else None

    @property
    def in_progress(self) -> bool:
        snapshot = self.runtime.central.data
        pi = ((snapshot.versions or {}).get("pis", {}) if snapshot else {}).get(self.serial)
        return bool(pi and pi["install"] is not None)

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """installPlayer(this Pi, latest): only the latest release, only this Pi."""
        latest = self.latest_version
        if latest is None:
            raise HomeAssistantError("Central holds no stable release ready to install")
        try:
            answer = await self.runtime.door.install_player(self.serial, latest)
        except DoorUnavailable as error:
            raise HomeAssistantError(f"Central did not answer the install: {error}") from error
        if answer["kind"] == "refused":
            raise HomeAssistantError(f"Central refused to install {latest} on {self.serial}: {answer['refusal']}")
        await self.runtime.central.async_request_refresh()
