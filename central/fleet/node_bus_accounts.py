"""Fleet's hub bus configuration: one account per Node, plus the wall-wide account (0017 C4/C5, API6).

Pure: no I/O and no NATS client. The output is nats-server configuration written as JSON text,
which the server's configuration parser reads, so the same set of serials always gives the same
bytes and a reload changes only what the enrolled set changed.

Each Node account holds the Node's leaf user and Central's client user, both selectors on the
trusted network rather than secrets. It has no JetStream (the Node holds its own objects), no
export and no import except the wall-wide set: this module has no form for any other. Both users
carry the leaf's subject contract (contracts.node_link, errata E-W1-LEAF-1, E-W1-LEAF-2): the leaf user
may send toward the hub only LEAF_EXPORTS and receive only LEAF_IMPORTS, and Central may send only
CENTRAL_PUBLISH and subscribe only to its wildcard inboxes. What keeps a Node program from sending
anything across is the Node's own account split (node-bus.conf); these lists are the hub's half.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from contracts.node_link import (
    CENTRAL_PUBLISH,
    CENTRAL_SUBSCRIPTIONS,
    HUB_DOMAIN,
    LEAF_EXPORTS,
    LEAF_IMPORTS,
    NODE_MAX_CONTROL_LINE,
    NODE_MAX_PAYLOAD,
    WALL_ACCOUNT,
    WALL_API_PREFIX,
    WALL_CONSUMER_SERVICES,
    WALL_DELIVER_PREFIX,
    WALL_FLOW_CONTROL_SERVICES,
    WALL_STREAM_BYTES,
    WALL_WRITER_USER,
    account_id,
    central_user,
    node_user,
)

FLEET_SYSTEM_USER: Final = "fleet"
SYSTEM_ACCOUNT: Final = "SYS"

# The WALL mirror's delivery subjects, exported as a stream; its consumer API and flow control are
# the services contracts.node_link lists (WALL_CONSUMER_SERVICES, WALL_FLOW_CONTROL_SERVICES).
_WALL_DELIVERY_STREAM: Final = f"{WALL_DELIVER_PREFIX}.>"


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
    max_file_store_bytes: int    # the hub's JetStream store; at least WALL_STREAM_BYTES


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
        # The Node's max_payload, on every hub client and on the leaf listener: a message into a Node
        # account past it is refused at the hub (by Central's client before it is sent), never carried
        # across the leaf, where the Node closes the leaf for it (a maximum payload violation).
        "max_payload": NODE_MAX_PAYLOAD,
        # The Node's max_control_line (the server's default, pinned), on every hub client: a subject
        # Central writes into a Node is no longer than one a local client writes, so every stored
        # subject fits the reply envelope MAX_STORED_MESSAGE leaves (E-W1-TD-6, -8). Leaves are exempt.
        "max_control_line": NODE_MAX_CONTROL_LINE,
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


def _permissions(publish: tuple[str, ...], subscribe: tuple[str, ...]) -> dict[str, object]:
    return {"publish": {"allow": list(publish)}, "subscribe": {"allow": list(subscribe)}}


def _wall_account() -> dict[str, object]:
    return {
        # No store limit, only "every stream has its own byte cap". An account store limit is checked
        # with the new message added, before a full stream drops its oldest, so it would refuse (10002)
        # a write the stream's discard-old policy takes. The hub's max_file_store is the one fence:
        # the server reserves each stream's cap against it at create, never at a write (E-W1-BUF-2).
        "jetstream": {"max_bytes_required": True},
        "users": [_user(WALL_WRITER_USER)],
        "exports": [
            *({"service": subject} for subject in WALL_CONSUMER_SERVICES),
            {"stream": _WALL_DELIVERY_STREAM},
            *({"service": subject} for subject in WALL_FLOW_CONTROL_SERVICES),
        ],
    }


def _node_account(serial: str) -> dict[str, object]:
    return {
        "users": [
            {**_user(node_user(serial)), "permissions": _permissions(LEAF_EXPORTS, LEAF_IMPORTS)},
            {**_user(central_user(serial)), "permissions": _permissions(CENTRAL_PUBLISH, CENTRAL_SUBSCRIPTIONS)},
        ],
        "imports": [
            *({"service": {"account": WALL_ACCOUNT, "subject": subject},
               "to": WALL_API_PREFIX + subject[len("$JS.API"):]} for subject in WALL_CONSUMER_SERVICES),
            {"stream": {"account": WALL_ACCOUNT, "subject": _WALL_DELIVERY_STREAM}},
            *({"service": {"account": WALL_ACCOUNT, "subject": subject}}
              for subject in WALL_FLOW_CONTROL_SERVICES),
        ],
    }
