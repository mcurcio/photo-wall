"""Galleria's two transports against a real nats-server ($PHOTO_WALL_NATS_SERVER, the repository's pinned
binary: `python3 scripts/nats_server.py fetch --dest DIR`), each on the configuration the repository
ships, never a copy: a Pi's bus on appliance/bus/node-bus.conf (its read-only door), and Central's hub
on the configuration Fleet generates (central/fleet/node_bus_accounts.py `hub_configuration`, pure),
with Home Assistant's door. These prove Galleria reads and writes as those doors allow, and nothing
else."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import nats
import pytest
from nats.errors import Error as NatsError

from central.fleet.node_bus_accounts import HubListeners, hub_configuration
from custom_components.galleria import contract
from custom_components.galleria.door import CentralDoor, DoorUnavailable
from custom_components.galleria.pi import PiReading, read_pi

SERVER = os.environ.get("PHOTO_WALL_NATS_SERVER")
pytestmark = pytest.mark.skipif(not SERVER, reason="PHOTO_WALL_NATS_SERVER names no nats-server binary")

SERIAL = "75628d0c"
NODE_BUS_CONF = Path(__file__).resolve().parents[1] / "appliance" / "bus" / "node-bus.conf"
STORE_BYTES = 64 * 1024   # every stream carries its own cap on both servers (max_bytes_required)


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


def _serve(path: Path, server: Server, environment: Mapping[str, str] | None = None) -> Iterator[Server]:
    process = subprocess.Popen([SERVER, "-c", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               env={**os.environ, **(environment or {})})
    door_port = server.door_port
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
        yield server
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def pi_bus() -> Iterator[Server]:
    """A Pi's bus on the shipped node-bus.conf, its per-Node values from the environment as the unit
    gives them (appliance/boot/bus_environment.py): local programs in account API over the loopback
    client port; the door's anonymous WebSocket clients land in `reader`. Its leaf dials nobody."""
    server = Server(_free_port(), _free_port())
    yield from _serve(NODE_BUS_CONF, server, {
        "PHOTO_WALL_BUS_NAME": json.dumps(contract.bus_server_name(SERIAL)),
        "PHOTO_WALL_BUS_PORT": str(server.client_port),
        "PHOTO_WALL_BUS_DOOR_PORT": str(server.door_port),
        "PHOTO_WALL_BUS_LEAF_URL": json.dumps(f"ws://nobody:nobody@127.0.0.1:{_free_port()}"),
    })


@pytest.fixture
def hub(tmp_path: Path) -> Iterator[Server]:
    """Central's hub on the configuration Fleet generates, with no Pi enrolled: Central's client on
    the client port, Home Assistant's door on the WebSocket listener."""
    server = Server(_free_port(), _free_port())
    path = tmp_path / "hub.json"
    path.write_text(hub_configuration([], HubListeners(
        server_name="hub", client_host="127.0.0.1", client_port=server.client_port, websocket_host="127.0.0.1",
        websocket_port=server.door_port, leaf_host="127.0.0.1", leaf_port=_free_port(), monitor_port=None,
        max_memory_store_bytes=4 * 1024 * 1024)))
    yield from _serve(path, server)


async def _client(url: str, user: str) -> nats.NATS:
    return await nats.connect(url, user=user, password=user, allow_reconnect=False)


async def _local(server: Server) -> nats.NATS:
    """A local program on a Pi's bus (no credentials: the loopback port's user)."""
    return await nats.connect(f"nats://127.0.0.1:{server.client_port}", allow_reconnect=False)


async def _bucket(client: nats.NATS, name: str) -> object:
    return await client.jetstream().create_key_value(bucket=name, history=1, storage="memory",
                                                     max_bytes=STORE_BYTES)


@pytest.fixture
async def written_pi(pi_bus: Server) -> AsyncIterator[Server]:
    """The Pi's host and Player lines as their writers put them."""
    local = await _local(pi_bus)
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
        # Its one request with a reply naming a stored key: the reply lands in the reader's own account.
        await reader.publish(f"{contract.NODE_DOOR_API_PREFIX}.STREAM.MSG.GET.{contract.HOST_STATE_STREAM}",
                             json.dumps({"last_by_subj": contract.HOST_STATE_PREFIX + contract.HOST_BASE_KEY}).encode(),
                             reply=contract.HOST_STATE_PREFIX + contract.HOST_BASE_KEY)
        await reader.flush()
    finally:
        await reader.close()
    reading = await read_pi(SERIAL, "127.0.0.1", port=written_pi.door_port)
    assert isinstance(reading, PiReading) and reading.base_tag == "v0.35.0"


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

            light = {"kind": "light", "name": "Test", "on": True, "brightness": 128,
                     "colour": {"kind": "kelvin", "kelvin": 2700.0}}
            await door.publish("light.test", light)
            entry = await states.get("light.test")
            assert contract.decode_home_assistant_state(entry.value) == light
            galleria = {"component": "galleria", "version": "1.2.3",
                        "kinds": ["light", "illuminance", "colour-temperature"]}
            await door.publish_birth(galleria)
            entry = await states.get(contract.GALLERIA_BIRTH_KEY)
            assert entry.value == contract.encode_galleria_birth(galleria)

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
    state = await _bucket(central, "state_central")
    birth = b'{"component":"central","version":"0.35.0"}'
    await state.put(contract.CENTRAL_BIRTH_KEY, birth)
    client = await nats.connect(f"ws://127.0.0.1:{hub.door_port}/{contract.DOOR_PATH}",
                                user=contract.HOME_ASSISTANT_USER, password=contract.HOME_ASSISTANT_USER,
                                allow_reconnect=False)
    target = contract.CENTRAL_STATE_PREFIX + contract.CENTRAL_BIRTH_KEY
    try:
        with pytest.raises((NatsError, asyncio.TimeoutError)):
            await client.jetstream(timeout=1).publish(target, b"{}")
        # Its allowed read with a reply naming Central's state: the reply lands in its own account.
        await client.publish(f"{contract.CENTRAL_DOOR_API_PREFIX}.STREAM.MSG.GET.{contract.CENTRAL_STATE_STREAM}",
                             json.dumps({"last_by_subj": target}).encode(), reply=target)
        await client.flush()
        assert (await client.jetstream(prefix=contract.CENTRAL_DOOR_API_PREFIX, timeout=1).get_last_msg(
            contract.CENTRAL_STATE_STREAM, target)).data == birth
    finally:
        await client.close()
        await central.close()
