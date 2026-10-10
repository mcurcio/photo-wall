"""The worker keeps the hub beside its other loops (E3d S5; E3b design §7.2 rows "Hub runner and
Fleet generator", "NodeLink supervisors", §9.6; erratum E-E3D-CC1-1).

The worker's own composition, `build_node_bus(dsn, env)`, runs through `media.worker._run_workers`
beside an idle media loop and an idle job runtime. The hub is a real nats-server on the deployed
listeners (`HUB_LISTENERS`), started only once the worker has written its configuration file, as the
Compose hub waits for it. A Node on the shipped configuration links to it. The worker writes the
file, Central's links record the Node's birth, and WallWriter creates WALL. A link the hub drops is
back by itself. One SIGTERM ends all three loops normally and leaves no Central client on the hub.
Without PHOTO_WALL_HUB_URL there is no bus. Hub clients come and go asynchronously, so every /connz
check is polled, never a snapshot.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import urllib.request

import nats
from integration.bus_servers import BusServer, line_slices, node_server, until

from central.content_catalog.catalog import device_id_for_serial
from central.db import Database
from central.fleet.node_bus_accounts import FLEET_SYSTEM_USER
from central.fleet.node_bus_hub import HUB_LISTENERS
from central.node_bus_wiring import HUB_CONFIG_ENV, HUB_URL_ENV, build_node_bus
from contracts.node_link import WALL_STREAM, WALL_WRITER_USER, central_user
from nodeapi.node import NodeSession, Release

SERIAL = "serial-worker-hub"
RELEASE = Release("1.0.0", "sha256:display-release", {"display": 1})
HUB_URL = f"nats://127.0.0.1:{HUB_LISTENERS.client_port}"
MONITOR_URL = f"http://127.0.0.1:{HUB_LISTENERS.monitor_port}"


def _monitor(path: str) -> dict:
    with urllib.request.urlopen(f"{MONITOR_URL}{path}", timeout=2) as response:
        return json.load(response)


def _connections() -> dict[int, str]:
    """Connection id -> the user the hub's client is authorized as (/connz)."""
    return {connection["cid"]: connection.get("authorized_user")
            for connection in _monitor("/connz?auth=true&limit=1024").get("connections") or []}


def _users() -> set[str]:
    """Users the hub's clients are authorized as (/connz)."""
    return set(_connections().values())


async def _drop(user: str) -> set[int]:
    """Close every hub client authorized as `user` ($SYS.REQ.SERVER.<id>.KICK, as the hub drops a
    client); the ids of the connections closed."""
    cids = {cid for cid, name in (await asyncio.to_thread(_connections)).items() if name == user}
    server = (await asyncio.to_thread(_monitor, "/varz"))["server_id"]
    system = await nats.connect(servers=[HUB_URL], user=FLEET_SYSTEM_USER, password=FLEET_SYSTEM_USER,
                                allow_reconnect=False, connect_timeout=2)
    try:
        for cid in cids:
            await system.request(f"$SYS.REQ.SERVER.{server}.KICK", json.dumps({"cid": cid}).encode(), timeout=5)
    finally:
        await system.close()
    return cids


def _streams() -> set[str]:
    """Every stream the hub holds, in any account (/jsz)."""
    return {stream["name"] for account in _monitor("/jsz?accounts=true&streams=true").get("account_details") or []
            for stream in account.get("stream_detail") or []}


async def _born(db: Database) -> bool:
    def read() -> bool:
        with db.transaction() as conn:
            return conn.execute(
                "SELECT 1 FROM node_link_records WHERE device_id=%s AND stream='KV_state_display' "
                "AND subject='$KV.state_display.birth'", (device_id_for_serial(SERIAL),)).fetchone() is not None
    return await asyncio.to_thread(read)


class _IdleRuntime:
    """A job runtime with no jobs: runs until stopped."""

    def __init__(self) -> None:
        self._stop = asyncio.Event()

    async def run(self) -> None:
        await self._stop.wait()

    def stop(self) -> None:
        self._stop.set()


def test_the_worker_keeps_the_hub_and_records_a_node(database, tmp_path):
    from media.worker import _run_workers

    assert build_node_bus(database.dsn, {}) is None
    with database.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id, serial, first_seen, last_seen) VALUES (%s, %s, 1, 1)",
                     (device_id_for_serial(SERIAL), SERIAL))
    config_dir = tmp_path / "hub-config"
    config_dir.mkdir()
    config_path = config_dir / "hub.json"
    bus = build_node_bus(database.dsn, {HUB_URL_ENV: HUB_URL, HUB_CONFIG_ENV: str(config_path)})
    assert bus is not None
    hub = BusServer(name="hub", config=config_path, client_url=HUB_URL, environment={},
                    websocket_port=HUB_LISTENERS.websocket_port, monitor_url=MONITOR_URL,
                    listeners=HUB_LISTENERS)
    node = node_server(tmp_path, SERIAL, hub)
    session = NodeSession("display", line_slices()["display"], RELEASE, url=node.client_url)

    async def run() -> None:
        worker = asyncio.create_task(_run_workers(asyncio.Event().wait(), _IdleRuntime(), bus))
        try:
            async def written():
                return config_path.exists()
            await until(written, 10, "the worker writes the hub's configuration")
            hub.start()
            node.start()
            session.start()
            await until(lambda: _born(database), 30, "the Node's birth is recorded")

            async def walled():
                return WALL_STREAM in await asyncio.to_thread(_streams)
            await until(walled, 10, "WallWriter creates WALL")

            # Every Central client is on the hub. Polled, never one /connz snapshot: a link the hub
            # drops reconnects by itself, so a snapshot taken during a reconnect misses it.
            async def connected():
                return {FLEET_SYSTEM_USER, WALL_WRITER_USER, central_user(SERIAL)} <= await asyncio.to_thread(_users)
            await until(connected, 10, "Fleet, WallWriter and the Node's links are on the hub")

            # A link the hub drops is back on new connections, with no help from the test.
            dropped = await until(lambda: _drop(central_user(SERIAL)), 10, "the hub drops the Node's links")

            async def relinked():
                current = await asyncio.to_thread(_connections)
                back = {cid for cid, user in current.items() if user == central_user(SERIAL)} - dropped
                return len(back) >= len(dropped)   # both pipes' links
            await until(relinked, 10, "the Node's dropped links reconnect")
            assert not worker.done()
        finally:
            if not worker.done():   # a worker that ended has removed its handler: SIGTERM would kill pytest
                os.kill(os.getpid(), signal.SIGTERM)   # the worker's one handler stops all three loops
            await asyncio.wait_for(worker, 30)

        async def gone():   # the hub drops a closed client's connection on its own schedule: polled
            return not await asyncio.to_thread(_users) & {FLEET_SYSTEM_USER, WALL_WRITER_USER, central_user(SERIAL)}
        await until(gone, 10, "no Central client is left on the hub")

    try:
        asyncio.run(run())
    finally:
        session.stop()
        node.stop()
        hub.stop()
