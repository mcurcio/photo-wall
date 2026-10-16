"""Galleria's two transports against a real nats-server ($PHOTO_WALL_NATS_SERVER, the repository's pinned
binary: `python3 scripts/nats_server.py fetch --dest DIR`), each started with an inline configuration
equal to its door's permissions: a Pi's read-only door (contracts/node_link.py NODE_READER_*,
DOOR_CONNECTIONS) and Home Assistant's door on Central's hub (contracts/central_api.py
HOME_ASSISTANT_PUBLISH/SUBSCRIBE). The repository's own config tests bind those lists to the shipped
configurations; these prove Galleria reads and writes as those permissions allow."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import nats
import pytest
from nats.errors import Error as NatsError

from custom_components.galleria import contract
from custom_components.galleria.door import CentralDoor, DoorUnavailable
from custom_components.galleria.pi import PiReading, read_pi

SERVER = os.environ.get("PHOTO_WALL_NATS_SERVER")
pytestmark = pytest.mark.skipif(not SERVER, reason="PHOTO_WALL_NATS_SERVER names no nats-server binary")

SERIAL = "75628d0c"
LOCAL = "local"


@pytest.fixture(autouse=True)
def _sockets(socket_enabled: None) -> Iterator[None]:
    """Home Assistant's harness blocks sockets for every test; these speak to a local nats-server."""
    yield


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@dataclass(frozen=True)
class Server:
    client_port: int
    door_port: int


