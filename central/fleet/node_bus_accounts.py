"""Fleet's hub bus configuration: one account per Node, plus the wall-wide account (0017 C4/C5, API6).

Pure: no I/O and no NATS client. The output is nats-server configuration written as JSON text,
which the server's configuration parser reads, so the same set of serials always gives the same
bytes and a reload changes only what the enrolled set changed.

Each Node account holds the Node's leaf user and Central's client user, both selectors on the
trusted network rather than secrets. It has no JetStream (the Node holds its own objects), no
export and no import except the wall-wide set: this module has no form for any other.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from contracts.node_link import (
    HUB_DOMAIN,
    WALL_ACCOUNT,
    WALL_API_PREFIX,
    WALL_DELIVER_PREFIX,
    WALL_STREAM,
    WALL_STREAM_BYTES,
    WALL_WRITER_USER,
    account_id,
    central_user,
    node_user,
)

FLEET_SYSTEM_USER: Final = "fleet"
SYSTEM_ACCOUNT: Final = "SYS"

# What a Node's mirror of WALL needs from the WALL account: the consumer API it creates and
# deletes its mirror consumer through, the delivery subjects, and flow control (v1 and v2 forms).
# Per the cross-account-subjects reference; the consumer API is imported under WALL_API_PREFIX.
_WALL_CONSUMER_SERVICES: Final = (
    f"$JS.API.CONSUMER.CREATE.{WALL_STREAM}",
    f"$JS.API.CONSUMER.CREATE.{WALL_STREAM}.>",
    f"$JS.API.CONSUMER.DELETE.{WALL_STREAM}.*",
)
_WALL_DELIVERY_STREAM: Final = f"{WALL_DELIVER_PREFIX}.>"
_WALL_FLOW_CONTROL_SERVICES: Final = (f"$JS.FC.{WALL_STREAM}.>", f"$JS.FC.*.*.{WALL_STREAM}.>")


@dataclass(frozen=True, slots=True)
class HubListeners:
    server_name: str
    client_host: str
    client_port: int
    websocket_host: str
    websocket_port: int          # the Nodes' leaf listener: ws://<user>:<user>@<host>:<port>[/<prefix>]
    leaf_host: str               # nats-server accepts WebSocket leaves only with a leafnodes port; no Node dials this one
    leaf_port: int
    monitor_port: int | None     # /leafz; None in production until E3d decides
    store_dir: str
    max_file_store_bytes: int    # the hub's JetStream store


def hub_configuration(serials: Iterable[str], listeners: HubListeners) -> str:
    """The hub's nats-server configuration for this set of enrolled serials, as JSON text."""
    if listeners.max_file_store_bytes < WALL_STREAM_BYTES:
        raise ValueError("hub_store_too_small")
    node_accounts = {account_id(serial): serial for serial in serials}
    accounts: dict[str, object] = {
        WALL_ACCOUNT: _wall_account(),
        SYSTEM_ACCOUNT: {"users": [_user(FLEET_SYSTEM_USER)]},
    }
    for name, serial in node_accounts.items():
        accounts[name] = _node_account(serial)
    configuration: dict[str, object] = {
        "server_name": listeners.server_name,
        "host": listeners.client_host,
        "port": listeners.client_port,
        "jetstream": {
            "domain": HUB_DOMAIN,
            "store_dir": listeners.store_dir,
            "max_file_store": listeners.max_file_store_bytes,
            "max_memory_store": 0,
        },
        "accounts": accounts,
        "system_account": SYSTEM_ACCOUNT,
        "websocket": {"host": listeners.websocket_host, "port": listeners.websocket_port, "no_tls": True},
        "leafnodes": {"host": listeners.leaf_host, "port": listeners.leaf_port},
    }
    if listeners.monitor_port is not None:
        configuration["http"] = f"{listeners.client_host}:{listeners.monitor_port}"
    return json.dumps(configuration, sort_keys=True, indent=2) + "\n"


def _user(name: str) -> dict[str, str]:
    # The password is the user name: a selector on the trusted network, not a secret.
    return {"user": name, "password": name}


def _wall_account() -> dict[str, object]:
    return {
        "jetstream": {"max_file": WALL_STREAM_BYTES, "max_mem": 0},
        "users": [_user(WALL_WRITER_USER)],
        "exports": [
            *({"service": subject} for subject in _WALL_CONSUMER_SERVICES),
            {"stream": _WALL_DELIVERY_STREAM},
            *({"service": subject} for subject in _WALL_FLOW_CONTROL_SERVICES),
        ],
    }


def _node_account(serial: str) -> dict[str, object]:
    return {
        "users": [_user(node_user(serial)), _user(central_user(serial))],
        "imports": [
            *({"service": {"account": WALL_ACCOUNT, "subject": subject},
               "to": WALL_API_PREFIX + subject[len("$JS.API"):]} for subject in _WALL_CONSUMER_SERVICES),
            {"stream": {"account": WALL_ACCOUNT, "subject": _WALL_DELIVERY_STREAM}},
            *({"service": {"account": WALL_ACCOUNT, "subject": subject}}
              for subject in _WALL_FLOW_CONTROL_SERVICES),
        ],
    }
