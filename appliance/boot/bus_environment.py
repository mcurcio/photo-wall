"""The Node bus's environment (E3b design §7.2 "Bus unit (E3c)"): every per-Node value the shipped
`node-bus.conf` and the Go runtime read, derived from the boot origin and the serial alone.

The handoff stage writes it as photo-wall-bus.service's EnvironmentFile: systemd reads that file
before a start's first process, so a unit's own ExecStartPre= could not write it (erratum
E-E3C-CUT-8). The leaf dials the boot origin's host and port, ws:// for an http origin and wss://
for https, under contracts.node_link.LEAF_PATH; one conf serves both. The port is always written:
nats-server reads a ws/wss remote with none as port 7422, not 80/443 (E-E3C-S1-3). nats-server
parses a `$VAR`'s value as config, not as text, so every string the conf reads arrives as a quoted
config string (an IPv6 host's `]` would otherwise end the conf's urls array: E-E3C-S1-5), and the
file quotes every value so systemd hands each one over unchanged. Stdlib, contracts and uplink only:
boot is an island.
"""
from __future__ import annotations

from pathlib import Path
from typing import Final

from contracts.node_link import LEAF_PATH, NODE_BUS_GOMEMLIMIT, NODE_BUS_PORT, node_user
from uplink.causes import UplinkError
from uplink.files import write_atomically
from uplink.origin import Origin

BUS_ENVIRONMENT: Final = Path("run/photo-wall-node/bus.env")  # relative to the root, as HANDOFF
MIB: Final = 1024 * 1024
_LEAF_SCHEMES: Final = {"http": "ws", "https": "wss"}

# GOMEMLIMIT is written in whole MiB, so the bus runs at exactly the number the fit checks.
if NODE_BUS_GOMEMLIMIT % MIB:
    raise RuntimeError("bus_gomemlimit_not_whole_mib")


def leaf_url(central: str, serial: str) -> str:
    """The leaf's URL: ws:// for an http origin, wss:// for https, the origin's host (IPv6
    bracketed) and port, always explicit (80/443 for a default-port origin: nats-server fills a
    missing ws/wss port with its leafnode port, 7422); user and password both node_user(serial);
    path "/" + LEAF_PATH. ValueError("bus_origin_invalid") for a `central` that is no Central root,
    ValueError("node_link_serial") for an unsafe serial."""
    try:
        origin = Origin.parse_root(central)
    except UplinkError:
        raise ValueError("bus_origin_invalid") from None
    user = node_user(serial)
    host = f"[{origin.host}]" if ":" in origin.host else origin.host
    return f"{_LEAF_SCHEMES[origin.scheme]}://{user}:{user}@{host}:{origin.port}/{LEAF_PATH}"


def config_string(value: str) -> str:
    """`value` as a nats-server config string, double-quoted: nats-server parses a `$VAR`'s value
    as config, so an unquoted string is re-lexed (E-E3C-S1-5). ValueError("bus_value_unquotable")
    for a value holding a double quote, a backslash or a line break, which the quotes would not
    carry verbatim (none can: the origin and the serial are validated)."""
    if any(c in value for c in '"\\\n\r'):
        raise ValueError("bus_value_unquotable")
    return f'"{value}"'


def bus_environment(central: str, serial: str) -> dict[str, str]:
    """Exactly the variables node-bus.conf and the Go runtime read, as the process sees them:
    the conf's strings as quoted config strings, its port a bare number, GOMEMLIMIT Go's text."""
    return {
        "PHOTO_WALL_BUS_NAME": config_string(node_user(serial)),
        "PHOTO_WALL_BUS_PORT": str(NODE_BUS_PORT),
        "PHOTO_WALL_BUS_LEAF_URL": config_string(leaf_url(central, serial)),
        "GOMEMLIMIT": f"{NODE_BUS_GOMEMLIMIT // MIB}MiB",
    }


def write_bus_environment(root: Path, central: str, serial: str) -> Path:
    """bus_environment as a systemd EnvironmentFile at root / BUS_ENVIRONMENT, written atomically,
    mode 0600: KEY='value' lines, keys sorted. systemd strips a value's enclosing double quotes, so
    every value is single-quoted, which systemd reads verbatim to the closing quote.
    ValueError("bus_value_unquotable") for a value holding a single quote or a line break."""
    path = root / BUS_ENVIRONMENT
    environment = bus_environment(central, serial)
    if any(c in value for value in environment.values() for c in "'\n\r"):
        raise ValueError("bus_value_unquotable")
    text = "".join(f"{key}='{environment[key]}'\n" for key in sorted(environment))
    write_atomically(path, text.encode(), mode=0o600)
    return path
