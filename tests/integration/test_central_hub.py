"""Fleet keeps the hub matched to Central's enrolled set (E3d S2; E3b design §7.2 rows "Hub runner and
Fleet generator", "NodeLink supervisors", §9.6, §9.7, §11 rows "Hub restart or Central deploy", "Hub
starts with a stale or missing account file", "Hub reload misses new service imports", "A Node dials
before its account exists"; errata E-E3B-FR1, E-E3D-CUT-6, -7).

`FleetHub` runs with both `NodeLinks` over PostgreSQL and writes the hub's configuration into an
empty directory; the test starts a real nats-server from that file only once it exists, as the
Compose hub does. Each Node is a real bus on the shipped configuration with a `display` session that
reads WALL. 1: worker start: the file lists the enrolled Node, its leaf links, its birth is recorded,
WALL starts at mark 0 + 1 + K and the Node's wall view holds Central's `timing`. 2: a device enrolled
while running is in the file, linked, recorded and receives WALL (the second reload wires its
account's WALL consumer API, X6). 3: the hub is killed and starts empty from the same file: WALL is
re-created at the recorded mark + 1 + K with Central's current `timing` on both Nodes, and the drains
resume in the Nodes' unchanged epochs. 4: a retired device leaves the file and the hub (its leaf is closed:
a reload keeps it), its links stop, and its records stay.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from pathlib import Path

from integration.bus_servers import (
    BusServer,
    Projection,
    _free_port,
    leaf_connections,
    line_slices,
    node_server,
    until,
    wall_writer,
)

from central.content_catalog.catalog import device_id_for_serial
from central.db import Database
from central.fleet.node_bus_accounts import FLEET_SYSTEM_USER, HubListeners
from central.fleet.node_bus_hub import HUB_LOOK_SECONDS, FleetHub
from central.infra.node_link_store import PgLinkStores, PgWallMarks
from central.infra.node_links import NodeLinks
from contracts.node_link import WALL_STREAM, Pipe, account_id, central_user
from nodeapi.buffers import KeyTable
from nodeapi.hub import WALL_MARGIN, HubAdmin
from nodeapi.node import NodeSession, Release

FIRST, SECOND = "serial-hub-1", "serial-hub-2"
RELEASE = Release("1.0.0", "sha256:display-release", {"display": 1})
WALL_TABLE = KeyTable({"timing": 4096})
RECORDS = "RECORD_display"
STATE = "KV_state_display"
ENROL_SECONDS = 10
RESTART_SECONDS = 60   # a Node's WALL mirror resumes up to about a minute after a hub outage (E-E3B-FR1)


class _WallDocuments:
    """Central's wall documents (`central.infra.node_link_store.WallDocuments`), as E5/E8 will hold them."""

    def __init__(self, timing: bytes) -> None:
        self.timing = timing

    async def wall_documents(self) -> dict[str, bytes]:
        return {"timing": self.timing}


def _enrol(db: Database, serial: str) -> None:
    with db.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id, serial, first_seen, last_seen) VALUES (%s, %s, 1, 1)",
                     (device_id_for_serial(serial), serial))


async def _rows(db: Database, sql: str, params: tuple = ()) -> list[dict]:
    def run() -> list[dict]:
        with db.transaction() as conn:
            return conn.execute(sql, params).fetchall()
    return await asyncio.to_thread(run)


async def _records(db: Database, serial: str, stream: str) -> list[dict]:
    rows = await _rows(db, "SELECT epoch, seq, subject, data FROM node_link_records WHERE device_id=%s AND stream=%s "
                           "ORDER BY epoch, seq", (device_id_for_serial(serial), stream))
    return [{**row, "data": bytes(row["data"])} for row in rows]


async def _born(db: Database, serial: str) -> bool:
    return any(row["subject"] == "$KV.state_display.birth" for row in await _records(db, serial, STATE))


def _accounts(path: Path) -> set[str] | None:
    """The Node accounts the configuration file lists; None while there is no file."""
    if not path.exists():
        return None
    return {name for name in json.loads(path.read_text())["accounts"] if name.startswith("N")}


def _central_links(hub: BusServer, serial: str) -> int:
    """Central's clients connected in the Node's hub account, from the hub's /connz."""
    with urllib.request.urlopen(f"{hub.monitor_url}/connz?auth=1&limit=1024", timeout=2) as response:
        connections = json.load(response).get("connections") or []
    return sum(1 for connection in connections if connection.get("authorized_user") == central_user(serial))


async def _wall_first_seq(hub: BusServer) -> int | None:
    client = await wall_writer(hub)
    try:
        return (await client.jetstream().stream_info(WALL_STREAM)).config.first_seq
    except Exception:   # absent, or the hub not up yet
        return None
    finally:
        await client.close()


