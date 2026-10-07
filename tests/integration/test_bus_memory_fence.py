"""The Node bus inside its memory fence (owner answer STORE1 = A, erratum E-W1-STORE-1).

Every Node buffer is a file stream on tmpfs, so the store is RAM charged to the bus's cgroup, beside
the server's heap. This runs the pinned linux-arm64 nats-server in a container fenced as the bus
unit is (`--memory` = `--memory-swap` = NODE_BUS_MEMORY_MAX, GOMEMLIMIT = NODE_BUS_GOMEMLIMIT, the
store on `--tmpfs`) with the shipped `node-bus.conf` and the class table `nodeapi` builds, its wall
copy the real mirror of a hub's WALL. It fills every buffer and keeps writing flat out for minutes,
and fails on any OOM kill, server restart or refused write. Then it kills the server with the store
full: the restart in the same cgroup, which still holds the store's pages, must come back with every
stream and stay up (a store that cannot fit its fence on restart OOM-loops forever).

The shipped file binds the client port to loopback; `-a 0.0.0.0` (a command-line flag, which
nats-server applies over the file) lets Docker publish it. Nothing else differs from the file.

A second run hangs the hub (SIGSTOP) while a Node program floods every subject the hub may hold
interest in, aims replies at Central's inbox (its own requests' answers, JetStream API replies),
churns subscriptions on the leaf's inbound list and points push consumers across the leaf, with
Central's pull waiting: the leaf has no pending limit toward the hub, so anything that crossed would
queue in the bus until its fence OOM-killed it. The leaf binds an account no program reaches (errata
E-W1-LEAF-1, E-W1-LEAF-2); once the hub resumes, Central's pull, conditional write and the wall mirror
all work. A third run has a program delete every message it writes, then create consumers until the
server refuses one: a removal's tombstones and each consumer's files sit on the store's tmpfs, and
both drove the fence to an OOM loop before the server refused them (E-W1-LEAF-2).

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
    NodeStore,
    desired_documents,
    node_split,
)
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from nats.js.errors import APIError

from central.fleet.node_bus_accounts import HubListeners, hub_configuration
from contracts.node_link import (
    CENTRAL_INBOX_PREFIX,
    FILESTORE_BLOCK_BOUND,
    MAX_STORED_MESSAGE,
    METHOD_TOKEN,
    NODE_BUS_GOMEMLIMIT,
    NODE_BUS_MEMORY_MAX,
    NODE_DOMAIN,
    NODE_MAX_CONSUMERS,
    STORE_CHANGING_API,
    WALL_API_PREFIX,
    WALL_STREAM,
    WALL_WRITER_USER,
    central_user,
    node_user,
)
from nodeapi.buffers import (
    CIRCULAR,
    Documents,
    buffer,
    buffer_kind,
    declare,
    declare_table,
    epoch_of,
    sticky_bucket,
    wall_config,
    wall_copy,
    wall_mirror_config,
)
from nodeapi.documents import WRONG_LAST_SEQUENCE, DocumentWriter
from nodeapi.pull import pull
from scripts.nats_server import NATS_SERVER_VERSION

SERVER_VARIABLE = "PHOTO_WALL_BUS_FENCE_SERVER"
MINUTES_VARIABLE = "PHOTO_WALL_BUS_FENCE_MINUTES"
STALL_VARIABLE = "PHOTO_WALL_BUS_FENCE_STALL_SECONDS"
FLOOD_BODY = 200_000   # random, so nothing on the way compresses it
REMOVAL_SECONDS = 30.0
MAX_CONSUMERS_REACHED = 10026   # JSMaximumConsumersLimitErr: past node-bus.conf's max_consumers
IMAGE = "alpine:3.23.4@sha256:5b10f432ef3da1b8d4c7eb6c487f2f5a8f096bc91145e68878dd4a5019afde11"
MIB = 1024 * 1024
SERIAL = "fence-node"
SERVER = "/opt/nats/nats-server"
CONF = "/etc/photo-wall/node-bus.conf"
SAMPLE_SECONDS = 2.0
RELOAD_SECONDS = 20.0
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
                        "grep -E '^(anon|shmem) ' /sys/fs/cgroup/memory.stat").split("\n")
        stat = dict(line.split() for line in lines[4:])
        return {"current_mib": int(lines[0]) / MIB, "peak_mib": int(lines[1]) / MIB,
                "oom_kill": int(lines[2].split()[1]), "starts": int(lines[3]),
                "anon_mib": int(stat["anon"]) / MIB, "store_mib": int(stat["shmem"]) / MIB}

    def logs(self) -> str:
        done = subprocess.run(["docker", "logs", "--tail", "60", self.name], capture_output=True, text=True)
        return (done.stdout + done.stderr)[-6000:]


def _writes(name: str, config) -> tuple[list[str], list[bytes]]:
    """What one writer cycles through for one Node buffer: subjects and bodies inside its table."""
    if name.startswith("KV_desired_"):
        table = desired_documents(name.removeprefix("KV_desired_"))
        keys = sorted(table.sizes)
        return [table.subject_prefix + key for key in keys], [b"d" * table.sizes[key] for key in keys]
    if name.startswith("KV_"):
        return [f"$KV.{name.removeprefix('KV_')}.key{index}" for index in range(80)], [b"s" * 900]
    return [config.subjects[0].replace(">", "fill")], [b"r" * 200, b"r" * 4000]


async def _writer(jetstream, name: str, subjects: list[str], bodies: list[bytes], stop: asyncio.Event,
                  counts: Counter, errors: Counter) -> None:
    index = 0
    while not stop.is_set():
        try:
            await jetstream.publish(subjects[index % len(subjects)], bodies[index % len(bodies)])
            counts[name] += 1
        except Exception as error:   # every failure is the finding: nothing may refuse a write
            errors[f"{name}: {type(error).__name__}: {error}"[:160]] += 1
            await asyncio.sleep(.5)
        index += 1


async def _states(store: NodeStore, names) -> dict[str, tuple[int, int, int, str]]:
    states = {}
    for name in names:
        info = await store.stream_info(name)
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
        store_dir="/hubstore", max_file_store_bytes=HUB_STORE_BYTES)))
    hub_conf.chmod(0o644)
    user = node_user(SERIAL)
    platform = ("--platform", "linux/arm64")
    try:
        _docker("network", "create", network)
        version = _docker("run", "--rm", *platform, "-v", f"{binary}:{SERVER}:ro", IMAGE, SERVER, "--version")
        assert version == f"nats-server: v{NATS_SERVER_VERSION}", version
        _docker("run", "-d", "--name", hub_name, "--network", network, *platform, "-p", "127.0.0.1::4222",
                "-v", f"{binary}:{SERVER}:ro", "-v", f"{hub_conf}:/etc/photo-wall/hub.conf:ro",
                IMAGE, SERVER, "-c", "/etc/photo-wall/hub.conf")
        # The bus unit's fence: MemoryMax, no swap, GOMEMLIMIT, the store on tmpfs with no size= (a
        # sized tmpfs refuses writes when full, which the buffer rule forbids).
        _docker("run", "-d", "--name", bus.name, "--network", network, *platform, "--cgroupns=private",
                f"--memory={NODE_BUS_MEMORY_MAX // MIB}m", f"--memory-swap={NODE_BUS_MEMORY_MAX // MIB}m",
                "--tmpfs", "/store:rw", "-p", "127.0.0.1::4222",
                "-e", f"GOMEMLIMIT={NODE_BUS_GOMEMLIMIT // MIB}MiB",
                "-e", f"PHOTO_WALL_BUS_NAME={user}", "-e", "PHOTO_WALL_BUS_PORT=4222",
                "-e", "PHOTO_WALL_BUS_STORE=/store",
                "-e", f"PHOTO_WALL_BUS_LEAF_URL=ws://{user}:{user}@{hub_name}:8080/bus",
                "-v", f"{binary}:{SERVER}:ro", "-v", f"{NODE_BUS_CONF}:{CONF}:ro",
                IMAGE, "sh", "-c", SUPERVISOR)
        hub_port, bus_port = _published(hub_name, 4222), _published(bus.name, 4222)
        _accepts(hub_port, 30, "the hub")
        _accepts(bus_port, 30, "the bus")
        yield bus, hub_name, hub_port, bus_port
    finally:
        subprocess.run(["docker", "kill", "--signal=CONT", hub_name], capture_output=True)
        subprocess.run(["docker", "rm", "-f", bus.name, hub_name], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


def test_a_full_store_keeps_writing_inside_the_bus_fence_and_reloads_after_a_crash(tmp_path):
    minutes = float(os.environ.get(MINUTES_VARIABLE, "5"))
    with _fenced_bus(tmp_path) as (bus, _, hub_port, bus_port):
        asyncio.run(_exercise(bus, hub_port, bus_port, minutes))


def test_a_stalled_hub_never_pushes_the_bus_past_its_fence(tmp_path):
    seconds = float(os.environ.get(STALL_VARIABLE, "60"))
    with _fenced_bus(tmp_path) as (bus, hub_name, hub_port, bus_port):
        asyncio.run(_stall(bus, hub_name, hub_port, bus_port, seconds))


def test_no_removal_or_consumer_grows_the_store_past_its_fence(tmp_path):
    with _fenced_bus(tmp_path) as (bus, _, _, bus_port):
        asyncio.run(_removals_and_consumers(bus, bus_port, REMOVAL_SECONDS))


async def _exercise(bus: _Bus, hub_port: int, bus_port: int, minutes: float) -> None:
    table = node_split()
    names = list(table.buffers)
    hub = await nats.connect(f"nats://127.0.0.1:{hub_port}", user=WALL_WRITER_USER,
                             password=WALL_WRITER_USER, allow_reconnect=False, connect_timeout=5)
    node = await nats.connect(f"nats://127.0.0.1:{bus_port}", allow_reconnect=False, connect_timeout=5)
    wall, jetstream, store = hub.jetstream(timeout=10), node.jetstream(timeout=10), NodeStore(node, timeout=10)
    await declare(wall, wall_config())
    await declare_table(node, table, timeout=10)
    await wall.publish("wall.first", b"w")
    deadline = time.monotonic() + 60
    while (await store.mirror.stream_info(WALL_STREAM)).state.last_seq < 1:
        assert time.monotonic() < deadline, f"the wall mirror never linked:\n{bus.logs()}"
        await asyncio.sleep(.5)
    # Each stream's origin as the server holds it: `declare` stamps a Node stream's first_seq at the
    # create (E-W1-FV-1), so the built configuration carries none to compare against.
    start = await _states(store, names)

    # Every buffer filled and written flat out, each writer inside its table: circular streams and
    # buckets drop their oldest, desired documents replace their own values, WALL churns its mirror.
    stop, counts, errors = asyncio.Event(), Counter(), Counter()
    writers = [asyncio.create_task(_writer(jetstream, name, *_writes(name, config), stop, counts, errors))
               for name, config in table.buffers.items() if name != WALL_STREAM]
    writers.append(asyncio.create_task(_writer(
        wall, WALL_STREAM, [f"wall.k{index}" for index in range(200)], [b"w" * 4000], stop, counts, errors)))
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
          f"store up to {max(sample['store_mib'] for sample in samples):.1f} MiB")

    # Every buffer was full: each circular one dropped its oldest, each desired bucket holds every
    # document `history` times, the mirror holds WALL's churn.
    full = await _states(store, names)
    for name, config in table.buffers.items():
        messages, first, _, _ = full[name]
        if name.startswith("KV_desired_"):
            documents = desired_documents(name.removeprefix("KV_desired_"))
            assert messages == documents.history * len(documents.sizes), name
        elif buffer_kind(config) == CIRCULAR:
            assert first > start[name][1], f"{name} never filled"
        else:
            assert (await store.stream_info(name)).state.bytes > config.max_bytes // 2, name
    await node.close()
    await hub.close()

    # The crash with the store full: the server restarts in the same cgroup, whose store pages are
    # still charged, reloads every stream and stays up.
    bus.sh(f"kill -9 $(pidof {Path(SERVER).name})")
    deadline = time.monotonic() + 60
    while bus.sample()["starts"] < 2:
        assert time.monotonic() < deadline, f"the bus never restarted:\n{bus.logs()}"
        await asyncio.sleep(.5)
    _accepts(bus_port, 60, "the restarted bus")
    reloaded = time.monotonic() + RELOAD_SECONDS
    while time.monotonic() < reloaded:
        sample = bus.sample()
        assert sample["oom_kill"] == 0 and sample["starts"] == 2, (
            f"the bus did not come back with its full store inside its fence: {sample}\n{bus.logs()}")
        await asyncio.sleep(SAMPLE_SECONDS)
    node = await nats.connect(f"nats://127.0.0.1:{bus_port}", allow_reconnect=False, connect_timeout=5)
    jetstream, store = node.jetstream(timeout=10), NodeStore(node, timeout=10)
    assert await _states(store, names) == full
    await declare_table(node, table, timeout=10)   # every connect re-declares: a no-op on the full store
    for name in names:
        if name == WALL_STREAM:
            continue
        subjects, bodies = _writes(name, table.buffers[name])
        acknowledgement = await jetstream.publish(subjects[0], bodies[0])
        assert (acknowledgement.stream, acknowledgement.seq) == (name, full[name][2] + 1)
    print(f"reloaded full store: {bus.sample()}")
    await node.close()


async def _stall(bus: _Bus, hub_name: str, hub_port: int, bus_port: int, seconds: float) -> None:
    user = central_user(SERIAL)
    central = await nats.connect(f"nats://127.0.0.1:{hub_port}", user=user, password=user,
                                 inbox_prefix=CENTRAL_INBOX_PREFIX, allow_reconnect=False, connect_timeout=5)
    wall = await nats.connect(f"nats://127.0.0.1:{hub_port}", user=WALL_WRITER_USER,
                              password=WALL_WRITER_USER, allow_reconnect=False, connect_timeout=5)
    refused = Counter()

    async def count(error):   # the program's replies aimed at a tracked response, refused locally
        refused[type(error).__name__] += 1
    node = await nats.connect(f"nats://127.0.0.1:{bus_port}", allow_reconnect=False, connect_timeout=5,
                              error_cb=count)
    # The program's second connection answers its own requests (one connection doing both would stall
    # on its own deliveries).
    responder = await nats.connect(f"nats://127.0.0.1:{bus_port}", allow_reconnect=False, connect_timeout=5)
    jetstream, across = node.jetstream(timeout=10), central.jetstream(domain=NODE_DOMAIN, timeout=10)
    mirror = wall_copy(node, timeout=10)
    await declare(wall.jetstream(), wall_config())
    await declare(mirror, wall_mirror_config())
    await declare(jetstream, buffer("REC_probe", 4 * MIB, subjects=["probe.record.>"]))
    await declare(jetstream, buffer("REC_flood", 4 * MIB, subjects=["flood.>"]))
    table = Documents.bucket("desired_probe", {"show": 64}, history=2)
    await declare(jetstream, sticky_bucket(table))
    await DocumentWriter(jetstream, table).put("show", b"node")
    await wall.jetstream().publish("wall.first", b"w")
    deadline = time.monotonic() + 60
    while (await mirror.stream_info(WALL_STREAM)).state.last_seq < 1:
        assert time.monotonic() < deadline, f"the wall mirror never linked:\n{bus.logs()}"
        await asyncio.sleep(.5)

    # A component's method: the reply it is delivered is its server's tracked response, never
    # Central's inbox.
    replies: list[str] = []

    async def reveal(message):
        replies.append(message.reply)
        await message.respond(b"ok")
    await responder.subscribe("probe.method.reveal", cb=reveal)
    requests = await responder.subscribe("player.echo")
    await responder.flush()
    while not replies:
        assert time.monotonic() < deadline, "no method across the leaf"
        try:
            await central.request("probe.method.reveal", b"", timeout=1)
        except (nats.errors.NoRespondersError, nats.errors.TimeoutError):
            await asyncio.sleep(.2)
    tracked = replies[-1]
    assert tracked.startswith("_R_."), tracked

    # Central's pull waits on an empty stream, and a live wildcard inbox of Central's is open: the
    # flood's targets, as if a program had learnt it.
    await across.add_consumer("REC_probe", ConsumerConfig(durable_name="central", ack_policy=AckPolicy.EXPLICIT))
    pulling = asyncio.create_task(pull(central, "REC_probe", "central", 50, timeout=seconds + 30,
                                       domain=NODE_DOMAIN))
    while (await jetstream.consumer_info("REC_probe", "central")).num_waiting < 1:
        assert time.monotonic() < deadline, "Central's pull never waited on the Node"
        await asyncio.sleep(.2)
    central_inbox = central.new_inbox()
    leaked = Counter()

    async def collect(message):
        leaked[message.subject] += 1
    await central.subscribe(central_inbox + ".*", cb=collect)
    await central.flush()
    await asyncio.sleep(.5)
    print(f"before the stall: {bus.sample()}")

    _docker("kill", "--signal=STOP", hub_name)
    started = time.monotonic()
    stop, sent = asyncio.Event(), Counter()
    consumer_api = f"{WALL_API_PREFIX}.CONSUMER.CREATE.{WALL_STREAM}"
    targets = [f"{central_inbox}.flood", consumer_api, "$JS.FC.WALL.a.b", tracked]
    body = os.urandom(FLOOD_BODY)

    async def publish(index):        # 200 KB on every subject the hub holds interest in
        await node.publish(["player.event.x", *targets[:3]][index % 4], body)

    async def reflect(index):        # a request to its own subscription, its 200 KB answer aimed there
        await node.publish("player.echo", b"r", reply=targets[index % 4])
        await node.flush()
        if index % 4 != 3:            # a tracked response as the reply is refused: nothing to answer
            await (await requests.next_msg(timeout=10)).respond(body)

    async def api(index):            # a JetStream API request whose 200 KB reply is aimed there, paced
        await node.publish("$JS.API.STREAM.MSG.GET.REC_flood", get, reply=targets[index % 4])
        await node.flush()

    async def churn(index):          # interest on the leaf's inbound list, and elsewhere
        subject = [f"z{index}.{METHOD_TOKEN}.x", f"$SRV.z{index}", node.new_inbox()][index % 3]
        await (await node.subscribe(subject)).unsubscribe()

    async def flood(name, act) -> None:
        while not stop.is_set():
            await act(sent[name])
            sent[name] += 1
            if sent[name] % 20 == 0:
                await asyncio.sleep(0)
    pushers = [f"push{target}{index}" for target in range(2) for index in range(NODE_MAX_CONSUMERS // 2)]
    samples = []
    try:
        record = os.urandom(MAX_STORED_MESSAGE - 1024)
        for index in range(40):
            await jetstream.publish(f"probe.record.{index}", record)
            stored = (await jetstream.publish(f"flood.{index}", record)).seq
        get = json.dumps({"seq": stored}).encode()
        for name in pushers:
            await jetstream.add_consumer("REC_flood", ConsumerConfig(
                durable_name=name, deliver_subject=(consumer_api, f"{central_inbox}.push")[int(name[4])],
                ack_policy=AckPolicy.NONE, deliver_policy=DeliverPolicy.ALL))
        floods = [asyncio.create_task(flood(name, act)) for name, act in (
            ("publish", publish), ("reflect", reflect), ("api", api), ("churn", churn))]
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
    assert all(count > 100 for count in sent.values()) and len(sent) == 4, dict(sent)
    assert refused, "the program's replies aimed at a tracked response were never refused"
    print(f"stalled hub {seconds:.0f} s: peak {max(sample['peak_mib'] for sample in samples):.1f} MiB "
          f"of {NODE_BUS_MEMORY_MAX // MIB}, sent {dict(sent)}")
    bound = [name for name in pushers if (await jetstream.consumer_info("REC_flood", name)).push_bound]
    assert bound == [], f"push consumers bound to a subject across the leaf: {bound}"

    # The hub resumed: Central's pull returns, its conditional write lands, the mirror catches up, and
    # nothing the program sent reached Central's inbox.
    got = await pulling
    assert got and got[0].subject == "probe.record.0", [message.subject for message in got]
    for message in got:
        await message.ack_sync()
    document = DocumentWriter.node_bucket(central, table)
    _, token = await document.read("show")
    await document.put("show", b"central", token=token)
    with pytest.raises(APIError) as stale:
        await document.put("show", b"stale", token=token)
    assert stale.value.err_code == WRONG_LAST_SEQUENCE
    acknowledgement = await wall.jetstream().publish("wall.after", b"w")
    deadline = time.monotonic() + 30
    while (await mirror.stream_info(WALL_STREAM)).state.last_seq < acknowledgement.seq:
        assert time.monotonic() < deadline, f"the wall mirror never caught up:\n{bus.logs()}"
        await asyncio.sleep(.2)
    await central.flush()
    assert not leaked, dict(leaked)
    sample = bus.sample()
    assert sample["oom_kill"] == 0 and sample["starts"] == 1, sample
    for client in (node, responder, central, wall):
        await client.close()


async def _removals_and_consumers(bus: _Bus, bus_port: int, seconds: float) -> None:
    # A program writes and deletes each message flat out on one stream (a user delete writes a
    # tombstone into a new block, freed only at the store's 2-minute sync: 224 MiB peak within 5 s
    # before the server refused it), then creates consumers on four streams until the server refuses
    # one (3,820 durables OOM-looped the bus before). Each is refused; the store stays at its charge.
    refused = Counter()

    async def count(error):
        refused[str(error).split('"')[1] if '"' in str(error) else str(error)] += 1
    node = await nats.connect(f"nats://127.0.0.1:{bus_port}", allow_reconnect=False, connect_timeout=5,
                              error_cb=count)
    jetstream = node.jetstream(timeout=10)
    cap = MIB
    await declare(jetstream, buffer("REC_churn", cap, subjects=["churn.>"]))
    delete = STORE_CHANGING_API[0].removesuffix(".>")
    started, written, samples = time.monotonic(), 0, []
    sampled = started
    while time.monotonic() - started < seconds:
        seq = (await jetstream.publish("churn.x", b"c" * 100)).seq
        await node.publish(f"$JS.API.{delete}.REC_churn", json.dumps({"seq": seq}).encode(), reply=node.new_inbox())
        written += 1
        if written % 50 == 0:
            await node.flush()
        if time.monotonic() - sampled >= SAMPLE_SECONDS:
            sampled = time.monotonic()
            samples.append(sample := bus.sample())
            assert sample["oom_kill"] == 0 and sample["starts"] == 1, (
                f"the bus was OOM-killed or restarted under write-and-delete: {sample}\n{bus.logs()}")
    await node.flush()
    state = (await jetstream.stream_info("REC_churn")).state
    assert state.last_seq - state.first_seq + 1 == state.messages and not state.num_deleted, state
    store_mib = float(bus.sh("du -sk /store | cut -f1")) / 1024
    assert store_mib * MIB <= cap + FILESTORE_BLOCK_BOUND + MIB, f"the store holds {store_mib:.1f} MiB"
    assert refused, "no delete was refused"
    print(f"write-and-delete {seconds:.0f} s: {written} writes, store {store_mib:.2f} MiB, "
          f"peak {max(sample['peak_mib'] for sample in samples):.1f} MiB")

    # Consumers until the server refuses one, on four empty streams: NODE_MAX_CONSUMERS each.
    created = Counter()
    for stream in ("REC_c0", "REC_c1", "REC_c2", "REC_c3"):
        await declare(jetstream, buffer(stream, 64 * 1024, subjects=[f"{stream.lower()}.>"]))
        while True:
            try:
                await jetstream.add_consumer(stream, ConsumerConfig(
                    durable_name=f"d{created[stream]}", ack_policy=AckPolicy.EXPLICIT))
            except APIError as error:
                assert error.err_code == MAX_CONSUMERS_REACHED, error
                break
            created[stream] += 1
            assert created[stream] <= NODE_MAX_CONSUMERS, dict(created)
    assert set(created.values()) == {NODE_MAX_CONSUMERS}, dict(created)
    sample = bus.sample()
    assert sample["oom_kill"] == 0 and sample["starts"] == 1, sample
    print(f"consumers {dict(created)}: {sample}")
    await node.close()
