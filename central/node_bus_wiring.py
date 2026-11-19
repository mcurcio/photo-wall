"""The worker's side of every Node's bus and of Central on its hub, composed from the environment.

With `PHOTO_WALL_HUB_URL` set, the worker runs one `NodeBus` beside its media loop and job runtime:
Fleet's keeper of the hub, the fleet and show NodeLink supervisors, and Central's hub session
(nodeapi.hub `run_hub_session`: Central's state and its installPlayer method for Home Assistant's
door, and Home Assistant's states heard by central/infra/home_assistant/states.py), long-lived tasks
in the one worker process (never a worker per Node or job kind). Without it no hub is deployed and
the worker runs as before.

The room-lights document rides WALL (`ROOM_LIGHTS_KEY`), the same for every Pi and never a link's
desired bucket or the control State: `RoomLightsWall` is that key's one writer, woken by
`NOTIFY room_lights` (every recorded change, from any process: Home Assistant's states, a House ›
Lights edit, a Pi's sensor), when WALL is ensured, and every WALL_RECHECK_SECONDS, which heals a
put that failed; WallWriter itself never writes it. The same NOTIFY wakes Central's hub session. A
second LISTEN hears a Player state commit (PLAYER_STATE_CHANNEL: every Coordination and Registry
write, a bind, an enrolment, a retirement) and a release write (RELEASES_CHANNEL: a release, a
deployment, an Install) and wakes both Central's hub session and FleetHub's look; with no wake they
look again only every CENTRAL_STATE_LOOK_SECONDS and HUB_LOOK_SECONDS.

Stop order: FleetHub and Central's hub session first, then the supervisors. Every look points the supervisors at the enrolled
set and starts any link not running, so a look still in flight after the supervisors stopped would
start links no supervisor stops.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Final, Protocol

from central import cache_layout
from central.content_wiring import build_release_services
from central.coordination import OutputLossLedger
from central.db import Database, DatabaseTransactionClock, PoolName
from central.fleet.node_bus_hub import HUB_LISTENERS, FleetHub
from central.fleet.node_calibration import NodeCalibration, TrialReportJudge
from central.fleet.node_release_catalog import NodeReleaseCatalog
from central.fleet.pi_reboot import BusPiReboot, PiReboot
from central.frame_lighting.paper_profiles import LUT_RECIPE_ID
from central.hub_api import central_state, hub_methods
from central.infra.central_state import PgCentralStateReads, PgStateWakes
from central.infra.display_store import (
    REPORT_STREAM,
    DisplayDocuments,
    DisplayWakes,
    OutputReportJudge,
)
from central.infra.frame_lighting_store import PgFrameLighting, PgHouseLock
from central.infra.home_assistant.states import HomeAssistantListener
from central.infra.light_sensor_judge import HOST_STATE_STREAM, LightSensorJudge
from central.infra.listen import listen
from central.infra.node_link_store import (
    LINK_STORE_CONCURRENCY,
    AllJudges,
    PgLinkStores,
    PgWallMarks,
)
from central.infra.node_links import NodeLinks
from central.infra.node_releases import RELEASES_CHANNEL
from central.infra.player_state_wakes import PLAYER_STATE_CHANNEL
from central.infra.player_versions import InstallEnds, PgPlayerVersions
from central.infra.room_light_store import ROOM_LIGHTS_CHANNEL, PgRoomLights
from central.registry import Registry
from central.room_light.ports import RoomLightsDocuments
from central.version import central_version
from contracts.node_link import Pipe
from contracts.room_light import ROOM_LIGHTS_BYTES, ROOM_LIGHTS_KEY, encode_room_lights
from contracts.time import SystemClock
from nodeapi.buffers import KeyTable
from nodeapi.hub import HubUnavailable, WallWriter, run_hub_session

log = logging.getLogger(__name__)

HUB_URL_ENV: Final = "PHOTO_WALL_HUB_URL"          # the worker's client URL of the hub, e.g. nats://photo-wall-hub:4222
HUB_CONFIG_ENV: Final = "PHOTO_WALL_HUB_CONFIG"    # the shared configuration file
DEFAULT_HUB_CONFIG: Final = Path("/var/lib/photo-wall/hub/hub.json")
# "timing" is a placeholder until the wall table is frozen in contracts.
WALL_TABLE: Final = KeyTable({"timing": 4096, ROOM_LIGHTS_KEY: ROOM_LIGHTS_BYTES})
WALL_RECHECK_SECONDS: Final = 5.0     # how often the writer looks again for a wall it could not put to yet
_LISTEN_RETRY_SECONDS: Final = 1.0
_HUB_SESSION_RETRY_SECONDS: Final = 2.0   # the hub session's first connect, tried again until it answers
_LISTEN_APPLICATION: Final[PoolName] = "photo-wall-room-lights"
_FLEET_LISTEN_APPLICATION: Final[PoolName] = "photo-wall-fleet-wakes"


class WallSource(Protocol):
    """Where WALL's writer is: None until WALL is ensured (FleetHub)."""

    @property
    def wall(self) -> WallWriter | None: ...


