"""The console reads whether a Node's leaf is linked to Central's hub (E3d S4; E3b design §13
Deferrals "Presence for the console"; 0017 Open item "Presence", leaf state only: E-E3D-CUT-8).

S2's bed with one enrolled Node: `FleetHub` writes the hub's configuration, a real nats-server hub
starts from it, and the Node is a real bus on the shipped configuration whose leaf dials the hub.
Central's read is `FleetService.status()` with an injected read clock, and its `bus_link` is rendered
by the console's own `busLinkFact` (facts.js, under Node); the Player page's own source, the node
device read, serves the same value (erratum E-E3D-S4-1). 1: the leaf links: linked, the look recent,
the fact derived "linked". 2: the Node bus crashes: not linked with a later changed_at; it restarts:
linked again. 3: FleetHub stops: a read 31 s after its last look renders unknown.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

from integration.bus_servers import BusServer, _free_port, node_server, until
from test_console_flow import _require_node

from central.content_catalog.catalog import device_id_for_serial
from central.db import Database
from central.fleet.node_bus_accounts import HubListeners
from central.fleet.node_bus_hub import HUB_LOOK_SECONDS, PRESENCE_STALE_SECONDS, FleetHub
from central.fleet.node_observations import NodeObservations
from central.fleet.node_sessions import NodeControlConfig, NodeSessions
from central.fleet.service import FleetService
from central.infra.node_link_store import PgWallMarks
from nodeapi.buffers import KeyTable

SERIAL = "serial-presence-1"
DEVICE = device_id_for_serial(SERIAL)
WALL_TABLE = KeyTable({"timing": 4096})
SETTLE_SECONDS = 10
FACTS = Path(__file__).parents[2] / "central/console/src/facts.js"
RENDER = r"""
const { busLinkFact, factText } = await import(process.argv[1]);
const value = busLinkFact(JSON.parse(process.argv[2]), JSON.parse(process.argv[3]));
console.log(JSON.stringify([value.kind, factText(value)]));
"""


class _ReadClock:
    """Central's clock for the read: the system's, or `at` once a leg pins it."""

    def __init__(self) -> None:
        self.at: float | None = None

    def utc(self) -> float:
        return time.time() if self.at is None else self.at

    def monotonic(self) -> float:
        return time.monotonic()


class _WallDocuments:
    async def wall_documents(self) -> dict[str, bytes]:
        return {"timing": b"timing-1"}


def _enrol(db: Database, serial: str) -> None:
    with db.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id, serial, first_seen, last_seen) VALUES (%s, %s, 1, 1)",
                     (device_id_for_serial(serial), serial))


def _rendered(bus_link: dict, read_at: float) -> list[str]:
    """[kind, wording] of the console's `busLinkFact` for this read."""
    result = subprocess.run(["node", "--input-type=module", "-e", RENDER, "--", FACTS.as_uri(),
                             json.dumps(bus_link), json.dumps(read_at)],
                            capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def test_the_console_reads_whether_a_node_is_linked(database, tmp_path):
    _require_node()
    _enrol(database, SERIAL)
    config_dir = tmp_path / "hub-config"
    config_dir.mkdir()
    config_path = config_dir / "hub.json"
    listeners = HubListeners(
        server_name="hub", client_host="127.0.0.1", client_port=_free_port(),
        websocket_host="127.0.0.1", websocket_port=_free_port(), leaf_host="127.0.0.1", leaf_port=_free_port(),
        monitor_port=_free_port(), max_memory_store_bytes=4 * 1024 * 1024)
    hub = BusServer(name="hub", config=config_path, client_url=f"nats://127.0.0.1:{listeners.client_port}",
                    environment={}, websocket_port=listeners.websocket_port,
                    monitor_url=f"http://127.0.0.1:{listeners.monitor_port}", listeners=listeners)
    node = node_server(tmp_path, SERIAL, hub)
    fleet = FleetHub(database, hub.client_url, config_path, listeners, WALL_TABLE,
                     PgWallMarks(database, _WallDocuments()), [])
    clock = _ReadClock()
    service = FleetService(database, clock)
    player_page = NodeObservations(NodeSessions(database, clock, NodeControlConfig("presence-test")))

    async def read() -> tuple[float, dict]:
        status = await asyncio.to_thread(service.status)
        [device] = [device for device in status["devices"] if device["device_id"] == DEVICE]
        return status["read_at"], device["bus_link"]

    def link_is(linked: bool, after: float | None = None):
        async def check():
            read_at, bus_link = await read()
            if bus_link["linked"] is linked and (after is None or bus_link["changed_at"] > after):
                return read_at, bus_link
            return None
        return check

    async def run() -> None:
        stop = asyncio.Event()
        running = asyncio.create_task(fleet.run(stop))
        try:
            await until(lambda: asyncio.sleep(0, config_path.exists()), SETTLE_SECONDS,
                        "Fleet writes the hub's configuration")
            hub.start()
            node.start()

            # 1. The leaf links: Central reads it linked, its look recent, and the console says so.
            read_at, bus_link = await until(link_is(True), SETTLE_SECONDS, "Central reads the Node linked")
            assert bus_link["looked_at"] is not None, "Central records its look at the hub"
            assert read_at - bus_link["looked_at"] <= 2 * HUB_LOOK_SECONDS + 1
            kind, text = _rendered(bus_link, read_at)
            assert kind == "derived" and text.startswith("linked for "), text
            page = (await asyncio.to_thread(player_page.status, DEVICE))["bus_link"]
            assert page["looked_at"] >= bus_link["looked_at"]
            assert (page["linked"], page["changed_at"]) == (True, bus_link["changed_at"])

            # 2. The Node bus crashes: not linked, with a later changed_at; it restarts: linked again.
            node.crash()
            _, down = await until(link_is(False, after=bus_link["changed_at"]), SETTLE_SECONDS,
                                  "Central reads the crashed Node not linked")
            read_at, _ = await read()
            assert _rendered(down, read_at)[1].startswith("not linked for ")
            node.start()
            await until(link_is(True, after=down["changed_at"]), SETTLE_SECONDS,
                        "Central reads the restarted Node linked")

            # 3. FleetHub stops: a read 31 s after its last look renders unknown.
            stop.set()
            await asyncio.wait_for(running, 30)
            _, last = await read()
            clock.at = last["looked_at"] + PRESENCE_STALE_SECONDS + 1
            read_at, stale = await read()
            assert read_at == clock.at and stale["looked_at"] == last["looked_at"]
            assert _rendered(stale, read_at) == ["unknown", "Unknown: Central has not looked at its hub for 31 s"]
            page = await asyncio.to_thread(player_page.status, DEVICE)
            assert (page["read_at"], page["bus_link"]) == (read_at, stale)
        finally:
            stop.set()
            await asyncio.wait_for(running, 30)

    try:
        asyncio.run(run())
    finally:
        node.stop()
        hub.stop()
