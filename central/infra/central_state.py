"""Central's state on its hub, read from Central's own services (central/hub_api.py CentralStateReads
and StateWakes; contracts/central_api.py): each read is its own read-only snapshot through the
service the console's route for it calls, and none writes.

| Key | Read |
|---|---|
| installation | the registry's inventory (GET /v1/installation), each bound Frame's Pi by its device's serial, and every enrolled Pi with the IPv4 address its host line states (`facts`, central/fleet/node_observations.py `host_facts_in`) |
| listening | the room-light setup's references of Home Assistant's source (House › Lights) |
| room-lights | each room light in House › Lights order, its name, and the room-lights document's entry, at the document's change (the WALL writer's `RoomLightsDocuments.current`) |
| releases | the Releases page's read (GET /v1/operator/releases), its newest RELEASES_LISTED deployable releases |
| versions | `PlayerVersions.versions` |

The wakes (`wake`, from the worker's LISTENs, central/node_bus_wiring.py): the room-lights
document's NOTIFY, a Player state commit's (PLAYER_STATE_CHANNEL: a bind, an enrolment, a
retirement, every Coordination and Registry write) and a release's (RELEASES_CHANNEL: a release, a
deployment, an Install), from any Central process; else a look every CENTRAL_STATE_LOOK_SECONDS,
which bounds a missed wake and covers what sends none (a download finishing, an address HostCore
reports).
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from typing import Final

from central.content_catalog.ports import PlayerVersions
from central.content_catalog.views import ReleasesView
from central.db import Database
from central.fleet.node_bus_hub import enrolled_devices_in
from central.fleet.node_observations import host_facts_in
from central.frame_lighting.paper_profiles import LUT_RECIPE_ID
from central.infra.frame_lighting_store import PgFrameLighting
from central.infra.home_assistant.references import SOURCE
from central.infra.room_light_rows import lights_in
from central.infra.room_light_store import named_in
from central.registry import Registry
from central.room_light.ports import RoomLightsDocuments
from contracts.central_api import (
    RELEASES_LISTED,
    DrivenBy,
    FrameListing,
    Installation,
    Listening,
    PlayerListing,
    ReleaseListing,
    Releases,
    RoomLightListing,
    RoomLightsListing,
    Versions,
)
from contracts.time import Clock

CENTRAL_STATE_LOOK_SECONDS: Final = 60.0   # how long Central's hub session waits for a wake before it looks again


class PgCentralStateReads:
    """hub_api.CentralStateReads over Central's database and services."""

    def __init__(self, db: Database, clock: Clock, versions: PlayerVersions, documents: RoomLightsDocuments,
                 releases: Callable[[], ReleasesView]) -> None:
        self._db = db
        self._clock = clock
        self._versions = versions
        self._documents = documents
        self._releases = releases
        self._registry = Registry(db, clock, lighting=PgFrameLighting(LUT_RECIPE_ID))

    def installation(self) -> Installation:
        with self._db.read_snapshot() as conn:
            now = self._clock.utc()
            inventory = self._registry.inventory_in(conn, now)
            serials = {device_id: serial for serial, device_id in enrolled_devices_in(conn).items()}
            facts = host_facts_in(conn)
        device_of = {player.id: player.device_id for player in inventory.players if player.retired_at is None}
        addresses = {device_id: each.get("address") for device_id, each in facts.items()}

        def driven_by(frame) -> DrivenBy | None:
            serial = serials.get(device_of.get(frame.player_id, ""))
            if serial is None or frame.output_id is None or frame.generation < 1:
                return None
            return DrivenBy(serial=serial, output_id=frame.output_id, generation=frame.generation)

        return Installation(
            frames=tuple(FrameListing(id=frame.id, name=frame.name, driven_by=driven_by(frame))
                         for frame in inventory.frames),
            players=tuple(PlayerListing(serial=serial, name=serial, address=addresses.get(device_id))
                          for device_id, serial in sorted(serials.items(), key=lambda pair: pair[1])))

    def listening(self) -> Listening:
        with self._db.read_snapshot() as conn:
            named = named_in(conn, SOURCE)
        return Listening(entities=tuple(sorted({reference.address for reference in named})))

    def room_lights(self) -> RoomLightsListing:
        document = self._documents.current()
        with self._db.read_snapshot() as conn:
            names = {light_id: light.name for light_id, light in lights_in(conn).items()}
        return RoomLightsListing(change=document.change, lights=tuple(
            RoomLightListing(id=light_id, name=name, entry=document.lights[light_id])
            for light_id, name in names.items() if light_id in document.lights))

    def releases(self) -> Releases:
        listed = [ReleaseListing(version=release.version, stable=release.stable, readiness=release.readiness)
                  for release in self._releases().releases if release.readiness is not None]
        return Releases(releases=tuple(listed[:RELEASES_LISTED]))

    def versions(self) -> Versions:
        return self._versions.versions()


class PgStateWakes:
    """hub_api.StateWakes: `changed` returns at a `wake` (something Central's state is read from
    changed, from any thread) or after CENTRAL_STATE_LOOK_SECONDS, whichever is first. A wake is
    kept until the next `changed` takes it, whenever it came: before the first `changed` (the loop
    not known yet) or while the caller reads, so a commit's NOTIFY is never lost to the ceiling."""

    def __init__(self, look_seconds: float = CENTRAL_STATE_LOOK_SECONDS) -> None:
        self._look = look_seconds
        self._lock = threading.Lock()
        self._woken = False   # a wake no `changed` has returned for yet
        self._waiter: tuple[asyncio.AbstractEventLoop, asyncio.Event] | None = None

    def wake(self) -> None:
        """Something Central's state reads may have changed: look again now. Safe from any thread."""
        with self._lock:
            self._woken = True
            waiter, self._waiter = self._waiter, None
        if waiter is not None:
            loop, event = waiter
            with contextlib.suppress(RuntimeError):   # that loop is closed: nobody waits there any more
                loop.call_soon_threadsafe(event.set)

    async def changed(self) -> None:
        event = asyncio.Event()
        with self._lock:
            waiting = not self._woken
            if waiting:
                self._waiter = (asyncio.get_running_loop(), event)
        if waiting:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), self._look)
        with self._lock:   # every wake until now is covered by the read that follows this return
            self._woken, self._waiter = False, None


__all__ = ["CENTRAL_STATE_LOOK_SECONDS", "PgCentralStateReads", "PgStateWakes"]
