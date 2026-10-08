"""A Node's leaf reaches the hub through Central's origin (E3d S3; E3b design §3, §9.6 step 1, §11 rows
"Wi-Fi, ingress or WAN stall", "A Node dials before its account exists"; W11; erratum E-E3D-CUT-4).

Real Central (`central.app.create_app`) under uvicorn on a loopback port, a real hub on Fleet's
generated configuration, a real Node bus whose leaf dials Central's port at LEAF_PATH, and Central's
fleet NodeLinks over PostgreSQL. 1: the leaf links through Central, the Node's birth is recorded (Node
to hub) and a WALL write reaches the Node's wall view (hub to Node). 2: Central stops and starts again
on the same port; the leaf relinks and a later event is recorded. 3: a Central with no hub refuses a
WebSocket at the leaf's route.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
import uvicorn
from integration.bus_servers import (
    Projection,
    _free_port,
    hub_server,
    leaf_connections,
    line_slices,
    node_server,
    until,
)
from test_registry import ADMIN
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.db import Database
from central.fleet.leaf_bridge import LEAF_ROUTE
from central.infra.node_link_store import PgLinkStores
from central.infra.node_links import NodeLinks
from contracts.node_link import LEAF_PATH, Pipe, account_id
from nodeapi.buffers import KeyTable
from nodeapi.hub import WallWriter
from nodeapi.node import NodeSession, Release

SERIAL = "serial-leaf-bridge"
RELEASE = Release("1.0.0", "sha256:display-release", {"display": 1})
WALL_TABLE = KeyTable({"timing": 4096})
RECORDS = "RECORD_display"
STATE = "KV_state_display"
LINK_SECONDS = 10
RELINK_SECONDS = 15
SECONDS = 30


class _Central:
    """Central's HTTP process: a fresh app from `create_app` under uvicorn on one loopback port, in its
    own thread and event loop. `stop` and `start` again is a restart on the same port."""

    def __init__(self, database: Database, port: int, hub_leaf_url: str | None) -> None:
        self.database, self.port, self.hub_leaf_url = database, port, hub_leaf_url
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        app = create_app(self.database, admin_token=ADMIN, run_scheduler=False, hub_leaf_url=self.hub_leaf_url)
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + SECONDS
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError(f"Central did not start on port {self.port}")
            time.sleep(.05)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.should_exit = True
        self._thread.join(SECONDS)
        assert not self._thread.is_alive(), "Central did not stop"
        self._server = self._thread = None


class _WallMarks:
    """Central's WALL marks and wall documents (`nodeapi.hub.WallMarks`): mark 0 and one `timing`."""

    async def mark(self) -> int:
        return 0

    async def record_mark(self, seq: int) -> None:
        pass

    async def wall_documents(self) -> dict[str, bytes]:
        return {"timing": b"timing-1"}


def _enrol(db: Database, serial: str) -> None:
    with db.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id, serial, first_seen, last_seen) VALUES (%s, %s, 1, 1)",
                     (device_id_for_serial(serial), serial))


async def _records(db: Database, stream: str) -> list[dict]:
    def run() -> list[dict]:
        with db.transaction() as conn:
            return conn.execute("SELECT subject, data FROM node_link_records WHERE device_id=%s AND stream=%s "
                                "ORDER BY epoch, seq", (device_id_for_serial(SERIAL), stream)).fetchall()
    return [{**row, "data": bytes(row["data"])} for row in await asyncio.to_thread(run)]


def test_a_node_leaf_links_through_centrals_origin(database, tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_WALL_CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.delenv("PHOTO_WALL_HUB_LEAF_URL", raising=False)
    _enrol(database, SERIAL)
    hub = hub_server(tmp_path, [SERIAL])
    central = _Central(database, _free_port(), f"ws://127.0.0.1:{hub.websocket_port}/leafnode")
    node = node_server(tmp_path, SERIAL, hub, leaf_port=central.port, prefix=LEAF_PATH.strip("/"))
    session = NodeSession("display", line_slices()["display"], RELEASE, url=node.client_url, reads_wall=True)
    links = NodeLinks(Pipe.FLEET, hub.client_url, PgLinkStores(database), Projection())
    unbridged = _Central(database, _free_port(), None)

    async def linked() -> int | None:
        return leaf_connections(hub).get(account_id(SERIAL))

    def recorded(data: bytes):
        async def check() -> bool:
            return any(row["data"] == data for row in await _records(database, RECORDS))
        return check

    async def run() -> None:
        stop = asyncio.Event()
        supervising = asyncio.create_task(links.run(stop))
        wall: WallWriter | None = None
        try:
            await links.track([SERIAL])
            # 1. The leaf links through Central's origin; both directions carry the bus.
            first = await until(linked, LINK_SECONDS, "the Node's leaf links through Central")

            async def born() -> bool:
                return any(row["subject"] == "$KV.state_display.birth" for row in await _records(database, STATE))
            await until(born, SECONDS, "the Node's birth is recorded (Node to hub)")
            wall = await WallWriter.connect(hub.client_url, WALL_TABLE, _WallMarks())
            await wall.ensure()

            async def walled() -> bool:
                return session.wall.get("timing") == b"timing-1"
            await until(walled, SECONDS, "a WALL write reaches the Node's wall view (hub to Node)")

            # 2. Central restarts on the same port: the leaf relinks, and the bus is recorded again.
            central.stop()
            central.start()

            async def relinked() -> bool:
                return (await linked()) not in (None, first)
            await until(relinked, RELINK_SECONDS, "the Node's leaf relinks through the restarted Central")
            session.events.emit("record.shown", b"after-restart", schema_major=1)
            await until(recorded(b"after-restart"), SECONDS, "an event emitted after the restart is recorded")

            # 3. A Central with no hub has no leaf route.
            unbridged.start()
            with pytest.raises(InvalidStatus):
                async with connect(f"ws://127.0.0.1:{unbridged.port}{LEAF_ROUTE}", proxy=None):
                    pass
            assert not supervising.done()
        finally:
            if wall is not None:
                await wall.close()
            stop.set()
            await asyncio.wait_for(supervising, SECONDS)

    hub.start()
    try:
        central.start()
        node.start()
        session.start()
        asyncio.run(run())
    finally:
        session.stop()
        node.stop()
        unbridged.stop()
        central.stop()
        hub.stop()
