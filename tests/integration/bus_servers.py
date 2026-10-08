"""Real nats-server processes for the Node bus seam probes (E3a): a hub and Nodes, with raw clients.

The hub runs Fleet's generated configuration and every Node runs the shipped
`appliance/bus/node-bus.conf`, unmodified, with its per-Node values in the environment. The
binary is the pinned one (`scripts/nats_server.py`), named by PHOTO_WALL_NATS_SERVER; without it
the bus tests skip with a reason CI's `node-bus` job owns. Every stream, service and subject a
probe uses is the test's own: nothing here is a subject grammar.

Every stream, KV bucket and mirror a probe declares is built by the shipped `nodeapi.buffers`
(erratum E-W1-TD-S1): circular event buffers drop their oldest when full, sticky documents are never
full (E-W1-TD-4). The config test fails on any other `nodeapi` module that configures one.

A server counts as started only on its own word: `BusServer.start` waits for the ports file that
pid writes and refuses unless it lists exactly the ports the harness gave it, so a server that lost
a port to another process exits with its log in the error instead of passing as up. `_free_port`
never issues a port twice in a process, and each pytest-xdist worker draws from its own slice of a
range below every kernel's ephemeral floor, so two of a test's servers cannot be handed one port.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import signal
import socket
import ssl
import subprocess
import time
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import nats
import pytest
from nats.js.errors import NotFoundError

from appliance.boot.bus_environment import bus_environment
from central.fleet.node_bus_accounts import FLEET_SYSTEM_USER, HubListeners, hub_configuration
from contracts.node_link import (
    CENTRAL_INBOX_PREFIX,
    STORE_LINES,
    WALL_STREAM,
    WALL_WRITER_USER,
    central_user,
    node_user,
)
from nodeapi.buffers import (
    KeyTable,
    Slice,
    declare,
    desired_bucket,
    event_buffer,
    state_bucket,
    wall_config,
    wall_mirror_config,
)
from nodeapi.envelope import message_id
from nodeapi.epoch import Token
from nodeapi.pull import Batch, Gap, Read

if TYPE_CHECKING:
    from nats.aio.client import Client
    from nats.js.api import StreamConfig
    from nats.js.client import JetStreamContext
    from nats.js.kv import KeyValue

NODE_BUS_CONF = Path(__file__).resolve().parents[2] / "appliance" / "bus" / "node-bus.conf"
NATS_SERVER_VARIABLE = "PHOTO_WALL_NATS_SERVER"
HUB_STORE_BYTES = 4 * 1024 * 1024
START_SECONDS = 10.0
MIB = 1024 * 1024
# The loopback ports the harness hands out: below Linux's (32768) and macOS's (49152) ephemeral floor,
# so no kernel-chosen port (a client socket's local end) lands in it; pytest-xdist workers split it.
PORT_FLOOR = 20000
PORT_CEILING = 32000


async def declare_bucket(jetstream: JetStreamContext, config: StreamConfig) -> KeyValue:
    """Declare a bucket configuration (`nodeapi.buffers.state_bucket` or `desired_bucket`) and bind a
    KeyValue handle to it."""
    await declare(jetstream, config)
    return await jetstream.key_value(config.name.removeprefix("KV_"))


def desired_documents(component: str) -> KeyTable:
    """The harness's desired-bucket document table for one component: a show document, a layout and
    twenty Frame documents, two values each, inside 0.25 MiB. Retention is per topic in each
    release's slice, never a document (owner answer Q2)."""
    sizes = {"show": 4096, "layout": 4096, **{f"frame{index:02}": 4096 for index in range(20)}}
    return KeyTable(sizes, history=2)


# The harness's state table for every line: eight keys of reported state, two values each.
STATE_TABLE = KeyTable({f"state{index}": 4096 for index in range(8)}, history=2)
# The harness's wall table: the wall keys its probes write through a writer.
WALL_TABLE = KeyTable({"timing": 4096, "scene": 4096})
_OBSERVATION_AGE = 6 * 3600.0


