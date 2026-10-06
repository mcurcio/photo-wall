"""Real nats-server processes for the Node bus seam probes (E3a): a hub and Nodes, with raw clients.

The hub runs Fleet's generated configuration and every Node runs the shipped
`appliance/bus/node-bus.conf`, unmodified, with its per-Node values in the environment. The
binary is the pinned one (`scripts/nats_server.py`), named by PHOTO_WALL_NATS_SERVER; without it
the bus tests skip with a reason CI's `node-bus` job owns. Every stream, service and subject a
probe uses is the test's own: nothing here is a subject grammar.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import nats
import pytest
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DeliverPolicy,
    DiscardPolicy,
    ExternalStream,
    Header,
    StorageType,
    StreamConfig,
    StreamSource,
)
from nats.js.errors import NotFoundError

from central.fleet.node_bus_accounts import FLEET_SYSTEM_USER, HubListeners, hub_configuration
from contracts.node_link import (
    NODE_DOMAIN,
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
    listeners: HubListeners | None = None  # the hub's generator input, kept for a reload
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
        monitor_url=f"http://127.0.0.1:{listeners.monitor_port}", listeners=listeners)


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


async def declare_wall_mirror(node_client: Client) -> bool:
    """Create-if-absent on the Node: a read-only local mirror of the hub's wall stream.

    A mirror that holds a sequence the hub's stream never reached is re-created: the hub lost its
    store and restarted its sequence at 1, and the mirror would wait for its old next sequence
    forever (§6 row 11's fallback, erratum E-W1-E3a-2-2). Returns True when it created a mirror."""
    jetstream = node_client.jetstream()
    try:
        held = (await jetstream.stream_info(WALL_STREAM)).state.last_seq
    except NotFoundError:
        held = None
    if held is not None:
        if await wall_origin_last(node_client) >= held:
            return False
        await jetstream.delete_stream(WALL_STREAM)
    await jetstream.add_stream(StreamConfig(
        name=WALL_STREAM, max_bytes=WALL_STREAM_BYTES, storage=StorageType.FILE,
        mirror=StreamSource(name=WALL_STREAM, external=ExternalStream(
            api=WALL_API_PREFIX, deliver=WALL_DELIVER_PREFIX))))
    return True


async def wall_origin_last(node_client: Client) -> int:
    """The hub wall stream's last sequence, read through the Node account's imported consumer API
    (the only WALL API a Node account has): an ephemeral consumer starting at the last message
    reports it, and is deleted at once."""
    wall_api = node_client.jetstream(prefix=WALL_API_PREFIX)
    info = await wall_api.add_consumer(WALL_STREAM, ConsumerConfig(
        deliver_policy=DeliverPolicy.LAST, ack_policy=AckPolicy.NONE, inactive_threshold=5))
    try:
        return info.delivered.stream_seq + info.num_pending
    finally:
        await wall_api.delete_consumer(WALL_STREAM, info.name)


async def wall_value(node_client: Client, subject: str) -> bytes | None:
    """The latest message for `subject` in the Node's local mirror, or None."""
    try:
        message = await node_client.jetstream().get_last_msg(WALL_STREAM, subject)
    except NotFoundError:
        return None
    return message.data


def node_kv_subject(bucket: str, key: str) -> str:
    """The only subject a KV write from Central lands on across the leaf (W9): the Node's domain
    API maps it to `$KV.<bucket>.<key>` on the Node; a plain `$KV.` subject is denied on the leaf."""
    return f"$JS.{NODE_DOMAIN}.API.$KV.{bucket}.{key}"


async def central_put(central_client: Client, bucket: str, key: str, value: bytes,
                      *, expected_revision: int | None) -> int:
    """Central writes a Node bucket's key, conditionally when `expected_revision` is set (API9).

    Returns the new revision; a stale revision raises nats.js.errors.APIError (err_code 10071)."""
    headers = None if expected_revision is None else {
        Header.EXPECTED_LAST_SUBJECT_SEQUENCE.value: str(expected_revision)}
    acknowledgement = await central_client.jetstream().publish(
        node_kv_subject(bucket, key), value, headers=headers)
    return acknowledgement.seq


@dataclass
class Recorder:
    """Central's commit, an append-only file: one row per committed stream sequence or counted gap."""
    path: Path

    def commit(self, seq: int, payload: bytes) -> None:
        self._append(f"seq {seq} {payload.hex()}")

    def gap(self, first: int, count: int) -> None:
        self._append(f"gap {first} {count}")

    def rows(self) -> list[tuple[str, int, int]]:
        """("seq", sequence, 0) and ("gap", first missing, count) rows, in commit order."""
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text().splitlines():
            kind, first, rest = line.split(" ")
            rows.append((kind, int(first), int(rest) if kind == "gap" else 0))
        return rows

    def sequences(self) -> list[int]:
        return [first for kind, first, _ in self.rows() if kind == "seq"]

    def gaps(self) -> list[tuple[int, int]]:
        return [(first, count) for kind, first, count in self.rows() if kind == "gap"]

    def _append(self, row: str) -> None:
        with self.path.open("a") as record:
            record.write(row + "\n")
            record.flush()
            os.fsync(record.fileno())


async def reload_hub(hub: BusServer, serials: Sequence[str]) -> None:
    """Rewrite the hub's configuration for `serials` in place and reload it through the system
    account (W7): a request as FLEET_SYSTEM_USER, answered once the server has applied it."""
    if hub.listeners is None:
        raise RuntimeError(f"{hub.name} is not a hub")
    hub.config.write_text(hub_configuration(serials, hub.listeners))
    fleet = await _connect(hub.client_url, user=FLEET_SYSTEM_USER, password=FLEET_SYSTEM_USER)
    try:
        identity = json.loads((await fleet.request("$SYS.REQ.SERVER.PING.IDZ", b"", timeout=2)).data)
        # nats-server 2.15.0 subscribes a reload-added account's service imports only on the next
        # reload (server/server.go:1413 skips them while reloading; the re-subscribe pass covers
        # accounts that already existed), so the new account's WALL consumer API answers "no
        # responders" until then. The second request applies them (erratum E-W1-E3a-2-1).
        for _ in range(2):
            reply = json.loads((await fleet.request(
                f"$SYS.REQ.SERVER.{identity['id']}.RELOAD", b"", timeout=5)).data)
            if reply.get("error"):
                raise RuntimeError(f"hub reload refused: {reply['error']}")
    finally:
        await fleet.close()


def kv_bucket_bytes(bucket: str, keys: Sequence[str], history: int, max_value: int,
                    header_bytes: int = 0) -> int:
    """A KV bucket's byte cap that its listed keys cannot fill: every key holding `history`
    values of `max_value` bytes, each charged nats-server's per-message file-store size
    (ns:server/filestore.go:10055-10062), plus one largest message of headroom. The class sizing
    E3b inherits.

    The headroom: a full discard-new bucket checks the new message's bytes before it drops the
    key's oldest value, and lets the put through only when that oldest value is no shorter than the
    new one (ns:server/filestore.go:5277-5279). Without it, a key whose oldest value is short is
    refused a full-length update in a bucket its listed keys fill (§6 row 12, E-W1-E3a-3-1)."""
    header = 4 + header_bytes if header_bytes else 0
    per_message = [34 + len(f"$KV.{bucket}.{key}") + max_value + header for key in keys]
    return sum(history * size for size in per_message) + max(per_message, default=0)


def leaf_connections(hub: BusServer) -> Mapping[str, int]:
    """Account -> the leaf connection's id (cid), from the hub's /leafz."""
    with urllib.request.urlopen(f"{hub.monitor_url}/leafz", timeout=2) as response:
        return {leaf["account"]: leaf["id"] for leaf in json.load(response).get("leafs") or []}
