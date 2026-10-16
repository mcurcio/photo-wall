"""The two readers: Central's state through its door, and each Pi through its read-only door, both
every POLL_SECONDS, gets only. Nothing is kept across a Home Assistant restart: a Pi's address comes
only from Central's `installation`, so while Central is away after a restart every Pi reads
unavailable (the design's rule: a copy would be a cache serving while its source is away)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, POLL_SECONDS
from .contract import (
    CENTRAL_BIRTH_KEY,
    INSTALLATION_KEY,
    RELEASES_KEY,
    ROOM_LIGHTS_KEY,
    STATES_EPOCH_KEY,
    VERSIONS_KEY,
    CentralBirth,
    FrameListing,
    Installation,
    PlayerListing,
    Releases,
    RoomLightsListing,
    Versions,
    decode_central_state,
)
from .door import CentralDoor, DoorUnavailable
from .pi import PiReading, read_pi
from .states import StatePublisher

_LOGGER = logging.getLogger(__name__)
_INTERVAL = timedelta(seconds=POLL_SECONDS)


def pi_signal(entry_id: str) -> str:
    """The dispatcher signal sent, with the serial, after any Pi of the entry was read."""
    return f"{DOMAIN}_{entry_id}_pi"


@dataclass(frozen=True)
class CentralSnapshot:
    """Central's state as last read; None: Central has not written that key (or wrote one the
    contract refuses)."""
    birth: CentralBirth | None
    installation: Installation | None
    room_lights: RoomLightsListing | None
    releases: Releases | None
    versions: Versions | None
    states_epoch: str | None

    def players(self) -> dict[str, PlayerListing]:
        return {player["serial"]: player for player in (self.installation or {}).get("players", ())}

    def frames(self) -> dict[str, FrameListing]:
        return {frame["id"]: frame for frame in (self.installation or {}).get("frames", ())}

    def latest(self) -> str | None:
        """The newest stable release Central holds ready (releases are listed newest first)."""
        return next((release["version"] for release in (self.releases or {}).get("releases", ())
                     if release["stable"] and release["readiness"] == "ready"), None)


class CentralCoordinator(DataUpdateCoordinator[CentralSnapshot]):
    """Central's state keys, decoded, every POLL_SECONDS. A states epoch other than the one the
    publisher last published every state into publishes them all again."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, door: CentralDoor, publisher: StatePublisher) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} central", update_interval=_INTERVAL)
        self.door = door
        self._publisher = publisher

    async def _value(self, key: str) -> Any:
        raw = await self.door.read(key)
        if raw is None:
            return None
        try:
            return decode_central_state(key, raw)
        except ValueError:
            _LOGGER.warning("Central's %s is not one this Galleria reads", key)
            return None

    async def _async_update_data(self) -> CentralSnapshot:
        try:
            values = {key: await self._value(key) for key in (
                CENTRAL_BIRTH_KEY, INSTALLATION_KEY, ROOM_LIGHTS_KEY, RELEASES_KEY, VERSIONS_KEY, STATES_EPOCH_KEY)}
        except DoorUnavailable as error:
            raise UpdateFailed(f"Central's door: {error}") from error
        epoch = values[STATES_EPOCH_KEY]["epoch"] if values[STATES_EPOCH_KEY] is not None else None
        if epoch is not None and epoch != self._publisher.published_epoch:
            await self._publisher.publish_all()
        return CentralSnapshot(birth=values[CENTRAL_BIRTH_KEY], installation=values[INSTALLATION_KEY],
                               room_lights=values[ROOM_LIGHTS_KEY], releases=values[RELEASES_KEY],
                               versions=values[VERSIONS_KEY], states_epoch=epoch)


class PiCoordinator(DataUpdateCoordinator[PiReading]):
    """One Pi, read through its door every POLL_SECONDS at the address Central lists for it."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, serial: str, address: str | None) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} pi {serial}", update_interval=_INTERVAL)
        self.serial = serial
        self.address = address

    async def _async_update_data(self) -> PiReading:
        if self.address is None:
            raise UpdateFailed(f"Central lists no address for Pi {self.serial}")
        reading = await read_pi(self.serial, self.address)
        if reading == "pi_unreachable":
            raise UpdateFailed(f"Pi {self.serial} at {self.address} is unreachable")
        issue = f"unreadable_player_{self.serial}"
        if reading.player_birth == "unreadable":
            ir.async_create_issue(self.hass, DOMAIN, issue, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                                  translation_key="unreadable_player", translation_placeholders={"serial": self.serial})
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue)
        return reading

    @callback
    def async_update_listeners(self) -> None:
        super().async_update_listeners()
        async_dispatcher_send(self.hass, pi_signal(self.config_entry.entry_id), self.serial)


@dataclass
class GalleriaRuntime:
    """The entry's runtime data: Central's origin, its door, the publisher and the readers."""
    origin: str
    door: CentralDoor
    publisher: StatePublisher
    central: CentralCoordinator
    pis: dict[str, PiCoordinator] = field(default_factory=dict)

    @callback
    def sync_pis(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """One PiCoordinator per Pi in Central's installation, at the address it lists now."""
        if self.central.data is None:
            return
        for serial, player in self.central.data.players().items():
            reader = self.pis.get(serial)
            if reader is None:
                reader = self.pis[serial] = PiCoordinator(hass, entry, serial, player["address"])
                entry.async_create_task(hass, reader.async_refresh(), f"{DOMAIN} first read of {serial}")
            elif reader.address != player["address"]:
                reader.address = player["address"]


type GalleriaConfigEntry = ConfigEntry[GalleriaRuntime]