class RoomLightsWall:
    """The room-lights key's one writer: puts the current document on WALL at every wake (`wake`,
    from any thread: NOTIFY room_lights, WALL ensured) and
    every WALL_RECHECK_SECONDS. WallWriter.put is content-keyed, so a put of the document WALL
    already holds writes nothing; a put that fails is tried again at the next wake or recheck."""

    def __init__(self, documents: RoomLightsDocuments) -> None:
        self._documents = documents
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None

    def wake(self) -> None:
        """The document may have changed: put it again soon. Safe from any thread."""
        loop, event = self._loop, self._event
        if loop is not None and event is not None:
            with contextlib.suppress(RuntimeError):   # that loop is closed: nobody waits there any more
                loop.call_soon_threadsafe(event.set)

    def wall_created(self) -> None:
        """WALL was ensured: put the document there."""
        self.wake()

    async def run(self, stop: asyncio.Event, wall: WallSource) -> None:
        """Until `stop`: put the current document on every wake and every WALL_RECHECK_SECONDS."""
        self._loop, self._event = asyncio.get_running_loop(), asyncio.Event()
        self._event.set()
        stopping = asyncio.ensure_future(stop.wait())
        try:
            while not stop.is_set():
                waking = asyncio.ensure_future(self._event.wait())
                await asyncio.wait((waking, stopping), timeout=WALL_RECHECK_SECONDS,
                                   return_when=asyncio.FIRST_COMPLETED)
                waking.cancel()
                self._event.clear()
                writer = wall.wall
                if writer is None or stop.is_set():
                    continue
                try:
                    document = await asyncio.to_thread(self._documents.current)
                    await writer.put(ROOM_LIGHTS_KEY, encode_room_lights(document))
                except Exception as error:   # the hub or the database away: the recheck tries again
                    log.warning("room lights: not put on WALL yet: %s", error)
        finally:
            stopping.cancel()
            self._loop = self._event = None


class NodeBus:
    """The worker's side of every Node's bus: FleetHub, the fleet and show NodeLinks, and the one
    LISTEN connection that wakes their Output documents on another process's command."""

    def __init__(self, db: Database, fleet: FleetHub, links: Sequence[NodeLinks], wakes: DisplayWakes,
                 room_lights: RoomLightsWall, *, hub_session: Callable[[asyncio.Event], Awaitable[None]],
                 state_wakes: PgStateWakes) -> None:
        self._db = db
        self._fleet = fleet
        self._links = tuple(links)
        self._wakes = wakes
        self.room_lights = room_lights
        self._hub_session = hub_session
        self._state_wakes = state_wakes

    def pi_reboot(self) -> PiReboot:
        """A Pi's reboot over its fleet-pipe link (the worker's `RebootPi` handler's)."""
        [fleet] = (links for links in self._links if links.pipe is Pipe.FLEET)
        return BusPiReboot(fleet)

    def _room_lights_changed(self) -> None:
        self.room_lights.wake()
        self._state_wakes.wake()

    def _fleet_changed(self) -> None:
        """A Player state or release commit: Central's state and FleetHub look again."""
        self._state_wakes.wake()
        self._fleet.wake()

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
        walling = asyncio.create_task(self.room_lights.run(fleet_stop, self._fleet), name="node-bus room lights")
        serving = asyncio.create_task(self._hub_session(fleet_stop), name="node-bus central on its hub")
        for task in (fleet, *supervisors, walling, serving):
            task.add_done_callback(ended)
        stopping = asyncio.create_task(stop.wait())
        listening = asyncio.create_task(self._wakes.listen(self._db.dsn), name="node-bus document wakes")
        room_listening = asyncio.create_task(listen(
            self._db.dsn, ROOM_LIGHTS_CHANNEL, application=_LISTEN_APPLICATION, connected=self._room_lights_changed,
            notified=lambda _payload: self._room_lights_changed(), retry_seconds=_LISTEN_RETRY_SECONDS),
            name="node-bus room light wakes")
        fleet_listening = asyncio.create_task(listen(
            self._db.dsn, (PLAYER_STATE_CHANNEL, RELEASES_CHANNEL), application=_FLEET_LISTEN_APPLICATION,
            connected=self._fleet_changed, notified=lambda _payload: self._fleet_changed(),
            retry_seconds=_LISTEN_RETRY_SECONDS), name="node-bus fleet wakes")
        try:
            await asyncio.wait((stopping, fleet, *supervisors, walling, serving), return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            listening.cancel()
            room_listening.cancel()
            fleet_listening.cancel()
            fleet_stop.set()
            await asyncio.wait((walling, serving))
            await asyncio.wait((fleet,))
            links_stop.set()
            await asyncio.wait(supervisors)
            for links in self._links:   # a supervisor that failed no longer stops what a last look started
                with contextlib.suppress(Exception):
                    await links.track(())
            await asyncio.wait((listening, room_listening, fleet_listening))
            await asyncio.to_thread(self._db.close)
        if failures:
            raise failures[0]


async def serve_hub(url: str, stop: asyncio.Event, **session: object) -> None:
    """Central's hub session until `stop`: `run_hub_session`, whose first connect is tried again every
    _HUB_SESSION_RETRY_SECONDS while the hub does not answer (once connected it reconnects forever)."""
    while not stop.is_set():
        try:
            await run_hub_session(url, stop=stop, **session)  # type: ignore[arg-type]
            return
        except HubUnavailable as error:
            log.info("central on its hub: %s", error)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), _HUB_SESSION_RETRY_SECONDS)


