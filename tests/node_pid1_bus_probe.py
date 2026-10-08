"""The Node bus as a local client and the kernel see it (E3c, E3e): run by the node-pid1 `success` and
`join` legs inside the PID1 container (`python3 -I -B node_pid1_bus_probe.py info|birth|fill|wall 4222`)
and imported by the bus integration tests. Stdlib only at import.

`info` prints {"server_name", "jetstream", "listen"}: the server's own INFO line from
127.0.0.1:<port>, and every address listening on that TCP port in /proc/net/tcp and /proc/net/tcp6.

`birth` prints {"birth", "base", "epoch"}: the host component's `birth` and `base` from its state
bucket and that bucket's epoch, read with the nats-py and `nodeapi` HostCore's launcher ships
(HOST_CORE first on sys.path), so it also proves the package's copy imports on the Node's python3.

`wall` prints {"value"}: as every wall reader does at attach, it creates the Node's WALL mirror when it
is absent (`nodeapi.buffers.wall_mirror_config`, create only), then reads the mirror's latest value of
the wall key WALL_KEY (None while the mirror holds none), with HostCore's shipped nats-py and nodeapi.

`fill` prints {"sent", "ended"}: with the same nats-py, it fills a FILL_BYTES memory stream with
FILL_MESSAGE-byte messages, up to FILL_PASSES times over, until the server ends the connection
(`ended`, the error's type): a bus under a lowered memory cap is OOM-killed (the `success` leg).
"""
from __future__ import annotations

import ipaddress
import json
import socket
import sys
from pathlib import Path

LISTEN = "0A"   # TCP_LISTEN in /proc/net/tcp's `st` column
HOST_CORE = "/usr/lib/photo-wall-host-core"
HOST_STATE = "KV_state_host"
# The induced OOM's stream: 8 MiB of the store's free room beside the host line, in small messages,
# whose heap per stored byte is the largest (erratum E-E3C-S2-1).
FILL_SUBJECT = "probe.fill"
FILL_BYTES = 8 * 1024 * 1024
FILL_MESSAGE = 1024
FILL_PASSES = 4
WALL_KEY = "timing"   # the one key of Central's placeholder wall table (central.node_bus_wiring.WALL_TABLE)


def server_info(port: int, host: str = "127.0.0.1", timeout: float = 5.0) -> dict:
    """The JSON of the INFO line a NATS server sends a new client first."""
    with socket.create_connection((host, port), timeout=timeout) as connection:
        line = connection.makefile("rb").readline(1024 * 1024)
    verb, _, body = line.decode().partition(" ")
    if verb != "INFO":
        raise ValueError(f"not a NATS INFO line: {line[:64]!r}")
    return json.loads(body)


def _address(text: str) -> str:
    # The kernel prints each 32-bit word of the address in host byte order.
    raw = bytes.fromhex(text)
    words = b"".join(raw[index:index + 4][::-1] for index in range(0, len(raw), 4))
    return str(ipaddress.ip_address(words))


def listen_addresses(port: int, proc: Path = Path("/proc/net")) -> list[str]:
    """Every local address with a LISTEN socket on `port`, IPv4 and IPv6, sorted."""
    found = set()
    for name in ("tcp", "tcp6"):
        table = proc / name
        if not table.exists():
            continue
        for row in table.read_text().splitlines()[1:]:
            local, state = row.split()[1], row.split()[3]
            address, _, hexport = local.partition(":")
            if state == LISTEN and int(hexport, 16) == port:
                found.add(_address(address))
    return sorted(found)


async def host_state(client) -> dict:
    """{"birth", "base", "epoch"} of the host's state bucket through a connected nats-py `client`;
    raises nats.js.errors.NotFoundError while the bucket or a key is absent."""
    from nodeapi.epoch import stream_epoch

    jetstream = client.jetstream()
    bucket = await jetstream.key_value(HOST_STATE.removeprefix("KV_"))
    values = {key: json.loads((await bucket.get(key)).value) for key in ("birth", "base")}
    return {**values, "epoch": await stream_epoch(jetstream, HOST_STATE)}


def host_birth(port: int) -> dict:
    """host_state of the bus on 127.0.0.1:<port>, with HostCore's shipped nats-py and nodeapi."""
    sys.path.insert(0, HOST_CORE)
    import asyncio

    import nats

    async def read() -> dict:
        client = await nats.connect(servers=[f"nats://127.0.0.1:{port}"], allow_reconnect=False,
                                    connect_timeout=2)
        try:
            return await host_state(client)
        finally:
            await client.close()

    return asyncio.run(read())


def wall(port: int) -> dict:
    """{"value": the Node's WALL mirror's latest WALL_KEY as text, or None}, the mirror created first
    when it is absent, on the bus on 127.0.0.1:<port>, with HostCore's shipped nats-py and nodeapi."""
    sys.path.insert(0, HOST_CORE)
    import asyncio

    import nats
    from nats.js.errors import NotFoundError

    from contracts.node_link import WALL_STREAM
    from nodeapi.buffers import WALL_PREFIX, declare, wall_mirror_config

    async def read() -> dict:
        client = await nats.connect(servers=[f"nats://127.0.0.1:{port}"], allow_reconnect=False,
                                    connect_timeout=2)
        try:
            jetstream = client.jetstream()
            await declare(jetstream, wall_mirror_config())
            try:
                message = await jetstream.get_last_msg(WALL_STREAM, WALL_PREFIX + WALL_KEY)
            except NotFoundError:
                return {"value": None}
            return {"value": message.data.decode()}
        finally:
            await client.close()

    return asyncio.run(read())


def fill(port: int) -> dict:
    """Fill a FILL_BYTES memory stream on the bus on 127.0.0.1:<port> until the server ends the
    connection or FILL_PASSES times its bytes are sent: {"sent": messages, "ended": error type or None}."""
    sys.path.insert(0, HOST_CORE)
    import asyncio

    import nats
    from nats.js.api import StorageType, StreamConfig

    async def run() -> dict:
        client = await nats.connect(servers=[f"nats://127.0.0.1:{port}"], allow_reconnect=False,
                                    connect_timeout=2)
        sent = 0
        try:
            await client.jetstream().add_stream(StreamConfig(
                name="PROBE_FILL", subjects=[FILL_SUBJECT], storage=StorageType.MEMORY, max_bytes=FILL_BYTES))
            payload = bytes(FILL_MESSAGE)
            for _ in range(FILL_PASSES * FILL_BYTES // FILL_MESSAGE):
                await client.publish(FILL_SUBJECT, payload)
                sent += 1
                if sent % 256 == 0:
                    await client.flush(timeout=5)
            await client.flush(timeout=5)
        except Exception as error:  # noqa: BLE001 - the OOM kill ends the connection: the point
            return {"sent": sent, "ended": type(error).__name__}
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - the server may already be gone
                pass
        return {"sent": sent, "ended": None}

    return asyncio.run(run())


def main(arguments: list[str]) -> None:
    command, port = arguments[0], int(arguments[1])
    if command == "birth":
        print(json.dumps(host_birth(port), sort_keys=True))
        return
    if command == "fill":
        print(json.dumps(fill(port), sort_keys=True))
        return
    if command == "wall":
        print(json.dumps(wall(port), sort_keys=True))
        return
    if command != "info":
        raise SystemExit(f"unknown command: {command}")
    info = server_info(port)
    print(json.dumps({"server_name": info.get("server_name"), "jetstream": info.get("jetstream", False),
                      "listen": listen_addresses(port)}, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1:])