def test_fleet_keeps_the_hub_with_the_enrolled_set(database, tmp_path):
    _enrol(database, FIRST)
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
    nodes = {serial: node_server(tmp_path, serial, hub) for serial in (FIRST, SECOND)}
    display = line_slices()["display"]
    sessions = {serial: NodeSession("display", display, RELEASE, url=nodes[serial].client_url, reads_wall=True)
                for serial in (FIRST, SECOND)}
    documents = _WallDocuments(b"timing-1")
    stores = PgLinkStores(database)
    projection = Projection()
    links = [NodeLinks(pipe, hub.client_url, stores, projection) for pipe in (Pipe.FLEET, Pipe.SHOW)]
    fleet = FleetHub(database, hub.client_url, config_path, listeners, WALL_TABLE,
                     PgWallMarks(database, documents), links)

    def walled(serial: str, value: bytes):
        async def check():
            return sessions[serial].wall.get("timing") == value
        return check

    def linked(serial: str):
        async def check():
            return account_id(serial) in leaf_connections(hub)
        return check

    async def run() -> None:
        stop = asyncio.Event()
        running = [asyncio.create_task(fleet.run(stop)), *(asyncio.create_task(link.run(stop)) for link in links)]
        try:
            # 1. Worker start: the file appears for the enrolled Node; the hub starts from it.
            async def written():
                return _accounts(config_path) == {account_id(FIRST)}
            await until(written, ENROL_SECONDS, "Fleet writes the hub's configuration")
            assert config_path.stat().st_mode & 0o777 == 0o644
            hub.start()
            nodes[FIRST].start()
            sessions[FIRST].start()
            await until(linked(FIRST), ENROL_SECONDS, "the first Node's leaf links")
            await until(lambda: _born(database, FIRST), 30, "the first Node's birth is recorded")
            assert await _wall_first_seq(hub) == 1 + WALL_MARGIN
            await until(walled(FIRST, b"timing-1"), 30, "the first Node's wall view holds timing")
            assert fleet.wall is not None

            # 2. Enrol while running: the second Node is in the file, linked, recorded and walled. The
            # Node starts once the hub holds its account (Central's links are in): a leaf that dials
            # before then delays the Node's first WALL by about 40 s (nats-server; erratum E-E3D-S2-3).
            _enrol(database, SECOND)
            enrolled = time.monotonic()
            left = lambda: ENROL_SECONDS - (time.monotonic() - enrolled)  # noqa: E731

            async def listed():
                return _accounts(config_path) == {account_id(FIRST), account_id(SECOND)}
            await until(listed, left(), "Fleet adds the second Node's account")

            async def admitted():
                return _central_links(hub, SECOND) == 2
            await until(admitted, left(), "the hub admits Central's fleet and show links for the second Node")
            nodes[SECOND].start()
            sessions[SECOND].start()
            await until(linked(SECOND), left(), "the second Node's leaf links")
            await until(walled(SECOND, b"timing-1"), left(), "the second Node's wall view holds timing")
            await until(lambda: _born(database, SECOND), left(), "the second Node's birth is recorded")

            # 3. Hub restart, empty: WALL past the recorded mark, Central's current wall documents on
            # both Nodes, and the drains resume in the Nodes' unchanged epochs.
            [mark] = await _rows(database, "SELECT mark FROM node_bus_wall")
            assert mark["mark"] >= 1 + WALL_MARGIN
            documents.timing = b"timing-2"   # Central's projection changes while the hub is away
            hub.crash()
            hub.start()
            restarted = time.monotonic()
            left = lambda: RESTART_SECONDS - (time.monotonic() - restarted)  # noqa: E731

            async def recreated():
                return await _wall_first_seq(hub) == mark["mark"] + 1 + WALL_MARGIN
            await until(recreated, left(), "Fleet re-creates WALL at the recorded mark + 1 + K")
            for serial in (FIRST, SECOND):
                await until(walled(serial, b"timing-2"), left(), f"{serial}'s wall view holds the re-put timing")
            sessions[FIRST].events.emit("record.shown", b"after-restart", schema_major=1)

            async def recorded():
                return any(row["data"] == b"after-restart" for row in await _records(database, FIRST, RECORDS))
            await until(recorded, left(), "an event emitted after the restart is recorded")
            assert await _rows(database, "SELECT * FROM node_link_gaps WHERE lost IS NULL") == []

            # 4. Retire: the account leaves the file and the hub; the links stop; the records stay.
            held = await _records(database, SECOND, STATE)
            with database.transaction() as conn:
                conn.execute("UPDATE devices SET retired_at=2 WHERE device_id=%s", (device_id_for_serial(SECOND),))
            admin = await HubAdmin.connect(hub.client_url, FLEET_SYSTEM_USER)
            try:
                async def retired():
                    return (_accounts(config_path) == {account_id(FIRST)}
                            and account_id(SECOND) not in await admin.linked())
                await until(retired, ENROL_SECONDS, "Fleet removes the retired Node's account")
            finally:
                await admin.close()
            sessions[SECOND].events.emit("record.shown", b"after-retirement", schema_major=1)
            await asyncio.sleep(3 * HUB_LOOK_SECONDS)   # three looks and a drain's worth
            assert not any(row["data"] == b"after-retirement" for row in await _records(database, SECOND, RECORDS))
            assert await _records(database, SECOND, STATE) == held
            assert not any(task.done() for task in running)
        finally:
            stop.set()
            await asyncio.wait_for(asyncio.gather(*running), 30)

    try:
        asyncio.run(run())
    finally:
        for session in sessions.values():
            session.stop()
        for node in nodes.values():
            node.stop()
        hub.stop()
