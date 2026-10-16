"""The worker's side of every Node's bus, composed from the environment (E3b design §7.2 rows "Hub
runner and Fleet generator", "NodeLink supervisors", §9.6; errata E-E3D-CUT-7, E-E3D-CC1-1).

With `PHOTO_WALL_HUB_URL` set, the worker runs one `NodeBus` beside its media loop and job runtime:
Fleet's keeper of the hub and the fleet and show NodeLink supervisors, long-lived tasks in the one
worker process (never a worker per Node or job kind). Without it no hub is deployed and the worker
runs as before.

Room light (decision 0028): the room-lights document rides WALL (`ROOM_LIGHTS_KEY`), the same for
every Pi and never a link's desired bucket or the control State: `RoomLightsWall` puts it whenever it
changes (the Home Assistant link's `on_recorded` in this process, a House > Lights edit or a Pi's
meter through `NOTIFY room_lights`) and hands it to WallWriter when WALL is re-created.

Stop order: FleetHub first, then the supervisors. Every look points the supervisors at the enrolled
set and starts any link not running, so a look still in flight after the supervisors stopped would
start links no supervisor stops (E-E3D-CC1-1).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

import psycopg

from central.db import Database, DatabaseTransactionClock
from central.fleet.node_bus_hub import HUB_LISTENERS, FleetHub
from central.infra.display_store import (
    REPORT_STREAM,
    DisplayDocuments,
    DisplayWakes,
    OutputReportJudge,
)
from central.infra.node_link_store import LINK_STORE_CONCURRENCY, PgLinkStores, PgWallMarks
from central.infra.node_links import NodeLinks
from central.infra.room_light_store import (
    HOST_STATE_STREAM,
    ROOM_LIGHTS_CHANNEL,
    LightSensorJudge,
    PgRoomLights,
)
from central.room_light.ports import RoomLightsDocuments
from contracts.node_link import Pipe
from contracts.room_light import ROOM_LIGHTS_BYTES, ROOM_LIGHTS_KEY, encode_room_lights
from nodeapi.buffers import KeyTable
from nodeapi.hub import WallWriter

log = logging.getLogger(__name__)

HUB_URL_ENV: Final = "PHOTO_WALL_HUB_URL"          # the worker's client URL of the hub, e.g. nats://photo-wall-hub:4222
HUB_CONFIG_ENV: Final = "PHOTO_WALL_HUB_CONFIG"    # the shared configuration file
DEFAULT_HUB_CONFIG: Final = Path("/var/lib/photo-wall/hub/hub.json")
# "timing" is a placeholder until E8 freezes the wall table in contracts (§10; E-E3D-CUT-7).
WALL_TABLE: Final = KeyTable({"timing": 4096, ROOM_LIGHTS_KEY: ROOM_LIGHTS_BYTES})
WALL_RECHECK_SECONDS: Final = 5.0     # how often the writer looks again for a wall it could not put to yet
_LISTEN_RETRY_SECONDS: Final = 1.0
_LISTEN_APPLICATION: Final = "photo-wall-room-lights"


class RoomLightsWall:
    """Central's wall documents (`WallDocuments`: the room-lights document) and their writer: puts
    the current document on WALL whenever it may have changed (`wake`, from any thread; `listen`,
    other processes' NOTIFY) and once the wall is there. `wall` is FleetHub's writer (None until WALL
    is ensured). A put that fails is tried again at the next wake or recheck."""

    def __init__(self, documents: RoomLightsDocuments, wall: Callable[[], WallWriter | None]) -> None:
        self._documents = documents
        self._wall = wall
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None

    async def wall_documents(self) -> Mapping[str, bytes]:
        document = await asyncio.to_thread(self._documents.current)
        return {ROOM_LIGHTS_KEY: encode_room_lights(document)}

    def wake(self) -> None:
        """The document may have changed: put it again soon. Safe from any thread."""
        loop, event = self._loop, self._event
        if loop is not None and event is not None:
            with contextlib.suppress(RuntimeError):   # that loop is closed: nobody waits there any more
                loop.call_soon_threadsafe(event.set)

    async def listen(self, dsn: str) -> None:
        """Until cancelled: `LISTEN ROOM_LIGHTS_CHANNEL` on its own connection and wake on each
        notification, and on every (re)connect (notifications sent while none listened are lost)."""
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                        dsn, autocommit=True, application_name=_LISTEN_APPLICATION) as conn:
                    await conn.execute(f"LISTEN {ROOM_LIGHTS_CHANNEL}")
                    self.wake()
                    async for _ in conn.notifies():
                        self.wake()
            except Exception as error:   # the writer keeps its recheck
                log.warning("room lights LISTEN connection lost: %s", error)
            await asyncio.sleep(_LISTEN_RETRY_SECONDS)

    async def run(self, stop: asyncio.Event) -> None:
        """Until `stop`: put the document whenever it, or the wall writer, is not the one last put."""
        self._loop, self._event = asyncio.get_running_loop(), asyncio.Event()
        self._event.set()
        last: tuple[int, int] | None = None
        stopping = asyncio.ensure_future(stop.wait())
        try:
            while not stop.is_set():
                waking = asyncio.ensure_future(self._event.wait())
                await asyncio.wait((waking, stopping), timeout=WALL_RECHECK_SECONDS,
                                   return_when=asyncio.FIRST_COMPLETED)
                waking.cancel()
                self._event.clear()
                writer = self._wall()
                if writer is None or stop.is_set():
                    continue
                try:
                    document = await asyncio.to_thread(self._documents.current)
                    if last == (id(writer), document.change):
                        continue
                    await writer.put(ROOM_LIGHTS_KEY, encode_room_lights(document))
                    last = (id(writer), document.change)
                except Exception as error:   # the hub or the database away: the recheck tries again
                    log.info("room lights: not put on WALL yet: %s", error)
        finally:
            stopping.cancel()
            self._loop = self._event = None


class NodeBus:
    """The worker's side of every Node's bus: FleetHub, the fleet and show NodeLinks, and the one
    LISTEN connection that wakes their Output documents on another process's command."""

    def __init__(self, db: Database, fleet: FleetHub, links: Sequence[NodeLinks], wakes: DisplayWakes,
                 room_lights: RoomLightsWall) -> None:
        self._db = db
        self._fleet = fleet
        self._links = tuple(links)
        self._wakes = wakes
        self.room_lights = room_lights

    async def run(self, stop: asyncio.Event) -> None:
        """Until `stop` or the first failure; then FleetHub stops, then both supervisors, then the
        bus's database closes. Raises the first failure."""
        failures: list[BaseException] = []

        def ended(task: asyncio.Task[None]) -> None:
            if not task.cancelled() and task.exception() is not None:
                failures.append(task.exception())

        fleet_stop, links_stop = asyncio.Event(), asyncio.Event()
        fleet = asyncio.create_task(self._fleet.run(fleet_stop), name="node-bus fleet hub")
        supervisors = [asyncio.create_task(links.run(links_stop), name="node-bus links")
                       for links in self._links]
        walling = asyncio.create_task(self.room_lights.run(fleet_stop), name="node-bus room lights")
        for task in (fleet, *supervisors, walling):
            task.add_done_callback(ended)
        stopping = asyncio.create_task(stop.wait())
        listening = asyncio.create_task(self._wakes.listen(self._db.dsn), name="node-bus document wakes")
        room_listening = asyncio.create_task(self.room_lights.listen(self._db.dsn), name="node-bus room light wakes")
        try:
            await asyncio.wait((stopping, fleet, *supervisors, walling), return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            listening.cancel()
            room_listening.cancel()
            fleet_stop.set()
            await asyncio.wait((walling,))
            await asyncio.wait((fleet,))
            links_stop.set()
            await asyncio.wait(supervisors)
            for links in self._links:   # a supervisor that failed no longer stops what a last look started
                with contextlib.suppress(Exception):
                    await links.track(())
            await asyncio.wait((listening, room_listening))
            await asyncio.to_thread(self._db.close)
        if failures:
            raise failures[0]


def build_node_bus(dsn: str, env: Mapping[str, str]) -> NodeBus | None:
    """The worker's NodeBus over its own database pool, or None without HUB_URL_ENV (no hub
    deployed). Both pipes' links take their documents from `DisplayDocuments` (the fleet pipe's
    Output documents; the show pipe's are empty), and the display line's Output reports are judged
    as they are recorded (`OutputReportJudge`); a console command in Central's API process wakes the
    documents through `DisplayWakes.listen`. A Pi's light meter on the host line is judged as it is
    recorded (`LightSensorJudge`); WALL carries the room-lights document (`RoomLightsWall`)."""
    hub_url = env.get(HUB_URL_ENV)
    if not hub_url:
        return None
    db = Database(dsn, pool_size=LINK_STORE_CONCURRENCY + 2)
    wakes = DisplayWakes()
    room_lights = PgRoomLights(db)
    stores = PgLinkStores(db, {REPORT_STREAM: OutputReportJudge(wakes),
                               HOST_STATE_STREAM: LightSensorJudge(room_lights)})
    documents = DisplayDocuments(stores, wakes, times=DatabaseTransactionClock())
    links = tuple(NodeLinks(pipe, hub_url, stores, documents) for pipe in (Pipe.FLEET, Pipe.SHOW))
    fleet: FleetHub | None = None
    wall = RoomLightsWall(room_lights, lambda: None if fleet is None else fleet.wall)
    fleet = FleetHub(db, hub_url, Path(env.get(HUB_CONFIG_ENV) or DEFAULT_HUB_CONFIG), HUB_LISTENERS,
                     WALL_TABLE, PgWallMarks(db, wall), links)
    return NodeBus(db, fleet, links, wakes, wall)
