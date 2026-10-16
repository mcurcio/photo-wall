"""The worker keeps the hub beside its other loops (E3d S5; E3b design §7.2 rows "Hub runner and
Fleet generator", "NodeLink supervisors", §9.6; erratum E-E3D-CC1-1).

The worker's own composition, `build_node_bus(dsn, env)`, runs through `media.worker._run_workers`
beside an idle media loop and an idle job runtime. The hub is a real nats-server on the deployed
listeners (`HUB_LISTENERS`), started only once the worker has written its configuration file, as the
Compose hub waits for it. A Node on the shipped configuration links to it. The worker writes the
file, Central's links record the Node's birth, and WallWriter creates WALL. A link the hub drops is
back by itself. Central on its hub (C2): Home Assistant reads `installation` at the hub's WebSocket
listener, and a binding made in the registry reaches it within a look (CENTRAL_STATE_LOOK_SECONDS);
each Player's drawn report on its own line is House > Lights' (C4). A hub restart (empty) brings
every key of Central's state back and a new states epoch. One SIGTERM ends all three loops normally
and leaves no Central client on the hub.
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
from central.frame_lighting.paper_profiles import LUT_RECIPE_ID
from central.infra.central_state import CENTRAL_STATE_LOOK_SECONDS
from central.infra.frame_lighting_store import PgFrameLighting, PgHouseLock
from central.infra.room_light_store import PgRoomLights
from central.node_bus_wiring import HUB_CONFIG_ENV, HUB_URL_ENV, build_node_bus
from central.registry import Registry
from contracts.central_api import (
    CENTRAL_STATE_TABLE,
    INSTALLATION_KEY,
    STATES_EPOCH_KEY,
    InstallRefusal,
    InstallRefused,
    decode_central_state,
)
from contracts.node_link import STORE_LINES, WALL_STREAM, WALL_WRITER_USER, central_user
from contracts.node_state import FRAME_REPORTS_BYTES, FRAMES_KEY, PLAYER_LINE, ROOM_LIGHTS_DRAWN_KEY
from contracts.room_light import ROOM_LIGHTS_DRAWN_BYTES, RoomLightsDrawn, room_lights_digest
from contracts.time import ManualClock
from contracts.wire_codec import encode
from nodeapi.buffers import KeyTable, Slice, event_buffer, state_bucket
from nodeapi.node import NodeSession, Release

SERIAL = "serial-worker-hub"
RELEASE = Release("1.0.0", "sha256:display-release", {"display": 1})
HUB_URL = f"nats://127.0.0.1:{HUB_LISTENERS.client_port}"
DOOR_URL = f"ws://127.0.0.1:{HUB_LISTENERS.websocket_port}"   # the hub's client WebSocket
# The Player's line as a test writes it (0017 C4: the Player declares it): its two state keys.
PLAYER_TABLE = KeyTable({ROOM_LIGHTS_DRAWN_KEY: ROOM_LIGHTS_DRAWN_BYTES, FRAMES_KEY: FRAME_REPORTS_BYTES})
PLAYER_RELEASE = Release("1.0.0", "sha256:player-release", {"player": 1})


def player_slice() -> Slice:
    state = state_bucket(PLAYER_LINE, PLAYER_TABLE)
    return Slice(PLAYER_LINE, (event_buffer(PLAYER_LINE, "record", STORE_LINES[PLAYER_LINE].max_bytes - state.max_bytes),
                               state))
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


def test_the_worker_keeps_the_hub_and_records_a_node(database, tmp_path, monkeypatch):
    from integration.room_light_rig import HomeAssistant
    from test_registry import enroll, frame

    from media.worker import _run_workers
    monkeypatch.setenv("PHOTO_WALL_CACHE_ROOT", str(tmp_path / "worker-cache"))

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
    player_line = NodeSession(PLAYER_LINE, player_slice(), PLAYER_RELEASE, url=node.client_url)
    registry = Registry(database, ManualClock(1000), lighting=PgFrameLighting(LUT_RECIPE_ID))
    player = enroll(registry, device_id=device_id_for_serial(SERIAL))[0]
    frame(registry, "hall")
    lights = PgRoomLights(database, PgHouseLock())

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

            # C4: the Player's drawn report on its own line is House > Lights'.
            player_line.start()
            digest = room_lights_digest(await asyncio.to_thread(lights.current))
            player_line.state.put(ROOM_LIGHTS_DRAWN_KEY, encode(
                RoomLightsDrawn(drawn=digest, following=True).model_dump(mode="json"), ROOM_LIGHTS_DRAWN_BYTES, "x"))

            async def drawn():
                players = (await asyncio.to_thread(lights.drawn)).players
                return [(each.player_id, each.state) for each in players] == [(player["player_id"], "drawn")]
            await until(drawn, 10, "the Player's drawn report reaches House > Lights")

            # C2: Central's state on its hub follows the registry within a look.
            async with HomeAssistant(DOOR_URL) as house:
                async def installation():
                    raw = await house.central(INSTALLATION_KEY)
                    return None if raw is None else decode_central_state(INSTALLATION_KEY, raw)
                listed = await until(installation, 10, "Central lists the installation on its hub")
                assert [(each.id, each.driven_by) for each in listed.frames] == [("hall", None)]
                assert [(each.serial, each.address) for each in listed.players] == [(SERIAL, None)]
                bound = await asyncio.to_thread(registry.bind, "hall", player["player_id"], "HDMI-A-1",
                                                expected_generation=0)

                async def driven():
                    [hall] = (await installation()).frames
                    return hall.driven_by is not None and (hall.driven_by.serial, hall.driven_by.output_id,
                                                           hall.driven_by.generation) == (SERIAL, "HDMI-A-1",
                                                                                          bound["generation"])
                await until(driven, CENTRAL_STATE_LOOK_SECONDS + 1, "the binding reaches Central's state")
                assert await house.install("serial-unknown", "v1.0.0") == InstallRefused(
                    refusal=InstallRefusal.UNKNOWN_PLAYER)
                assert await house.install(SERIAL, "v9.9.9") == InstallRefused(refusal=InstallRefusal.UNKNOWN_VERSION)
                epoch = await house.central(STATES_EPOCH_KEY)

            # A hub restart starts empty: every key comes back, in a new states epoch.
            hub.stop()
            hub.start()
            async with HomeAssistant(DOOR_URL) as house:
                async def every_key():
                    held = {key: await house.central(key) for key in CENTRAL_STATE_TABLE}
                    return None if None in held.values() else held
                held = await until(every_key, 30, "Central puts every key again after the hub restart")
                assert held[STATES_EPOCH_KEY] != epoch
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
        player_line.stop()
        session.stop()
        node.stop()
        hub.stop()