def line_slices() -> dict[str, Slice]:
    """One slice per store line (`contracts.node_link.STORE_LINES`) at its full bytes and streams: a
    state bucket by STATE_TABLE, a desired bucket by `desired_documents` where the line has a third
    stream, and the line's events (observations for health and content, kept six hours; records for
    the rest) taking every byte the keyed buckets leave. With the WALL mirror, the store at its 17
    streams and 11.5 of 12 MiB (E3b design §7.3)."""
    slices = {}
    for name, line in STORE_LINES.items():
        keyed = [state_bucket(name, STATE_TABLE)]
        if line.streams == 3:
            keyed.append(desired_bucket(name, desired_documents(name)))
        rest = line.max_bytes - sum(config.max_bytes for config in keyed)
        events = (event_buffer(name, "observation", rest, max_age=_OBSERVATION_AGE) if name in ("health", "content")
                  else event_buffer(name, "record", rest))
        slices[name] = Slice(name, (events, *keyed))
    return slices


@dataclass
class BusServer:
    """One nats-server process. Its JetStream store is memory, so every `start` is empty."""
    name: str
    config: Path
    client_url: str
    environment: Mapping[str, str]
    websocket_port: int | None = None    # the hub's leaf listener for Nodes
    monitor_url: str | None = None       # the hub's /leafz
    listeners: HubListeners | None = None  # the hub's generator input, kept for a reload
    _process: subprocess.Popen | None = field(default=None, init=False, repr=False)

    def start(self) -> None:
        """Start the server and wait until it has written its ports file listing exactly the ports
        this server was given, then until its client port accepts. A server that exits first (a
        port another process holds is fatal to nats-server) raises with its log tail; so does one
        whose file lists other ports, or none within START_SECONDS, after it is stopped."""
        if self._process is not None and self._process.poll() is None:
            raise RuntimeError(f"{self.name} is already running")
        ports_dir = self.config.parent / "ports"
        shutil.rmtree(ports_dir, ignore_errors=True)
        ports_dir.mkdir(parents=True)
        log = self.config.with_suffix(".log").open("ab")
        self._process = subprocess.Popen(
            [str(nats_server_binary()), "-c", str(self.config), "--ports_file_dir", str(ports_dir)],
            # Its own TMPDIR: with no store_dir, nats-server 2.15.0 still makes
            # $TMPDIR/nats/jetstream/<account>/streams at start, and servers sharing one race on it
            # (fatal at start; erratum E-E3B-S3-1).
            env={**os.environ, "TMPDIR": str(self.config.parent), **self.environment}, stdout=log,
            stderr=subprocess.STDOUT)
        log.close()
        _RUNNING[id(self)] = self
        try:
            self._await_own_ports(ports_dir / f"nats-server_{self._process.pid}.ports")
        except BaseException:
            self.stop()
            raise

    def expected_ports(self) -> dict[str, set[int]]:
        """The ports file this server must write: its client port, and the hub's WebSocket and
        monitor ports. nats-server 2.15 lists no leafnodes port there; the hub's leaf listener is
        bound before the file is written and failing to bind it is fatal, so the file's presence
        covers it."""
        expected = {"nats": {_port_of(self.client_url)}}
        if self.websocket_port is not None:
            expected["websocket"] = {self.websocket_port}
        if self.monitor_url is not None:
            expected["monitoring"] = {_port_of(self.monitor_url)}
        return expected

    def _await_own_ports(self, path: Path) -> None:
        deadline = time.monotonic() + START_SECONDS
        while not path.exists():
            self._raise_if_exited()
            if time.monotonic() > deadline:
                raise RuntimeError(f"{self.name} wrote no ports file in {START_SECONDS}s: {self.log_tail()}")
            time.sleep(.05)
        while True:   # the server writes the file in one call; a read can still see it half written
            try:
                listed = json.loads(path.read_text())
                break
            except json.JSONDecodeError:
                self._raise_if_exited()
                if time.monotonic() > deadline:
                    raise
                time.sleep(.05)
        actual = {kind: {_port_of(url) for url in urls} for kind, urls in listed.items() if urls}
        if actual != self.expected_ports():
            raise RuntimeError(f"{self.name} listens on {actual}, was given {self.expected_ports()}: "
                               f"{self.log_tail()}")
        while True:
            self._raise_if_exited()
            try:
                socket.create_connection(("127.0.0.1", _port_of(self.client_url)), timeout=.2).close()
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{self.name} never accepted: {self.log_tail()}") from None
                time.sleep(.05)

    def _raise_if_exited(self) -> None:
        if self._process is not None and self._process.poll() is not None:
            raise RuntimeError(f"{self.name} exited {self._process.returncode}: {self.log_tail()}")

    def state(self) -> str:
        if self._process is None:
            return "stopped"
        code = self._process.poll()
        return "running" if code is None else f"exited {code}"

    def stop(self) -> None:
        """SIGTERM and wait (a paused server is resumed first); the memory store goes with it."""
        self._end(signal.SIGTERM)

    def crash(self) -> None:
        """SIGKILL and wait, as a crash or an OOM kill: the server stops nothing cleanly."""
        self._end(signal.SIGKILL)

    def _end(self, number: int) -> None:
        _RUNNING.pop(id(self), None)
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.send_signal(signal.SIGCONT)
        process.send_signal(number)
        try:
            process.wait(timeout=START_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def pause(self) -> None:
        """SIGSTOP: the server keeps its sockets and answers nothing, as a hub whose host or uplink
        hangs does, until `resume`."""
        self._signal(signal.SIGSTOP)

    def resume(self) -> None:
        self._signal(signal.SIGCONT)

    def rss_bytes(self) -> int:
        """The server process's resident memory (`ps -o rss=`)."""
        if self._process is None:
            raise RuntimeError(f"{self.name} is not running")
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(self._process.pid)], check=True,
                             capture_output=True, text=True).stdout
        return int(out.strip()) * 1024

    def _signal(self, number: int) -> None:
        if self._process is None or self._process.poll() is not None:
            raise RuntimeError(f"{self.name} is not running")
        self._process.send_signal(number)

    def log_tail(self, lines: int = 20) -> str:
        path = self.config.with_suffix(".log")
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:]) if path.exists() else ""


