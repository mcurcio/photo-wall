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

Needs Docker and PHOTO_WALL_BUS_FENCE_SERVER, a linux-arm64 nats-server (`scripts/nats_server.py
fetch` on an arm64 Linux host); checks.yml's `bus-fence` job runs it on ubuntu-24.04-arm.
PHOTO_WALL_BUS_FENCE_MINUTES sets the write phase (default 5).
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
import uuid
from collections import Counter
from pathlib import Path

import nats
import pytest
from integration.bus_servers import HUB_STORE_BYTES, NODE_BUS_CONF, desired_documents, node_split

from central.fleet.node_bus_accounts import HubListeners, hub_configuration
from contracts.node_link import (
    NODE_BUS_GOMEMLIMIT,
    NODE_BUS_MEMORY_MAX,
    WALL_STREAM,
    WALL_WRITER_USER,
    node_user,
)
from nodeapi.buffers import CIRCULAR, buffer_kind, declare, declare_table, epoch_of, wall_config
from scripts.nats_server import NATS_SERVER_VERSION

SERVER_VARIABLE = "PHOTO_WALL_BUS_FENCE_SERVER"
MINUTES_VARIABLE = "PHOTO_WALL_BUS_FENCE_MINUTES"
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


def test_a_full_store_keeps_writing_inside_the_bus_fence_and_reloads_after_a_crash(tmp_path):
    binary = _fence_server()
    minutes = float(os.environ.get(MINUTES_VARIABLE, "5"))
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
        asyncio.run(_exercise(bus, hub_port, bus_port, minutes))
    finally:
        subprocess.run(["docker", "rm", "-f", bus.name, hub_name], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


async def _exercise(bus: _Bus, hub_port: int, bus_port: int, minutes: float) -> None:
    table = node_split()
    names = list(table.buffers)
    hub = await nats.connect(f"nats://127.0.0.1:{hub_port}", user=WALL_WRITER_USER,
                             password=WALL_WRITER_USER, allow_reconnect=False, connect_timeout=5)
    node = await nats.connect(f"nats://127.0.0.1:{bus_port}", allow_reconnect=False, connect_timeout=5)
    wall, jetstream = hub.jetstream(timeout=10), node.jetstream(timeout=10)
    await declare(wall, wall_config())
    await declare_table(jetstream, table)
    await wall.publish("wall.first", b"w")
    deadline = time.monotonic() + 60
    while (await jetstream.stream_info(WALL_STREAM)).state.last_seq < 1:
        assert time.monotonic() < deadline, f"the wall mirror never linked:\n{bus.logs()}"
        await asyncio.sleep(.5)

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
    full = await _states(jetstream, names)
    for name, config in table.buffers.items():
        messages, first, _, _ = full[name]
        if name.startswith("KV_desired_"):
            documents = desired_documents(name.removeprefix("KV_desired_"))
            assert messages == documents.history * len(documents.sizes), name
        elif buffer_kind(config) == CIRCULAR:
            assert first > config.first_seq, f"{name} never filled"
        else:
            assert (await jetstream.stream_info(name)).state.bytes > config.max_bytes // 2, name
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
    jetstream = node.jetstream(timeout=10)
    assert await _states(jetstream, names) == full
    await declare_table(jetstream, table)   # every connect re-declares: a no-op on the full store
    for name in names:
        if name == WALL_STREAM:
            continue
        subjects, bodies = _writes(name, table.buffers[name])
        acknowledgement = await jetstream.publish(subjects[0], bodies[0])
        assert (acknowledgement.stream, acknowledgement.seq) == (name, full[name][2] + 1)
    print(f"reloaded full store: {bus.sample()}")
    await node.close()
