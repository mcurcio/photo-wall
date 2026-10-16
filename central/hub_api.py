"""Central's API on its own hub, a composition-root module like the *_routes.py modules: Central's
state keys and its one method (contracts/central_api.py), each answered by the same Central service
the console's route for it calls (Q3 A, 2026-10-16: "Only what Home Assistant uses moves onto NATS;
the console stays on HTTP"). The worker runs it through nodeapi.hub `run_hub_session`
(central/node_bus_wiring.py).

| Key or method | Contract | The service it calls (the console's route calls the same) |
|---|---|---|
| installation | Installation | the registry's Frames and bindings, the enrolled Pis, each Pi's last reported address |
| listening | Listening | the room-light setup's references of Home Assistant's source |
| room-lights | RoomLightsListing | the room lights and the room-lights document (`RoomLightsDocuments.current`) |
| releases | Releases | the release read behind GET /v1/operator/releases (its newest RELEASES_LISTED) |
| versions | Versions | `PlayerVersions.versions` (GET /v1/operator/releases' players) |
| installPlayer | InstallPlayer -> InstallAnswer | `PlayerVersions.install` (POST /v1/operator/players/{serial}/install) |
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Protocol

from central.content_catalog.ports import PlayerVersions, VersionRefused
from contracts.central_api import (
    INSTALL_PLAYER,
    INSTALLATION_KEY,
    LISTENING_KEY,
    RELEASES_KEY,
    ROOM_LIGHTS_KEY,
    VERSIONS_KEY,
    Installation,
    InstallRefused,
    InstallStarted,
    Listening,
    Releases,
    RoomLightsListing,
    Versions,
    decode_install_player,
    encode_central_state,
    encode_install_answer,
)
from nodeapi.envelope import MethodCall, MethodHandler
from nodeapi.hub import CentralState

log = logging.getLogger(__name__)


class CentralStateReads(Protocol):
    """One plain read per state key, each through the service the console's route calls. Every read
    is its own read-only snapshot; none writes."""

    def installation(self) -> Installation: ...

    def listening(self) -> Listening: ...

    def room_lights(self) -> RoomLightsListing: ...

    def releases(self) -> Releases: ...

    def versions(self) -> Versions: ...


class StateWakes(Protocol):
    """When any read above may have changed: the database's NOTIFY of a write that changes one (the
    registry, the room-light setup and document, the release catalog, a Pi's version or install), in
    any Central process, or Central's own look every few seconds when no NOTIFY reaches it."""

    async def changed(self) -> None: ...


class _CentralState:
    """nodeapi.hub's CentralState over one Central's reads and wakes."""

    def __init__(self, reads: CentralStateReads, wakes: StateWakes) -> None:
        self._reads = reads
        self._wakes = wakes
        self._returned = False

    async def values(self) -> Mapping[str, bytes]:
        return await asyncio.to_thread(self._encoded)

    def _encoded(self) -> dict[str, bytes]:
        reads = self._reads
        return {INSTALLATION_KEY: encode_central_state(INSTALLATION_KEY, reads.installation()),
                LISTENING_KEY: encode_central_state(LISTENING_KEY, reads.listening()),
                ROOM_LIGHTS_KEY: encode_central_state(ROOM_LIGHTS_KEY, reads.room_lights()),
                RELEASES_KEY: encode_central_state(RELEASES_KEY, reads.releases()),
                VERSIONS_KEY: encode_central_state(VERSIONS_KEY, reads.versions())}

    async def changed(self) -> None:
        if not self._returned:   # at once the first time
            self._returned = True
            return
        await self._wakes.changed()


def central_state(reads: CentralStateReads, wakes: StateWakes) -> CentralState:
    """nodeapi.hub's CentralState over `reads`: `values()` encodes each read under its key
    (contracts.central_api encode_central_state, in a thread: the reads block); `changed()` is
    `wakes.changed()`."""
    return _CentralState(reads, wakes)


def hub_methods(versions: PlayerVersions) -> Mapping[str, MethodHandler]:
    """{INSTALL_PLAYER: handler}: the handler decodes InstallPlayer (an undecodable request answers
    an error reply, never an install), calls `versions.install` in a thread, and answers
    encode_install_answer(InstallStarted) or encode_install_answer(InstallRefused(refusal)) for a
    VersionRefused; the call's caller is logged with the outcome."""

    async def install_player(call: MethodCall) -> bytes:
        request = decode_install_player(call.payload)   # ValueError: an error reply, never an install
        answer: InstallStarted | InstallRefused
        try:
            answer = await asyncio.to_thread(versions.install, request.serial, request.version)
        except VersionRefused as refused:
            answer = InstallRefused(refusal=refused.refusal)
        log.info("installPlayer %s %s from %s: %s", request.serial, request.version, call.caller,
                 answer.install if isinstance(answer, InstallStarted) else answer.refusal.value)
        return encode_install_answer(answer)

    return {INSTALL_PLAYER: install_player}


__all__ = ["CentralStateReads", "StateWakes", "central_state", "hub_methods"]