# Every server started and not yet stopped, for `until`'s timeout message.
_RUNNING: dict[int, BusServer] = {}


async def until(check: Callable[[], Awaitable], seconds: float, what: str):
    """Poll `check` until it returns a truthy value and return it. Past `seconds` it fails with
    every started server's state and log tail, so a server's own error shows beside the timeout."""
    deadline = time.monotonic() + seconds
    while True:
        value = await check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"not within {seconds}s: {what}{servers_report()}")
        await asyncio.sleep(.05)


def servers_report(lines: int = 8) -> str:
    """Each started, unstopped server's state and last log lines."""
    return "".join(f"\n--- {server.name} ({server.state()}):\n{server.log_tail(lines)}"
                   for server in _RUNNING.values())


def nats_server_binary() -> Path:
    value = os.environ.get(NATS_SERVER_VARIABLE)
    if not value:
        pytest.skip("set PHOTO_WALL_NATS_SERVER (checks.yml node-bus runs it)")
    return Path(value)


def _port_of(url: str) -> int:
    return int(url.rsplit(":", 1)[1])


_ISSUED: set[int] = set()


def _port_slice() -> range:
    """This process's share of [PORT_FLOOR, PORT_CEILING): one slice per pytest-xdist worker, so
    workers never draw the same port; the whole range outside xdist."""
    count = max(int(os.environ.get("PYTEST_XDIST_WORKER_COUNT", "1")), 1)
    index = int(os.environ.get("PYTEST_XDIST_WORKER", "gw0").removeprefix("gw"))
    width = (PORT_CEILING - PORT_FLOOR) // count
    return range(PORT_FLOOR + index * width, PORT_FLOOR + (index + 1) * width)


def _draw() -> int:
    """A port in this process's slice that nothing holds right now. May repeat an earlier draw."""
    ports = _port_slice()
    for _ in range(len(ports)):
        port = random.choice(ports)
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError(f"no free loopback port in {ports}")


