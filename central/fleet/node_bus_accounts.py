"""Fleet's hub bus configuration: one account per Node, the wall-wide account and Central's own account
with Home Assistant's door (0017 C4/C5, API6; the Home Assistant system design, 2026-10-16).

Pure: no I/O and no NATS client. The output is nats-server configuration written as JSON text,
which the server's configuration parser reads, so the same set of serials always gives the same
bytes and a reload changes only what the enrolled set changed.

Each Node account holds the Node's leaf user and Central's client user, both selectors on the
trusted network rather than secrets. It has no JetStream (the Node holds its own objects), no
export and no import except the wall-wide set: this module has no form for any other. Both users
carry the leaf's subject contract (contracts.node_link, erratum E-W1-LEAF-1): the leaf user may send
toward the hub only LEAF_EXPORTS and receive only LEAF_IMPORTS, and Central may send only
LEAF_IMPORTS and subscribe only to its wildcard inboxes, so nothing a Node program does crosses the
leaf except what Central asked for.

CENTRAL_ACCOUNT (contracts/central_api.py) holds Central's state, its methods and Home Assistant's
states, and its one user, Central's own client (CENTRAL_USER, unrestricted inside its account). It
exports the services HOME_ASSISTANT_IMPORTS names, and only HOME_ASSISTANT_ACCOUNT imports them: the
door's one user (HOME_ASSISTANT_USER) lives there, publishing only those (HOME_ASSISTANT_PUBLISH) and
hearing only its inboxes (HOME_ASSISTANT_SUBSCRIBE). Every reply to it is routed back into its own account, so no reply subject
it names makes JetStream or Central write into CENTRAL_ACCOUNT.

Every user is confined to the one connection type it arrives on (`allowed_connection_types`), so a
user of one listener cannot log in on another: a Node's leaf user only as a WebSocket leaf (through
Central's origin relay), Home Assistant only as a WebSocket client (the door), and every Central and
Fleet user only on the plain client port inside the deployment.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from contracts.central_api import (
    CENTRAL_ACCOUNT,
    CENTRAL_USER,
    HOME_ASSISTANT_ACCOUNT,
    HOME_ASSISTANT_IMPORTS,
    HOME_ASSISTANT_PUBLISH,
    HOME_ASSISTANT_SUBSCRIBE,
    HOME_ASSISTANT_USER,
)
from contracts.node_link import (
    CENTRAL_SUBSCRIPTIONS,
    DOOR_CONNECTIONS,
    HUB_DOMAIN,
    LEAF_EXPORTS,
    LEAF_IMPORTS,
    LEAF_WEBSOCKET_CONNECTION,
    LOCAL_CONNECTIONS,
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
from nodeapi.hub import DOOR_CLOSES_SUBJECT

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
    max_memory_store_bytes: int  # the hub's JetStream store, in memory; at least WALL_STREAM_BYTES


def hub_configuration(serials: Iterable[str], listeners: HubListeners) -> str:
    """The hub's nats-server configuration for this set of enrolled serials, as JSON text."""
    if listeners.max_memory_store_bytes < WALL_STREAM_BYTES:
        raise ValueError("hub_store_too_small")
    node_accounts = {account_id(serial): serial for serial in serials}
    accounts: dict[str, object] = {
        WALL_ACCOUNT: _wall_account(),
        SYSTEM_ACCOUNT: {"users": [_local(FLEET_SYSTEM_USER)], "exports": [{"stream": DOOR_CLOSES_SUBJECT}]},
        CENTRAL_ACCOUNT: _central_account(),
        HOME_ASSISTANT_ACCOUNT: _home_assistant_account(),
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
        # The memory store and no store directory (E3b design §6, §12): the hub starts empty, and
        # Central rebuilds WALL from its own records (nodeapi.hub.WallWriter).
        "jetstream": {
            "domain": HUB_DOMAIN,
            "max_memory_store": listeners.max_memory_store_bytes,
            "max_file_store": 0,
        },
        "accounts": accounts,
        "system_account": SYSTEM_ACCOUNT,
        "websocket": {"host": listeners.websocket_host, "port": listeners.websocket_port, "no_tls": True},
        # No S2 on any leaf: nats-server wraps a compressed leaf's bytes, WebSocket frames and all, in
        # an S2 stream, which Central's leaf bridge (a WebSocket relay, central/fleet/leaf_bridge.py)
        # cannot carry. The accepting side's "off" turns it off whatever a Node offers (E-E3D-S3-1).
        "leafnodes": {"host": listeners.leaf_host, "port": listeners.leaf_port, "compression": "off"},
    }
    if listeners.monitor_port is not None:
        configuration["http"] = f"{listeners.client_host}:{listeners.monitor_port}"
    return json.dumps(configuration, sort_keys=True, indent=2) + "\n"


