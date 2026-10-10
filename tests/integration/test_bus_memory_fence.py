"""The Node bus inside its memory fence (owner answer Q1 = start clean, E3b design §6).

Every Node buffer is a memory stream, so the store is heap charged to the bus's cgroup with the rest
of the server. This runs the pinned linux-arm64 nats-server in a container fenced as the bus unit is
(`--memory` = `--memory-swap` = NODE_BUS_MEMORY_MAX, GOMEMLIMIT = NODE_BUS_GOMEMLIMIT) with the shipped
`node-bus.conf` and every store line `nodeapi` applies at its full bytes and streams (E3b design
§7.3), its wall copy the real mirror of a hub's WALL.
It gives every stream the most consumers the server admits (the next is refused, erratum
E-W1-CONS-2), fills every buffer and keeps writing flat out for minutes, and fails on any OOM kill,
server restart or refused write. Every event it writes carries the headers `nodeapi` sends, from the
smallest event (empty payload) to 4000 B.

The fit `contracts.node_link` checks at import rests on one measured number, the heap per stored byte
at the smallest event `nodeapi` stores (MEMORY_STORE_FACTOR; E3b design §7.2, erratum E-E3B-R2-5). A
second run fills the whole store with those events, every stream at its consumer cap, and measures
the factor here, on the Pi's architecture. Nothing survives a bus start, so there is no reload to
prove (E3b design §12); a third run kills the server with SIGKILL inside the fence and finds it back
at once with an empty store that takes every line again (§11 "Bus crash, OOM or restart").

The shipped file binds the client port to loopback; `-a 0.0.0.0` (a command-line flag, which
nats-server applies over the file) lets Docker publish it. Nothing else differs from the file.

A second run hangs the hub (SIGSTOP) while a Node program floods a local stream, churns
subscriptions, with Central's pull waiting: the leaf has no pending limit toward the hub, so anything
that crossed would queue in the bus until its fence OOM-killed it. The leaf's subject contract keeps
it all on the Node (erratum E-W1-LEAF-1); once the hub resumes, Central's pull, conditional write and
the wall mirror all work. A raw client publishing on the leaf's exports is out of scope (R16).

Needs Docker and PHOTO_WALL_BUS_FENCE_SERVER, a linux-arm64 nats-server (`scripts/nats_server.py
fetch` on an arm64 Linux host); checks.yml's `bus-fence` job runs it on ubuntu-24.04-arm.
PHOTO_WALL_BUS_FENCE_MINUTES sets the write phase (default 5), PHOTO_WALL_BUS_FENCE_STALL_SECONDS
the hub's stall (default 60).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import nats
import pytest
from integration.bus_servers import (
    HUB_STORE_BYTES,
    NODE_BUS_CONF,
    WALL_TABLE,
    line_slices,
)
from nats.js.api import AckPolicy, ConsumerConfig
from nats.js.errors import APIError

from appliance.boot.bus_environment import bus_environment
from central.fleet.node_bus_accounts import HubListeners, hub_configuration
from contracts.node_link import (
    CENTRAL_INBOX_PREFIX,
    CENTRAL_WRITER,
    MAX_STORED_MESSAGE,
    MEMORY_STORE_FACTOR,
    NODE_BUS_GOMEMLIMIT,
    NODE_BUS_HEADROOM,
    NODE_BUS_MEMORY_MAX,
    NODE_BUS_PORT,
    NODE_DOMAIN,
    NODE_MAX_CONSUMERS,
    WALL_STREAM,
    WALL_WRITER_USER,
    central_user,
)
from nodeapi.buffers import (
    CIRCULAR,
    KeyTable,
    apply,
    buffer_kind,
    declare,
    desired_bucket,
    event_buffer,
    table_of,
    wall_config,
    wall_mirror_config,
)
from nodeapi.documents import ABSENT, Conflict, DocumentWriter
from nodeapi.envelope import event_headers
from nodeapi.epoch import epoch_of
from nodeapi.pull import pull
from scripts.nats_server import NATS_SERVER_VERSION

SERVER_VARIABLE = "PHOTO_WALL_BUS_FENCE_SERVER"
MINUTES_VARIABLE = "PHOTO_WALL_BUS_FENCE_MINUTES"
STALL_VARIABLE = "PHOTO_WALL_BUS_FENCE_STALL_SECONDS"
IMAGE = "alpine:3.23.4@sha256:5b10f432ef3da1b8d4c7eb6c487f2f5a8f096bc91145e68878dd4a5019afde11"
MIB = 1024 * 1024
SERIAL = "fence-node"
SERVER = "/opt/nats/nats-server"
CONF = "/etc/photo-wall/node-bus.conf"
MAXIMUM_CONSUMERS = 10026   # JSMaximumConsumersLimitErr: a stream's max_consumers reached
SAMPLE_SECONDS = 2.0
SCHEMA_MAJOR = 1           # the schema major an event carries here: one digit, as every first major
FILL_BATCH = 500           # smallest events in flight at once while a circular buffer fills
SETTLE_SECONDS = 5.0       # after the fill, before the heap is sampled
RESTART_SECONDS = 15.0     # a killed bus is back, empty, its lines applied and its mirror synced
# One line per server start, so a restart (an OOM kill or a crash) is counted, not missed.
SUPERVISOR = (f"while true; do echo start >> /tmp/starts; {SERVER} -c {CONF} -a 0.0.0.0; "
              "echo \"exited $?\" >> /tmp/exits; sleep 1; done")


def _docker(*arguments: str) -> str:
    done = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, f"docker {' '.join(arguments)}: {done.stderr.strip()}"
    return done.stdout.strip()


def _published(container: str, port: int) -> int:
    return int(_docker("port", container, f"{port}/tcp").splitlines()[0].rsplit(":", 1)[1])


def _accepts(port: int, seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=.5).close()
            return
        except OSError:
            assert time.monotonic() < deadline, f"{what} never accepted on {port}"
            time.sleep(.2)


class _Bus:
    """The fenced bus container: its cgroup is the container's own (private cgroup namespace)."""

    def __init__(self, name: str) -> None:
        self.name = name

    def sh(self, command: str) -> str:
        return _docker("exec", self.name, "sh", "-c", command)

    def sample(self) -> dict[str, float]:
        lines = self.sh("cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.peak; "
                        "grep '^oom_kill ' /sys/fs/cgroup/memory.events; wc -l < /tmp/starts; "
                        "grep '^anon ' /sys/fs/cgroup/memory.stat").split("\n")
        return {"current_mib": int(lines[0]) / MIB, "peak_mib": int(lines[1]) / MIB,
                "oom_kill": int(lines[2].split()[1]), "starts": int(lines[3]),
                "anon_mib": int(lines[4].split()[1]) / MIB}

    def logs(self) -> str:
        done = subprocess.run(["docker", "logs", "--tail", "60", self.name], capture_output=True, text=True)
        return (done.stdout + done.stderr)[-6000:]