def _free_port() -> int:
    """A free loopback port this process has never issued: a repeated draw is drawn again."""
    for _ in range(len(_port_slice())):
        port = _draw()
        if port not in _ISSUED:
            _ISSUED.add(port)
            return port
    raise RuntimeError(f"every port in {_port_slice()} was already issued")


def hub_server(tmp: Path, serials: Sequence[str]) -> BusServer:
    """The hub on free loopback ports, configured by Fleet's generator for these serials."""
    nats_server_binary()
    directory = Path(tmp) / "hub"
    directory.mkdir(parents=True, exist_ok=True)
    listeners = HubListeners(
        server_name="hub", client_host="127.0.0.1", client_port=_free_port(),
        websocket_host="127.0.0.1", websocket_port=_free_port(),
        leaf_host="127.0.0.1", leaf_port=_free_port(), monitor_port=_free_port(),
        max_memory_store_bytes=HUB_STORE_BYTES)
    config = directory / "hub.conf"
    config.write_text(hub_configuration(serials, listeners))
    return BusServer(
        name="hub", config=config,
        client_url=f"nats://127.0.0.1:{listeners.client_port}", environment={},
        websocket_port=listeners.websocket_port,
        monitor_url=f"http://127.0.0.1:{listeners.monitor_port}", listeners=listeners)


def node_server(tmp: Path, serial: str, hub: BusServer, *, leaf_port: int | None = None,
                scheme: str = "http", host: str = "127.0.0.1",
                extra_environment: Mapping[str, str] | None = None) -> BusServer:
    """A Node on the shipped configuration file whose environment is the one its handoff stage
    writes, `bus_environment(f"{scheme}://{host}:{port}", serial)` with `port` the hub's WebSocket
    port or `leaf_port` (a proxy in front of it), so every harness Node exercises the real
    derivation: its leaf at LEAF_PATH, ws:// for http and wss:// for https. Only
    PHOTO_WALL_BUS_PORT is replaced, by a free port (NODE_BUS_PORT cannot be shared by servers
    side by side); `extra_environment` adds to it (SSL_CERT_FILE, say)."""
    nats_server_binary()
    directory = Path(tmp) / node_user(serial)
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "node-bus.conf"
    shutil.copyfile(NODE_BUS_CONF, config)
    port = _free_port()
    environment = {**bus_environment(f"{scheme}://{host}:{leaf_port or hub.websocket_port}", serial),
                   "PHOTO_WALL_BUS_PORT": str(port), **(extra_environment or {})}
    return BusServer(name=f"node {serial}", config=config,
                     client_url=f"nats://127.0.0.1:{port}", environment=environment)


async def _connect(url: str, **options) -> Client:
    return await nats.connect(servers=[url], allow_reconnect=False, connect_timeout=2, **options)


async def central(hub: BusServer, serial: str, **options) -> Client:
    """Central's client in the Node's hub account, its inboxes under CENTRAL_INBOX_PREFIX (the only
    ones the hub lets it subscribe to, E-W1-LEAF-1)."""
    user = central_user(serial)
    return await _connect(hub.client_url, user=user, password=user, inbox_prefix=CENTRAL_INBOX_PREFIX,
                          **options)


async def wall_writer(hub: BusServer) -> Client:
    """Central's client in the wall-wide account."""
    return await _connect(hub.client_url, user=WALL_WRITER_USER, password=WALL_WRITER_USER)


async def local(node: BusServer, **options) -> Client:
    """A Node component's client: no credentials (no_auth_user)."""
    return await _connect(node.client_url, **options)


async def declare_wall(writer: Client, table: KeyTable = WALL_TABLE, *, first_seq: int = 1) -> None:
    """Create-if-absent: the hub's wall-wide stream (`wall_config`) with `table` in its metadata.
    A WALL re-created after the hub lost its store is `nodeapi.hub.WallWriter`'s (tracer step 4)."""
    await declare(writer.jetstream(), wall_config(table, first_seq=first_seq))


