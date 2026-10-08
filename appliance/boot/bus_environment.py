"""The Node bus's environment (E3b design §7.2 "Bus unit (E3c)"): every per-Node value the shipped
`node-bus.conf` and the Go runtime read, derived from the boot origin and the serial alone.

The handoff stage writes it as photo-wall-bus.service's EnvironmentFile: systemd reads that file
before a start's first process, so a unit's own ExecStartPre= could not write it (erratum
E-E3C-CUT-8). The leaf dials the boot origin's host and port, ws:// for an http origin and wss://
for https, under contracts.node_link.LEAF_PATH; one conf serves both. Stdlib, contracts and
uplink only: boot is an island.
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
    """The leaf's URL: ws:// for an http origin, wss:// for https, the origin's host and port
    unchanged (default port omitted, IPv6 bracketed); user and password both node_user(serial);
    path "/" + LEAF_PATH. ValueError("bus_origin_invalid") for a `central` that is no Central root,
    ValueError("node_link_serial") for an unsafe serial."""
    try:
        origin = Origin.parse_root(central)
    except UplinkError:
        raise ValueError("bus_origin_invalid") from None
    user = node_user(serial)
    authority = str(origin).split("://", 1)[1]
    return f"{_LEAF_SCHEMES[origin.scheme]}://{user}:{user}@{authority}/{LEAF_PATH}"


def bus_environment(central: str, serial: str) -> dict[str, str]:
    """Exactly the variables node-bus.conf and the Go runtime read."""
    return {
        "PHOTO_WALL_BUS_NAME": node_user(serial),
        "PHOTO_WALL_BUS_PORT": str(NODE_BUS_PORT),
        "PHOTO_WALL_BUS_LEAF_URL": leaf_url(central, serial),
        "GOMEMLIMIT": f"{NODE_BUS_GOMEMLIMIT // MIB}MiB",
    }


def write_bus_environment(root: Path, central: str, serial: str) -> Path:
    """bus_environment as a systemd EnvironmentFile at root / BUS_ENVIRONMENT, written atomically,
    mode 0600: KEY=value lines, keys sorted, unquoted (no value holds whitespace, a quote, `$` or
    a backslash: the origin and the serial are validated above)."""
    path = root / BUS_ENVIRONMENT
    environment = bus_environment(central, serial)
    text = "".join(f"{key}={environment[key]}\n" for key in sorted(environment))
    write_atomically(path, text.encode(), mode=0o600)
    return path