def _user(name: str, connections: tuple[str, ...]) -> dict[str, object]:
    # The password is the user name: a selector on the trusted network, not a secret. The user logs in
    # only on `connections` (nats-server's allowed_connection_types).
    return {"user": name, "password": name, "allowed_connection_types": list(connections)}


def _local(name: str) -> dict[str, object]:
    """A user of the hub's plain client port only: Central's and Fleet's clients inside the deployment."""
    return _user(name, LOCAL_CONNECTIONS)


def _permissions(publish: tuple[str, ...], subscribe: tuple[str, ...]) -> dict[str, object]:
    return {"publish": {"allow": list(publish)}, "subscribe": {"allow": list(subscribe)}}


def _wall_account() -> dict[str, object]:
    return {
        # No store limit, only "every stream has its own byte cap". An account store limit is checked
        # with the new message added, before a full stream drops its oldest, so it would refuse (10002)
        # a write the stream's discard-old policy takes. The hub's max_memory_store is the one fence:
        # the server reserves each stream's cap against it at create, never at a write (E-W1-BUF-2).
        "jetstream": {"max_bytes_required": True},
        "users": [_local(WALL_WRITER_USER)],
        "exports": [
            *({"service": subject} for subject in WALL_CONSUMER_SERVICES),
            {"stream": _WALL_DELIVERY_STREAM},
            *({"service": subject} for subject in WALL_FLOW_CONTROL_SERVICES),
        ],
    }


def _node_account(serial: str) -> dict[str, object]:
    return {
        "users": [
            {**_user(node_user(serial), (LEAF_WEBSOCKET_CONNECTION,)),
             "permissions": _permissions(LEAF_EXPORTS, LEAF_IMPORTS)},
            {**_local(central_user(serial)), "permissions": _permissions(LEAF_IMPORTS, CENTRAL_SUBSCRIPTIONS)},
        ],
        "imports": [
            *({"service": {"account": WALL_ACCOUNT, "subject": subject},
               "to": WALL_API_PREFIX + subject[len("$JS.API"):]} for subject in WALL_CONSUMER_SERVICES),
            {"stream": {"account": WALL_ACCOUNT, "subject": _WALL_DELIVERY_STREAM}},
            *({"service": {"account": WALL_ACCOUNT, "subject": subject}}
              for subject in WALL_FLOW_CONTROL_SERVICES),
        ],
    }


def _central_account() -> dict[str, object]:
    """Central on its hub: its state bucket, its methods and Home Assistant's states bucket (each
    stream has its own byte cap, as WALL's do), Central's own client on the plain port, the
    services Home Assistant's door imports: publish Home Assistant's states, read Central's state one
    key at a time, call Central's methods; and the hub's disconnect events of the door's account,
    imported from the system account (the system account exports only those), so Central hears its
    link to Home Assistant close."""
    return {
        "jetstream": {"max_bytes_required": True},
        "users": [_local(CENTRAL_USER)],
        "exports": [{"service": subject} for subject in HOME_ASSISTANT_IMPORTS],
        "imports": [{"stream": {"account": SYSTEM_ACCOUNT, "subject": DOOR_CLOSES_SUBJECT}}],
    }


def _home_assistant_account() -> dict[str, object]:
    """Home Assistant's door: its one user, only over WebSocket, publishing only the services it
    imports from CENTRAL_ACCOUNT and hearing only its inboxes. No JetStream of its own."""
    return {
        "users": [{**_user(HOME_ASSISTANT_USER, DOOR_CONNECTIONS),
                   "permissions": _permissions(HOME_ASSISTANT_PUBLISH, HOME_ASSISTANT_SUBSCRIBE)}],
        "imports": [{"service": {"account": CENTRAL_ACCOUNT, "subject": subject}, "to": to}
                    for subject, to in HOME_ASSISTANT_IMPORTS.items()],
    }