async def declare_wall_mirror(node_client: Client) -> bool:
    """Create-if-absent on the Node: the local mirror of the hub's wall stream (`wall_mirror_config`).
    Local only: it never asks the hub, so it succeeds while the hub is away, in any order with
    Central's WALL declare (erratum E-W1-E3a-R-4). Returns True when it created one."""
    return await declare(node_client.jetstream(), wall_mirror_config())


async def wall_value(node_client: Client, subject: str) -> bytes | None:
    """The latest message for `subject` in the Node's local mirror, or None."""
    try:
        message = await node_client.jetstream().get_last_msg(WALL_STREAM, subject)
    except NotFoundError:
        return None
    return message.data


class LinkStoreCrash(Exception):
    """Central's process dies mid-transaction: the transaction never happened."""


class FileLinkStore:
    """Central's link store (`nodeapi.hub.LinkStore`) and WALL marks (`nodeapi.hub.WallMarks`) as an
    append-only file of JSON rows: one commit is one write of its gap rows, raw records and cursor. A
    record already held is not written again (the idempotent repeat) but is kept in `repeats`, so a
    test sees a reader that repeated itself. `crash_next` names the next transaction ("commit",
    "wrote" or "record_mark") to raise before it writes anything, as a Central that dies between a
    Node's or the hub's acknowledgement and its own record. `hold_wall` keeps one of Central's wall
    documents, as its projection would."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.crash_next: str | None = None
        self.repeats: list[tuple[str, str, int]] = []

    def _crash(self, kind: str, what: str) -> None:
        if self.crash_next == kind:
            self.crash_next = None
            raise LinkStoreCrash(what)

    async def cursors(self) -> Mapping[str, Token]:
        cursors: dict[str, Token | None] = {}
        for row in self.rows("cursor"):
            cursors[row["stream"]] = None if row["epoch"] is None else Token(row["epoch"], row["seq"])
        return {stream: cursor for stream, cursor in cursors.items() if cursor is not None}

    async def commit(self, stream: str, batch: Batch) -> None:
        self._crash("commit", stream)
        held = {(row["stream"], row["epoch"], row["seq"]) for row in self.rows("record")}
        rows: list[dict] = []
        for item in batch.items:
            if isinstance(item, Gap):
                rows.append({"kind": "gap", "stream": stream, "epoch": item.epoch, "after": item.after,
                             "count": item.count})
                continue
            assert isinstance(item, Read)
            key = (stream, item.token.epoch, item.token.seq)
            if key in held:
                self.repeats.append(key)
                continue
            held.add(key)
            rows.append({"kind": "record", "stream": stream, "epoch": item.token.epoch, "seq": item.token.seq,
                         "subject": item.subject, "message_id": message_id(item.headers),
                         "data": item.data.decode("latin-1")})
        cursor = batch.cursor
        rows.append({"kind": "cursor", "stream": stream, "epoch": cursor and cursor.epoch,
                     "seq": cursor and cursor.seq})
        self._append(rows)

    async def action(self, kind: str, body: Mapping[str, object]) -> None:
        self._append([{"kind": "action", "action": kind, "body": body}])

    async def own_tokens(self, stream: str) -> Mapping[str, tuple[str, Token]]:
        return {row["key"]: (row["digest"], Token(row["epoch"], row["seq"])) for row in self.rows("own")
                if row["stream"] == stream}

    async def wrote(self, stream: str, key: str, digest: str, token: Token) -> None:
        self._crash("wrote", f"{stream} {key}")
        self._append([{"kind": "own", "stream": stream, "key": key, "digest": digest, "epoch": token.epoch,
                       "seq": token.seq}])

    async def mark(self) -> int:
        return max((row["seq"] for row in self.rows("mark")), default=0)

    async def record_mark(self, seq: int) -> None:
        self._crash("record_mark", str(seq))
        self._append([{"kind": "mark", "seq": seq}])

    def hold_wall(self, key: str, value: bytes) -> None:
        self._append([{"kind": "wall", "key": key, "value": value.decode("latin-1")}])

    async def wall_documents(self) -> Mapping[str, bytes]:
        return {row["key"]: row["value"].encode("latin-1") for row in self.rows("wall")}

    def rows(self, kind: str) -> list[dict]:
        if not self.path.exists():
            return []
        return [row for row in map(json.loads, self.path.read_text().splitlines()) if row["kind"] == kind]

    def records(self, stream: str) -> list[dict]:
        """The stream's raw records in commit order, each `data` as bytes."""
        return [{**row, "data": row["data"].encode("latin-1")} for row in self.rows("record")
                if row["stream"] == stream]

    def actions(self, kind: str) -> list[Mapping[str, object]]:
        return [row["body"] for row in self.rows("action") if row["action"] == kind]

    def _append(self, rows: list[dict]) -> None:
        with self.path.open("a") as store:
            store.write("".join(json.dumps(row) + "\n" for row in rows))
            store.flush()
            os.fsync(store.fileno())