def _smallest_event(config) -> str:
    """The subject of the smallest event `nodeapi` stores in a circular buffer: `<line>.<topic>.e`. Sent
    with exactly the headers `nodeapi` sends with an event (`event_headers`) and an empty payload, so no
    stored `nodeapi` event is smaller."""
    return config.subjects[0].replace(">", "e")


def _writes(config) -> tuple[list[str], list[bytes], bool]:
    """What one writer cycles through for one Node buffer: subjects and bodies inside its table (every
    key of a state or desired bucket at its largest), or events from the smallest to 4000 B, each with
    an event's headers (the flag)."""
    table = table_of(config)
    if table is not None:
        keys = sorted(table.sizes)
        prefix = config.subjects[0].removesuffix(">")
        return [prefix + key for key in keys], [b"k" * table.sizes[key] for key in keys], False
    return [_smallest_event(config)], [b"", b"r" * 200, b"r" * 4000], True


async def _writer(jetstream, name: str, subjects: list[str], bodies: list[bytes], event: bool,
                  stop: asyncio.Event, counts: Counter, errors: Counter) -> None:
    index = 0
    while not stop.is_set():
        try:
            await jetstream.publish(subjects[index % len(subjects)], bodies[index % len(bodies)],
                                    headers=event_headers(SCHEMA_MAJOR) if event else None)
            counts[name] += 1
        except Exception as error:   # every failure is the finding: nothing may refuse a write
            errors[f"{name}: {type(error).__name__}: {error}"[:160]] += 1
            await asyncio.sleep(.5)
        index += 1