def _serve(config: dict, directory: Path) -> Iterator[Server]:
    client_port, door_port = _free_port(), _free_port()
    config = {**config, "host": "127.0.0.1", "port": client_port,
              "websocket": {**config.get("websocket", {}), "host": "127.0.0.1", "port": door_port, "no_tls": True}}
    path = directory / "server.conf"
    path.write_text(json.dumps(config))
    process = subprocess.Popen([SERVER, "-c", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                socket.create_connection(("127.0.0.1", door_port), timeout=0.2).close()
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"nats-server did not start: {process.stderr.read().decode()}") from None
                time.sleep(0.02)
        yield Server(client_port, door_port)
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def pi_bus(tmp_path: Path) -> Iterator[Server]:
    """A Pi's bus: local programs in account API over the client port; the door's anonymous WebSocket
    clients land in `reader`, which may only ask for one stored message at a time."""
    yield from _serve({
        "server_name": contract.bus_server_name(SERIAL),
        "jetstream": {"store_dir": str(tmp_path / "js"), "max_memory_store": 12 * 1024 * 1024},
        "accounts": {"API": {"jetstream": "enabled", "users": [
            {"user": LOCAL, "password": LOCAL, "allowed_connection_types": ["STANDARD"]},
            {"user": "reader", "password": "", "allowed_connection_types": ["WEBSOCKET"],
             "permissions": {"publish": {"allow": ["$JS.API.STREAM.MSG.GET.*"]},
                             "subscribe": {"allow": ["_INBOX.>"]}}}]}},
        "websocket": {"no_auth_user": "reader"},
    }, tmp_path)


@pytest.fixture
def hub(tmp_path: Path) -> Iterator[Server]:
    """Central's hub: Central's client on the client port; Home Assistant only over WebSocket, allowed
    its states, Central's state one key at a time, and Central's methods."""
    yield from _serve({
        "server_name": "hub",
        "jetstream": {"store_dir": str(tmp_path / "js"), "max_memory_store": 12 * 1024 * 1024},
        "accounts": {contract.CENTRAL_ACCOUNT: {"jetstream": "enabled", "users": [
            {"user": "central-api", "password": "central-api", "allowed_connection_types": ["STANDARD"]},
            {"user": contract.HOME_ASSISTANT_USER, "password": contract.HOME_ASSISTANT_USER,
             "allowed_connection_types": ["WEBSOCKET"],
             "permissions": {"publish": {"allow": [contract.HOME_ASSISTANT_STATES_PREFIX + ">",
                                                   f"$JS.API.STREAM.MSG.GET.{contract.CENTRAL_STATE_STREAM}",
                                                   "central.method.>"]},
                             "subscribe": {"allow": ["_INBOX.>"]}}}]}},
    }, tmp_path)


async def _client(url: str, user: str) -> nats.NATS:
    return await nats.connect(url, user=user, password=user, allow_reconnect=False)


async def _bucket(client: nats.NATS, name: str) -> object:
    return await client.jetstream().create_key_value(bucket=name, history=1, storage="memory")


@pytest.fixture
async def written_pi(pi_bus: Server) -> AsyncIterator[Server]:
    """The Pi's host and Player lines as their writers put them."""
    local = await _client(f"nats://127.0.0.1:{pi_bus.client_port}", LOCAL)
    host, player = await _bucket(local, "state_host"), await _bucket(local, "state_player")
    await host.put(contract.HOST_BASE_KEY, b'{"base_tag":"v0.35.0"}')
    await host.put(contract.LIGHT_SENSOR_KEY, b'{"reading":{"kind":"measured","lux":120.0}}')
    await player.put(contract.BIRTH_KEY, json.dumps({
        "component": "player", "version": "1.0.0", "pipe": "show", "release_digest": "sha256:ab",
        "schema_majors": {"player": contract.PLAYER_SCHEMA_MAJOR}, "slice_digest": "0" * 64}).encode())
    await player.put(contract.FRAMES_KEY, b'{"frames":[{"frame_id":"frame-1","generation":3,'
                                          b'"output_id":"HDMI-A-1","showing":true,"light_lux":41.0}]}')
    await local.close()
    yield pi_bus


async def test_a_pi_reads_through_its_door(written_pi: Server) -> None:
    reading = await read_pi(SERIAL, "127.0.0.1", port=written_pi.door_port)
    assert reading == PiReading(
        base_tag="v0.35.0", light_meter={"reading": {"kind": "measured", "lux": 120.0}},
        player_birth=reading.player_birth if isinstance(reading, PiReading) else None,
        frames=({"frame_id": "frame-1", "generation": 3, "output_id": "HDMI-A-1", "showing": True,
                 "light_lux": 41.0},))
    assert reading.player_birth["schema_majors"] == {"player": contract.PLAYER_SCHEMA_MAJOR}


async def test_a_door_that_answers_another_server_name_reads_unreachable(written_pi: Server) -> None:
    assert await read_pi("another-pi", "127.0.0.1", port=written_pi.door_port) == "pi_unreachable"


async def test_a_door_nobody_answers_reads_unreachable() -> None:
    assert await read_pi(SERIAL, "127.0.0.1", port=_free_port()) == "pi_unreachable"


async def test_central_s_door_that_nobody_answers_is_unavailable_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # nats-py retries a first connect forever when it may reconnect forever; the door bounds it
    monkeypatch.setattr("custom_components.galleria.door.CENTRAL_TIMEOUT", 0.25)
    with pytest.raises(DoorUnavailable):
        await CentralDoor.connect(f"http://127.0.0.1:{_free_port()}")


async def test_a_fresh_bus_reads_not_yet_written_and_no_meter(pi_bus: Server) -> None:
    reading = await read_pi(SERIAL, "127.0.0.1", port=pi_bus.door_port)
    assert reading == PiReading(base_tag="not_yet_written", light_meter=None, player_birth="not_yet_written",
                                frames="not_yet_written")


async def test_the_pi_door_refuses_a_write(written_pi: Server) -> None:
    reader = await nats.connect(f"ws://127.0.0.1:{written_pi.door_port}", allow_reconnect=False)
    try:
        with pytest.raises((NatsError, asyncio.TimeoutError)):
            await reader.jetstream(timeout=1).publish(contract.HOST_STATE_PREFIX + contract.HOST_BASE_KEY, b"{}")
    finally:
        await reader.close()


async def test_central_s_door_reads_publishes_and_installs(hub: Server) -> None:
    central = await _client(f"nats://127.0.0.1:{hub.client_port}", "central-api")
    try:
        door = await CentralDoor.connect(f"http://127.0.0.1:{hub.door_port}")
        try:
            assert door.connected
            with pytest.raises(DoorUnavailable):   # no states bucket yet
                await door.publish("light.test", {"kind": "illuminance", "name": "Lux", "lux": 1.0})
            assert await door.read(contract.CENTRAL_BIRTH_KEY) is None   # no state bucket yet

            state = await _bucket(central, "state_central")
            states = await _bucket(central, "home_assistant")
            birth = b'{"component":"central","version":"0.35.0"}'
            await state.put(contract.CENTRAL_BIRTH_KEY, birth)
            assert await door.read(contract.CENTRAL_BIRTH_KEY) == birth
            assert await door.read(contract.RELEASES_KEY) is None

            light = {"kind": "light", "name": "Test", "on": True, "brightness": 128, "kelvin": 2700, "rgb": None}
            await door.publish("light.test", light)
            entry = await states.get("light.test")
            assert contract.decode_home_assistant_state(entry.value) == light

            async def answer(message: nats.aio.msg.Msg) -> None:
                request = json.loads(message.data)
                await message.respond(json.dumps({"answer": {"kind": "refused", "refusal": "unknown_version"}}
                                                 if request["version"] != "v0.35.0" else
                                                 {"answer": {"kind": "started", "serial": request["serial"],
                                                             "version": request["version"],
                                                             "install": "downloading"}}).encode())

            await central.subscribe(contract.INSTALL_PLAYER_SUBJECT, cb=answer)
            await central.flush()
            assert await door.install_player(SERIAL, "v0.35.0") == {
                "kind": "started", "serial": SERIAL, "version": "v0.35.0", "install": "downloading"}
            assert await door.install_player(SERIAL, "v9.9.9") == {"kind": "refused", "refusal": "unknown_version"}
        finally:
            await door.close()
    finally:
        await central.close()


async def test_central_s_door_refuses_home_assistant_writing_central_s_state(hub: Server) -> None:
    central = await _client(f"nats://127.0.0.1:{hub.client_port}", "central-api")
    await _bucket(central, "state_central")
    client = await nats.connect(f"ws://127.0.0.1:{hub.door_port}/{contract.DOOR_PATH}",
                                user=contract.HOME_ASSISTANT_USER, password=contract.HOME_ASSISTANT_USER,
                                allow_reconnect=False)
    try:
        with pytest.raises((NatsError, asyncio.TimeoutError)):
            await client.jetstream(timeout=1).publish(contract.CENTRAL_STATE_PREFIX + contract.CENTRAL_BIRTH_KEY, b"{}")
    finally:
        await client.close()
        await central.close()
