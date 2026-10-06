"""Real nats-server processes for the Node bus seam probes (E3a): a hub and Nodes, with raw clients.

The hub runs Fleet's generated configuration and every Node runs the shipped
`appliance/bus/node-bus.conf`, unmodified, with its per-Node values in the environment. The
binary is the pinned one (`scripts/nats_server.py`), named by PHOTO_WALL_NATS_SERVER; without it
the bus tests skip with a reason CI's `node-bus` job owns. Every stream, service and subject a
probe uses is the test's own: nothing here is a subject grammar.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import nats
import pytest
from nats.js.api import DiscardPolicy, ExternalStream, StorageType, StreamConfig, StreamSource
from nats.js.errors import NotFoundError

from central.fleet.node_bus_accounts import HubListeners, hub_configuration
from contracts.node_link import (
    WALL_API_PREFIX,
    WALL_DELIVER_PREFIX,
    WALL_STREAM,
    WALL_STREAM_BYTES,
    WALL_WRITER_USER,
    account_id,
    central_user,
    node_user,
)

if TYPE_CHECKING:
    from nats.aio.client import Client

NODE_BUS_CONF = Path(__file__).resolve().parents[2] / "appliance" / "bus" / "node-bus.conf"
NATS_SERVER_VARIABLE = "PHOTO_WALL_NATS_SERVER"
HUB_STORE_BYTES = 4 * 1024 * 1024
START_SECONDS = 10.0


@dataclass
class BusServer:
    name: str
    config: Path
    store: Path
    client_url: str
    environment: Mapping[str, str]
    websocket_port: int | None = None    # the hub's leaf listener for Nodes
    monitor_url: str | None = None       # the hub's /leafz
    _process: subprocess.Popen | None = field(default=None, init=False, repr=False)

    def start(self) -> None:
        """Start the server and wait until its client port accepts."""
        if self._process is not None and self._process.poll() is None:
            raise RuntimeError(f"{self.name} is already running")
        log = self.config.with_suffix(".log").open("ab")
        self._process = subprocess.Popen(
            [str(nats_server_binary()), "-c", str(self.config)],
            env={**os.environ, **self.environment}, stdout=log, stderr=subprocess.STDOUT)
        log.close()
        host, port = self.client_url.removeprefix("nats://").rsplit(":", 1)
        deadline = time.monotonic() + START_SECONDS
        while True:
            if self._process.poll() is not None:
                raise RuntimeError(f"{self.name} exited {self._process.returncode}: {self.log_tail()}")
            try:
                socket.create_connection((host, int(port)), timeout=.2).close()
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{self.name} never accepted: {self.log_tail()}") from None
                time.sleep(.05)

    def stop(self) -> None:
        """SIGTERM and wait; the store stays."""
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=START_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def wipe(self) -> None:
        """Stop, then delete the store directory."""
        self.stop()
        shutil.rmtree(self.store, ignore_errors=True)

    def log_tail(self, lines: int = 20) -> str:
        path = self.config.with_suffix(".log")
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:]) if path.exists() else ""


def nats_server_binary() -> Path:
    value = os.environ.get(NATS_SERVER_VARIABLE)
    if not value:
        pytest.skip("set PHOTO_WALL_NATS_SERVER (checks.yml node-bus runs it)")
    return Path(value)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def hub_server(tmp: Path, serials: Sequence[str]) -> BusServer:
    """The hub on free loopback ports, configured by Fleet's generator for these serials."""
    nats_server_binary()
    directory = Path(tmp) / "hub"
    directory.mkdir(parents=True, exist_ok=True)
    listeners = HubListeners(
        server_name="hub", client_host="127.0.0.1", client_port=_free_port(),
        websocket_host="127.0.0.1", websocket_port=_free_port(),
        leaf_host="127.0.0.1", leaf_port=_free_port(), monitor_port=_free_port(),
        store_dir=str(directory / "store"), max_file_store_bytes=HUB_STORE_BYTES)
    config = directory / "hub.conf"
    config.write_text(hub_configuration(serials, listeners))
    return BusServer(
        name="hub", config=config, store=directory / "store",
        client_url=f"nats://127.0.0.1:{listeners.client_port}", environment={},
        websocket_port=listeners.websocket_port,
        monitor_url=f"http://127.0.0.1:{listeners.monitor_port}")


def node_server(tmp: Path, serial: str, hub: BusServer, *, prefix: str = "bus") -> BusServer:
    """A Node on the shipped configuration file, its leaf a ws:// URL with a path prefix."""
    nats_server_binary()
    directory = Path(tmp) / f"node-{account_id(serial)}"
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "node-bus.conf"
    shutil.copyfile(NODE_BUS_CONF, config)
    port = _free_port()
    user = node_user(serial)
    environment = {
        "PHOTO_WALL_BUS_NAME": f"node-{account_id(serial)}",
        "PHOTO_WALL_BUS_PORT": str(port),
        "PHOTO_WALL_BUS_STORE": str(directory / "store"),
        "PHOTO_WALL_BUS_LEAF_URL": f"ws://{user}:{user}@127.0.0.1:{hub.websocket_port}/{prefix}",
    }
    return BusServer(name=f"node {serial}", config=config, store=directory / "store",
                     client_url=f"nats://127.0.0.1:{port}", environment=environment)


async def _connect(url: str, **options) -> Client:
    return await nats.connect(servers=[url], allow_reconnect=False, connect_timeout=2, **options)


async def central(hub: BusServer, serial: str) -> Client:
    """Central's client in the Node's hub account."""
    user = central_user(serial)
    return await _connect(hub.client_url, user=user, password=user)


async def wall_writer(hub: BusServer) -> Client:
    """Central's client in the wall-wide account."""
    return await _connect(hub.client_url, user=WALL_WRITER_USER, password=WALL_WRITER_USER)


async def local(node: BusServer) -> Client:
    """A Node component's client: no credentials (no_auth_user)."""
    return await _connect(node.client_url)


async def declare_wall(writer: Client) -> None:
    """Create-if-absent: the hub's wall-wide stream, latest value per subject."""
    jetstream = writer.jetstream()
    try:
        await jetstream.stream_info(WALL_STREAM)
    except NotFoundError:
        await jetstream.add_stream(StreamConfig(
            name=WALL_STREAM, subjects=["wall.>"], max_msgs_per_subject=1, discard=DiscardPolicy.NEW,
            max_bytes=WALL_STREAM_BYTES, storage=StorageType.FILE))


async def declare_wall_mirror(node_client: Client) -> None:
    """Create-if-absent on the Node: a read-only local mirror of the hub's wall stream."""
    jetstream = node_client.jetstream()
    try:
        await jetstream.stream_info(WALL_STREAM)
    except NotFoundError:
        await jetstream.add_stream(StreamConfig(
            name=WALL_STREAM, max_bytes=WALL_STREAM_BYTES, storage=StorageType.FILE,
            mirror=StreamSource(name=WALL_STREAM, external=ExternalStream(
                api=WALL_API_PREFIX, deliver=WALL_DELIVER_PREFIX))))


async def wall_value(node_client: Client, subject: str) -> bytes | None:
    """The latest message for `subject` in the Node's local mirror, or None."""
    try:
        message = await node_client.jetstream().get_last_msg(WALL_STREAM, subject)
    except NotFoundError:
        return None
    return message.data