def build_node_bus(dsn: str, env: Mapping[str, str]) -> NodeBus | None:
    """The worker's NodeBus over its own database pool, or None without HUB_URL_ENV (no hub
    deployed). Both pipes' links take their documents from `DisplayDocuments` (the fleet pipe's
    Output, region, picture and Position trial documents; the show pipe's are empty), and the display
    line's Output reports are judged as they are recorded (`OutputReportJudge`, which keeps each
    Output's loss through `OutputLossLedger`), then its trial reports (`TrialReportJudge`); a console command in Central's API process wakes the
    documents through `DisplayWakes.listen`. A Pi's light meter on the host line is judged as it is
    recorded (`LightSensorJudge`; each Player's drawn report is read where its line is recorded), and
    a recorded host base wakes the look that ends a Pi's install (`InstallEnds`);
    WALL carries the room-lights document (`RoomLightsWall`). Central's hub session serves its state
    and installPlayer (`PgPlayerVersions`, whose running installs move on at each FleetHub look) and
    hands Home Assistant's states to `HomeAssistantListener`. `NodeBus.pi_reboot()` asks a Pi's host
    to reboot over the fleet pipe's links (the worker's `RebootPi` handler)."""
    hub_url = env.get(HUB_URL_ENV)
    if not hub_url:
        return None
    db = Database(dsn, application_name="photo-wall-bus", pool_size=LINK_STORE_CONCURRENCY + 4)
    clock = SystemClock()
    wakes = DisplayWakes()
    room_lights = PgRoomLights(db, PgHouseLock())
    # The output judge first: it takes Coordination and Runtime when a loss is due, then `players` and
    # `outputs`; the trial judge locks only `frame_trials` rows, last.
    stores = PgLinkStores(db, {REPORT_STREAM: AllJudges(OutputReportJudge(wakes, losses=OutputLossLedger()),
                                                        TrialReportJudge()),
                               HOST_STATE_STREAM: AllJudges(LightSensorJudge(room_lights), InstallEnds())})
    lighting = PgFrameLighting(LUT_RECIPE_ID)
    trials = NodeCalibration(db, registry=Registry(db, clock, lighting=lighting), lighting=lighting)
    documents = DisplayDocuments(stores, wakes, times=DatabaseTransactionClock(), trials=trials)
    links = tuple(NodeLinks(pipe, hub_url, stores, documents) for pipe in (Pipe.FLEET, Pipe.SHOW))
    wall = RoomLightsWall(room_lights)
    readiness, publisher = build_release_services(db, clock, cache_root=cache_layout.cache_root())
    versions = PgPlayerVersions(db, clock, readiness, publisher)
    state_wakes = PgStateWakes()
    reads = PgCentralStateReads(db, clock, versions, room_lights,
                                NodeReleaseCatalog(db, clock, readiness=readiness).view)
    fleet = FleetHub(db, hub_url, Path(env.get(HUB_CONFIG_ENV) or DEFAULT_HUB_CONFIG), HUB_LISTENERS,
                     WALL_TABLE, PgWallMarks(db, wall.wall_created), links, each_look=(versions.advance_installs,))

    def hub_session(stop: asyncio.Event) -> Awaitable[None]:
        return serve_hub(hub_url, stop, version=central_version(), state=central_state(reads, state_wakes),
                         listened=HomeAssistantListener(room_lights, room_lights, room_lights), methods=hub_methods(versions))

    return NodeBus(db, fleet, links, wakes, wall, hub_session=hub_session, state_wakes=state_wakes)