class Projection:
    """Central's document projection (`nodeapi.hub.DocumentSource`): stream -> key -> value."""

    def __init__(self) -> None:
        self.streams: dict[str, dict[str, bytes]] = {}

    async def documents(self, stream: str) -> Mapping[str, bytes]:
        return dict(self.streams.get(stream, {}))


async def reload_hub(hub: BusServer, serials: Sequence[str], *, requests: int = 2) -> None:
    """Rewrite the hub's configuration for `serials` in place and reload it through the system
    account (W7): `requests` requests as FLEET_SYSTEM_USER, each answered once the server applied it.
    Two by default: the upstream bug below; `test_one_reload_wires_a_new_accounts_service_imports`
    (xfail, strict) goes red the day one is enough."""
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
        for _ in range(requests):
            reply = json.loads((await fleet.request(
                f"$SYS.REQ.SERVER.{identity['id']}.RELOAD", b"", timeout=5)).data)
            if reply.get("error"):
                raise RuntimeError(f"hub reload refused: {reply['error']}")
    finally:
        await fleet.close()


def leaf_connections(hub: BusServer) -> Mapping[str, int]:
    """Account -> the leaf connection's id (cid), from the hub's /leafz."""
    with urllib.request.urlopen(f"{hub.monitor_url}/leafz", timeout=2) as response:
        return {leaf["account"]: leaf["id"] for leaf in json.load(response).get("leafs") or []}


class PrefixProxy:
    """A reverse proxy in front of the hub's WebSocket listener, as the origin's ingress route is
    (W11, E3d/E4): it forwards a connection whose HTTP request path starts with `/<prefix>/`
    unchanged, upgrade and all, and answers 404 to any other. Plain asyncio: one task per direction.
    With `tls` it terminates TLS on its listener before it reads the request path, as an https
    ingress does; upstream stays plain. `drop` discards every byte in both directions for a while and `sever` cuts every link through it,
    as a Wi-Fi stall and a dropped uplink do."""

    def __init__(self, upstream_port: int, prefix: str, *, tls: ssl.SSLContext | None = None) -> None:
        self.upstream_port = upstream_port
        self.tls = tls
        self.prefix = "/" + prefix.strip("/") + "/"
        self.port = _free_port()
        self.paths: list[str] = []
        self._server: asyncio.base_events.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._dropping_until = 0.0

    async def drop(self, seconds: float) -> None:
        """Discard every byte either way for `seconds`; returns when the window ends."""
        self._dropping_until = time.monotonic() + seconds
        await asyncio.sleep(seconds)

    def sever(self) -> None:
        """Cut every connection through the proxy; a client dials again on its own."""
        for writer in list(self._writers):
            writer.transport.abort()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port, ssl=self.tls)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return
        path = head.split(b"\r\n", 1)[0].split(b" ")[1].decode()
        self.paths.append(path)
        if not path.startswith(self.prefix):
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", self.upstream_port)
        upstream_writer.write(head)
        self._writers |= {writer, upstream_writer}
        try:
            await asyncio.gather(self._pipe(reader, upstream_writer), self._pipe(upstream_reader, writer))
        finally:
            self._writers -= {writer, upstream_writer}

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                if not (data := await reader.read(65536)):
                    break
                if time.monotonic() < self._dropping_until:
                    continue
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