async def _states(jetstream, names) -> dict[str, tuple[int, int, int, str]]:
    states = {}
    for name in names:
        info = await jetstream.stream_info(name)
        states[name] = (info.state.messages, info.state.first_seq, info.state.last_seq, epoch_of(info))
    return states


def _fence_server() -> Path:
    value = os.environ.get(SERVER_VARIABLE)
    if not value:
        pytest.skip(f"set {SERVER_VARIABLE} (checks.yml bus-fence runs it)")
    return Path(value).resolve()


@contextmanager
def _fenced_bus(tmp_path: Path) -> Iterator[tuple[_Bus, str, int, int]]:
    """A hub on Fleet's configuration and the fenced bus leafed to it, each in its own container on one
    network: (the bus, the hub's container, the hub's and the bus's published client ports)."""
    binary = _fence_server()
    run = uuid.uuid4().hex[:8]
    network, hub_name, bus = f"fence-{run}", f"fence-hub-{run}", _Bus(f"fence-bus-{run}")
    hub_conf = tmp_path / "hub.conf"
    hub_conf.write_text(hub_configuration([SERIAL], HubListeners(
        server_name="hub", client_host="0.0.0.0", client_port=4222, websocket_host="0.0.0.0",
        websocket_port=8080, leaf_host="127.0.0.1", leaf_port=7422, monitor_port=None,
        max_memory_store_bytes=HUB_STORE_BYTES)))
    hub_conf.chmod(0o644)
    # The environment the handoff stage writes for a Node whose boot origin is the hub's listener.
    environment = bus_environment(f"http://{hub_name}:8080", SERIAL)
    platform = ("--platform", "linux/arm64")
    try:
        _docker("network", "create", network)
        version = _docker("run", "--rm", *platform, "-v", f"{binary}:{SERVER}:ro", IMAGE, SERVER, "--version")
        assert version == f"nats-server: v{NATS_SERVER_VERSION}", version
        _docker("run", "-d", "--name", hub_name, "--network", network, *platform, "-p", "127.0.0.1::4222",
                "-v", f"{binary}:{SERVER}:ro", "-v", f"{hub_conf}:/etc/photo-wall/hub.conf:ro",
                IMAGE, SERVER, "-c", "/etc/photo-wall/hub.conf")
        # The bus unit's fence: MemoryMax, no swap, and its environment file (GOMEMLIMIT with it);
        # the store is the server's own heap.
        _docker("run", "-d", "--name", bus.name, "--network", network, *platform, "--cgroupns=private",
                f"--memory={NODE_BUS_MEMORY_MAX // MIB}m", f"--memory-swap={NODE_BUS_MEMORY_MAX // MIB}m",
                "-p", f"127.0.0.1::{NODE_BUS_PORT}",
                *(item for key in sorted(environment) for item in ("-e", f"{key}={environment[key]}")),
                "-v", f"{binary}:{SERVER}:ro", "-v", f"{NODE_BUS_CONF}:{CONF}:ro",
                IMAGE, "sh", "-c", SUPERVISOR)
        hub_port, bus_port = _published(hub_name, 4222), _published(bus.name, NODE_BUS_PORT)
        _accepts(hub_port, 30, "the hub")
        _accepts(bus_port, 30, "the bus")
        yield bus, hub_name, hub_port, bus_port
    finally:
        subprocess.run(["docker", "kill", "--signal=CONT", hub_name], capture_output=True)
        subprocess.run(["docker", "rm", "-f", bus.name, hub_name], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


def test_a_full_store_keeps_writing_inside_the_bus_fence(tmp_path):
    minutes = float(os.environ.get(MINUTES_VARIABLE, "5"))
    with _fenced_bus(tmp_path) as (bus, _, hub_port, bus_port):
        asyncio.run(_exercise(bus, hub_port, bus_port, minutes))


def test_a_stalled_hub_never_pushes_the_bus_past_its_fence(tmp_path):
    seconds = float(os.environ.get(STALL_VARIABLE, "60"))
    with _fenced_bus(tmp_path) as (bus, hub_name, hub_port, bus_port):
        asyncio.run(_stall(bus, hub_name, hub_port, bus_port, seconds))


def test_a_store_of_the_smallest_events_fits_the_measured_heap_factor(tmp_path):
    with _fenced_bus(tmp_path) as (bus, _, hub_port, bus_port):
        asyncio.run(_measure(bus, hub_port, bus_port))


def test_a_hard_kill_restarts_the_fenced_bus_empty(tmp_path):
    with _fenced_bus(tmp_path) as (bus, _, hub_port, bus_port):
        asyncio.run(_kill(bus, hub_port, bus_port))


def _buffers() -> dict:
    """Every Node buffer: each store line's at its full bytes and streams, and the WALL mirror."""
    buffers = {config.name: config for slice_ in line_slices().values() for config in slice_.buffers}
    buffers[WALL_STREAM] = wall_mirror_config()
    return buffers


async def _node_client(bus_port: int):
    return await nats.connect(f"nats://127.0.0.1:{bus_port}", allow_reconnect=False, connect_timeout=5)


async def _mirror_synced(bus: _Bus, jetstream, wall, seconds: float) -> None:
    """Wait until the Node's WALL mirror holds the hub's last WALL message."""
    last = (await wall.stream_info(WALL_STREAM)).state.last_seq
    deadline = time.monotonic() + seconds
    while (await jetstream.stream_info(WALL_STREAM)).state.last_seq < last:
        assert time.monotonic() < deadline, f"the wall mirror never synced:\n{bus.logs()}"
        await asyncio.sleep(.2)


async def _declared(bus: _Bus, hub_port: int, bus_port: int):
    """The hub's WALL with a first value, every store line applied on the bus and its WALL mirror
    synced: (the hub client, the Node client, the hub's and the Node's JetStream)."""
    hub = await nats.connect(f"nats://127.0.0.1:{hub_port}", user=WALL_WRITER_USER,
                             password=WALL_WRITER_USER, allow_reconnect=False, connect_timeout=5)
    node = await _node_client(bus_port)
    wall, jetstream = hub.jetstream(timeout=10), node.jetstream(timeout=10)
    await declare(wall, wall_config(WALL_TABLE, first_seq=1))
    for slice_ in line_slices().values():
        await apply(jetstream, slice_)
    await declare(jetstream, wall_mirror_config())
    await wall.publish("wall.first", b"w")
    await _mirror_synced(bus, jetstream, wall, 60)
    return hub, node, wall, jetstream


async def _hold_consumers(jetstream, names) -> None:
    """Every stream at the server's consumer cap: each consumer is heap the fit charges
    (CONSUMER_HEAP), so the fence must hold all of them (E-W1-CONS-2)."""
    for name in names:
        for index in range(NODE_MAX_CONSUMERS + 1):
            consumer = ConsumerConfig(durable_name=f"held{index}", ack_policy=AckPolicy.EXPLICIT)
            if index < NODE_MAX_CONSUMERS:
                await jetstream.add_consumer(name, consumer)
                continue
            with pytest.raises(APIError) as refused:
                await jetstream.add_consumer(name, consumer)
            assert refused.value.err_code == MAXIMUM_CONSUMERS, (name, refused.value)


async def _fill(jetstream, buffers) -> None:
    """Every circular buffer full of the smallest event (its first sequence moved: it dropped its
    oldest), and every key of every keyed bucket written `history` times at its largest."""
    for name, config in buffers.items():
        if (table := table_of(config)) is not None:
            subjects, bodies, _ = _writes(config)
            for _ in range(table.history):
                for subject, body in zip(subjects, bodies, strict=True):
                    await jetstream.publish(subject, body)
        elif buffer_kind(config) == CIRCULAR:
            subject = _smallest_event(config)

            async def batch(subject: str = subject) -> None:
                await asyncio.gather(*(jetstream.publish(subject, b"", headers=event_headers(SCHEMA_MAJOR))
                                       for _ in range(FILL_BATCH)))
            # The first sequence with messages stored (an empty stream's may read otherwise).
            await batch()
            first = (await jetstream.stream_info(name)).state.first_seq
            while (await jetstream.stream_info(name)).state.first_seq == first:
                await batch()


async def _measure(bus: _Bus, hub_port: int, bus_port: int) -> None:
    buffers = _buffers()
    hub, node, _, jetstream = await _declared(bus, hub_port, bus_port)
    await _hold_consumers(jetstream, list(buffers))
    baseline = bus.sample()
    await _fill(jetstream, buffers)
    await asyncio.sleep(SETTLE_SECONDS)
    full = bus.sample()
    stored = (await jetstream.account_info()).memory
    factor = (full["anon_mib"] - baseline["anon_mib"]) * MIB / stored
    # The record the job keeps: the factor MEMORY_STORE_FACTOR must cover, measured on this host.
    print(f"smallest-event store: heap factor {factor:.2f} (fit {MEMORY_STORE_FACTOR}), stored {stored} B, "
          f"anon {baseline['anon_mib']:.1f} -> {full['anon_mib']:.1f} MiB, peak {full['peak_mib']:.1f} MiB "
          f"(GOMEMLIMIT + headroom {(NODE_BUS_GOMEMLIMIT + NODE_BUS_HEADROOM) // MIB} MiB)")
    assert full["oom_kill"] == 0 and full["starts"] == 1, (
        f"the bus was OOM-killed or restarted under its fence: {full}\n{bus.logs()}")
    assert factor <= MEMORY_STORE_FACTOR, f"heap per stored byte {factor:.2f} > {MEMORY_STORE_FACTOR}"
    assert full["peak_mib"] * MIB <= NODE_BUS_GOMEMLIMIT + NODE_BUS_HEADROOM, full
    await node.close()
    await hub.close()


async def _kill(bus: _Bus, hub_port: int, bus_port: int) -> None:
    hub, node, wall, jetstream = await _declared(bus, hub_port, bus_port)
    await _fill(jetstream, _buffers())
    value = (await wall.get_last_msg(WALL_STREAM, "wall.first")).data
    await node.close()
    killed = time.monotonic()
    bus.sh("kill -9 $(pidof nats-server)")
    deadline = killed + RESTART_SECONDS

    # The supervisor (the unit's Restart=always) starts it again; nothing the store held survives.
    while (sample := bus.sample())["starts"] < 2:
        assert time.monotonic() < deadline, f"the killed bus never restarted: {sample}\n{bus.logs()}"
        await asyncio.sleep(.2)
    while True:
        try:
            node = await _node_client(bus_port)
            break
        except Exception:   # Docker's published port takes a connection before the server listens
            assert time.monotonic() < deadline, f"the restarted bus never took a client:\n{bus.logs()}"
            await asyncio.sleep(.2)
    jetstream = node.jetstream(timeout=10)
    assert (await jetstream.account_info()).streams == 0, "a stream outlived a bus start"

    # Every line applies again without refusal, and a re-declared mirror takes the hub's value.
    for slice_ in line_slices().values():
        await apply(jetstream, slice_)
    assert await declare(jetstream, wall_mirror_config())
    await _mirror_synced(bus, jetstream, wall, deadline - time.monotonic())
    assert (await jetstream.get_last_msg(WALL_STREAM, "wall.first")).data == value
    elapsed, sample = time.monotonic() - killed, bus.sample()
    print(f"kill -9: back empty, every line applied, the mirror synced in {elapsed:.1f} s: {sample}")
    assert sample["oom_kill"] == 0 and sample["starts"] == 2, sample
    assert elapsed <= RESTART_SECONDS, elapsed
    await node.close()
    await hub.close()


async def _exercise(bus: _Bus, hub_port: int, bus_port: int, minutes: float) -> None:
    buffers = _buffers()
    names = list(buffers)
    hub, node, wall, jetstream = await _declared(bus, hub_port, bus_port)
    # Each stream's origin as the server holds it: `declare` stamps a Node stream's first_seq at the
    # create (E-W1-FV-1), so the built configuration carries none to compare against.
    start = await _states(jetstream, names)
    await _hold_consumers(jetstream, names)

    # Every buffer filled and written flat out, each writer inside its table: circular streams and
    # buckets drop their oldest, desired documents replace their own values, WALL churns its mirror.
    stop, counts, errors = asyncio.Event(), Counter(), Counter()
    writers = [asyncio.create_task(_writer(jetstream, name, *_writes(config), stop, counts, errors))
               for name, config in buffers.items() if name != WALL_STREAM]
    writers.append(asyncio.create_task(_writer(
        wall, WALL_STREAM, [f"wall.k{index}" for index in range(200)], [b"w" * 4000], False, stop, counts,
        errors)))
    samples = []
    end = time.monotonic() + minutes * 60
    try:
        while time.monotonic() < end:
            await asyncio.sleep(SAMPLE_SECONDS)
            sample = bus.sample()
            samples.append(sample)
            assert sample["oom_kill"] == 0 and sample["starts"] == 1, (
                f"the bus was OOM-killed or restarted under its fence: {sample}\n{bus.logs()}")
            assert not errors, f"a write was refused or lost: {dict(errors)}"
    finally:
        stop.set()
        await asyncio.gather(*writers)
    assert not errors, f"a write was refused or lost: {dict(errors)}"
    assert len(samples) >= minutes * 60 / SAMPLE_SECONDS / 2
    peak = max(sample["peak_mib"] for sample in samples)
    print(f"bus fence {NODE_BUS_MEMORY_MAX // MIB} MiB, GOMEMLIMIT {NODE_BUS_GOMEMLIMIT // MIB} MiB: "
          f"{sum(counts.values())} writes in {minutes} min, peak {peak:.1f} MiB, "
          f"anon up to {max(sample['anon_mib'] for sample in samples):.1f} MiB")

    # Every buffer was full: each circular one dropped its oldest, each state and desired bucket holds
    # every key `history` times, the mirror holds WALL's churn.
    full = await _states(jetstream, names)
    for name, config in buffers.items():
        messages, first, _, _ = full[name]
        if (table := table_of(config)) is not None:
            assert messages == table.history * len(table.sizes), name
        elif buffer_kind(config) == CIRCULAR:
            assert first > start[name][1], f"{name} never filled"
        else:
            assert (await jetstream.stream_info(name)).state.bytes > config.max_bytes // 2, name
    await node.close()
    await hub.close()


async def _stall(bus: _Bus, hub_name: str, hub_port: int, bus_port: int, seconds: float) -> None:
    user = central_user(SERIAL)
    central = await nats.connect(f"nats://127.0.0.1:{hub_port}", user=user, password=user,
                                 inbox_prefix=CENTRAL_INBOX_PREFIX, allow_reconnect=False, connect_timeout=5)
    wall = await nats.connect(f"nats://127.0.0.1:{hub_port}", user=WALL_WRITER_USER,
                              password=WALL_WRITER_USER, allow_reconnect=False, connect_timeout=5)
    node = await nats.connect(f"nats://127.0.0.1:{bus_port}", allow_reconnect=False, connect_timeout=5)
    jetstream, across = node.jetstream(timeout=10), central.jetstream(domain=NODE_DOMAIN, timeout=10)
    await declare(wall.jetstream(), wall_config(WALL_TABLE, first_seq=1))
    await declare(jetstream, wall_mirror_config())
    await declare(jetstream, event_buffer("player", "record", 4 * MIB))
    await declare(jetstream, event_buffer("host", "record", 4 * MIB))
    await declare(jetstream, desired_bucket("player", KeyTable({"show": 64}, history=2)))
    await (await DocumentWriter.bind(node, "KV_desired_player", writer="node")).put("show", b"node", expect=ABSENT)
    await wall.jetstream().publish("wall.first", b"w")
    deadline = time.monotonic() + 60
    while (await jetstream.stream_info(WALL_STREAM)).state.last_seq < 1:
        assert time.monotonic() < deadline, f"the wall mirror never linked:\n{bus.logs()}"
        await asyncio.sleep(.5)

    # Central's pull waits on an empty stream, its request already at the Node.
    await across.add_consumer("RECORD_player", ConsumerConfig(durable_name="central", ack_policy=AckPolicy.EXPLICIT))
    waiting_request = asyncio.get_running_loop().create_future()

    async def spy(message):
        if "expires" in json.loads(message.data) and not waiting_request.done():
            waiting_request.set_result(True)
    await node.subscribe("$JS.API.CONSUMER.MSG.NEXT.RECORD_player.central", cb=spy)
    await node.flush()
    pulling = asyncio.create_task(pull(central, "RECORD_player", "central", 50, timeout=seconds + 30,
                                       domain=NODE_DOMAIN))
    await asyncio.wait_for(waiting_request, 10)
    print(f"before the stall: {bus.sample()}")

    _docker("kill", "--signal=STOP", hub_name)
    started = time.monotonic()
    stop, sent = asyncio.Event(), Counter()
    record = os.urandom(MAX_STORED_MESSAGE - 1024)   # random, so nothing on the way compresses it

    async def flood(subject: str | None) -> None:
        # A publisher flat out on a local stream (a nodeapi outbox waits for each acknowledgement, so
        # this is more), or subscription churn when `subject` is None.
        while not stop.is_set():
            if subject is None:
                await (await node.subscribe(node.new_inbox())).unsubscribe()
            else:
                await node.publish(f"{subject}.{sent[subject] % 64}", record)
            sent[subject or "churn"] += 1
            if sent[subject or "churn"] % 20 == 0:
                await asyncio.sleep(0)
    samples = []
    floods: list[asyncio.Task] = []
    try:
        for index in range(40):
            await jetstream.publish(f"player.record.{index}", record)
        floods = [asyncio.create_task(flood(subject)) for subject in ("host.record", None)]
        while time.monotonic() - started < seconds:
            await asyncio.sleep(SAMPLE_SECONDS)
            try:
                samples.append(sample := bus.sample())
            except AssertionError as error:   # docker exec cannot start a process in a cgroup at its limit
                raise AssertionError(f"the bus's cgroup hit its fence under a stalled hub after "
                                     f"{time.monotonic() - started:.0f} s: {error}") from None
            assert sample["oom_kill"] == 0 and sample["starts"] == 1, (
                f"the bus was OOM-killed or restarted under a stalled hub: {sample} {dict(sent)}\n{bus.logs()}")
    finally:
        stop.set()
        _docker("kill", "--signal=CONT", hub_name)
    await asyncio.gather(*floods)
    assert all(count > 100 for count in sent.values()) and len(sent) == 2, dict(sent)
    assert (await jetstream.stream_info("RECORD_host")).state.messages > 0
    print(f"stalled hub {seconds:.0f} s: peak {max(sample['peak_mib'] for sample in samples):.1f} MiB "
          f"of {NODE_BUS_MEMORY_MAX // MIB}, sent {dict(sent)}")

    # The hub resumed: Central's pull returns, its conditional write lands, the mirror catches up.
    got = (await pulling).messages
    assert got and got[0].subject == "player.record.0", [message.subject for message in got]
    for message in got:
        await message.ack_sync()
    document = await DocumentWriter.bind(central, "KV_desired_player", writer=CENTRAL_WRITER, domain=NODE_DOMAIN)
    token = (await document.read("show")).token
    await document.put("show", b"central", expect=token)
    with pytest.raises(Conflict):
        await document.put("show", b"stale", expect=token)
    acknowledgement = await wall.jetstream().publish("wall.after", b"w")
    deadline = time.monotonic() + 30
    while (await jetstream.stream_info(WALL_STREAM)).state.last_seq < acknowledgement.seq:
        assert time.monotonic() < deadline, f"the wall mirror never caught up:\n{bus.logs()}"
        await asyncio.sleep(.2)
    sample = bus.sample()
    assert sample["oom_kill"] == 0 and sample["starts"] == 1, sample
    for client in (node, central, wall):
        await client.close()
