"""The worker's side of every Node's bus, composed from the environment (E3b design §7.2 rows "Hub
runner and Fleet generator", "NodeLink supervisors", §9.6; errata E-E3D-CUT-7, E-E3D-CC1-1).

With `PHOTO_WALL_HUB_URL` set, the worker runs one `NodeBus` beside its media loop and job runtime:
Fleet's keeper of the hub and the fleet and show NodeLink supervisors, long-lived tasks in the one
worker process (never a worker per Node or job kind). Without it no hub is deployed and the worker
runs as before.

Stop order: FleetHub first, then the supervisors. Every look points the supervisors at the enrolled
set and starts any link not running, so a look still in flight after the supervisors stopped would
start links no supervisor stops (E-E3D-CC1-1).
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from central.db import Database
from central.fleet.node_bus_hub import HUB_LISTENERS, FleetHub
from central.infra.node_link_store import LINK_STORE_CONCURRENCY, PgLinkStores, PgWallMarks
from central.infra.node_links import NodeLinks
from contracts.node_link import Pipe
from nodeapi.buffers import KeyTable

HUB_URL_ENV: Final = "PHOTO_WALL_HUB_URL"          # the worker's client URL of the hub, e.g. nats://photo-wall-hub:4222
HUB_CONFIG_ENV: Final = "PHOTO_WALL_HUB_CONFIG"    # the shared configuration file
DEFAULT_HUB_CONFIG: Final = Path("/var/lib/photo-wall/hub/hub.json")
WALL_TABLE: Final = KeyTable({"timing": 4096})    # placeholder until E8 freezes the wall table in contracts (§10; E-E3D-CUT-7)


class _NoDocuments:
    """Central's desired documents and wall documents: none until E5 and E8 project them."""

    async def documents(self, stream: str) -> Mapping[str, bytes]:
        return {}

    async def changed(self) -> None:
        await asyncio.Event().wait()   # nothing here ever changes

    async def wall_documents(self) -> Mapping[str, bytes]:
        return {}


class NodeBus:
    """The worker's side of every Node's bus: FleetHub and the fleet and show NodeLinks."""

    def __init__(self, db: Database, fleet: FleetHub, links: Sequence[NodeLinks]) -> None:
        self._db = db
        self._fleet = fleet
        self._links = tuple(links)

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
        for task in (fleet, *supervisors):
            task.add_done_callback(ended)
        stopping = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait((stopping, fleet, *supervisors), return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            fleet_stop.set()
            await asyncio.wait((fleet,))
            links_stop.set()
            await asyncio.wait(supervisors)
            for links in self._links:   # a supervisor that failed no longer stops what a last look started
                with contextlib.suppress(Exception):
                    await links.track(())
            await asyncio.to_thread(self._db.close)
        if failures:
            raise failures[0]


def build_node_bus(dsn: str, env: Mapping[str, str]) -> NodeBus | None:
    """The worker's NodeBus over its own database pool, or None without HUB_URL_ENV (no hub
    deployed). Documents and wall documents are empty until E5 and E8."""
    hub_url = env.get(HUB_URL_ENV)
    if not hub_url:
        return None
    db = Database(dsn, pool_size=LINK_STORE_CONCURRENCY + 2)
    stores = PgLinkStores(db)
    nothing = _NoDocuments()
    links = tuple(NodeLinks(pipe, hub_url, stores, nothing) for pipe in (Pipe.FLEET, Pipe.SHOW))
    fleet = FleetHub(db, hub_url, Path(env.get(HUB_CONFIG_ENV) or DEFAULT_HUB_CONFIG), HUB_LISTENERS,
                     WALL_TABLE, PgWallMarks(db, nothing), links)
    return NodeBus(db, fleet, links)
