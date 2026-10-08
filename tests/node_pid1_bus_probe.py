"""The Node bus as a local client and the kernel see it (E3c): stdlib only, run by the node-pid1
`success` leg inside the PID1 container (`python3 node_pid1_bus_probe.py info 4222`) and imported
by the bus integration tests.

`info` prints {"server_name", "jetstream", "listen"}: the server's own INFO line from
127.0.0.1:<port>, and every address listening on that TCP port in /proc/net/tcp and /proc/net/tcp6.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import sys
from pathlib import Path

LISTEN = "0A"   # TCP_LISTEN in /proc/net/tcp's `st` column


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


def main(arguments: list[str]) -> None:
    command, port = arguments[0], int(arguments[1])
    if command != "info":
        raise SystemExit(f"unknown command: {command}")
    info = server_info(port)
    print(json.dumps({"server_name": info.get("server_name"), "jetstream": info.get("jetstream", False),
                      "listen": listen_addresses(port)}, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1:])
