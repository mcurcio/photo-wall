"""Central relays a Node's leaf, and Home Assistant's door, to the hub (E3b design §3, §9.6 step 1, §11
rows "Wi-Fi, ingress or WAN stall", "A Node dials before its account exists"; W11; erratum E-E3D-CUT-4;
the Home Assistant system design, 2026-10-16).

A Node dials its leaf at its origin, the host and port it already boots from, at "/" + LEAF_PATH; its
nats-server appends /leafnode (erratum E-E3D-FIX-1: LEAF_PATH carries no slash at either end). Production has no HTTP router in front of Central, so Central's own app
takes that WebSocket and relays it to the hub's leaf listener: no new LAN port. Every message goes
across untouched, both ways, one at a time, until either side closes, which closes the other; a hub
that does not accept closes the Node's socket (1011) at once and the leaf redials. Nothing here
authenticates or reads the leaf: the hub admits the Node's user and enforces its lists.

Home Assistant's door (contracts/central_api.py DOOR_PATH) is the same relay to the hub's client
WebSocket: nats-server's one WebSocket listener serves leaves at /leafnode and clients at any other
path, so the door's hub URL is the leaf URL less its /leafnode (`hub_door_url`) and needs no setting
of its own. The hub admits only HOME_ASSISTANT_USER there and holds its permissions.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Final

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.uri import parse_uri

from contracts.central_api import DOOR_PATH
from contracts.node_link import LEAF_PATH

if LEAF_PATH != LEAF_PATH.strip("/") or DOOR_PATH != DOOR_PATH.strip("/"):
    raise RuntimeError("leaf_path_slashes")   # a route without its one leading "/" mounts but matches nothing
_LEAF_SUFFIX: Final = "/leafnode"   # nats-server's path for a WebSocket leaf
LEAF_ROUTE: Final = f"/{LEAF_PATH}{_LEAF_SUFFIX}"
DOOR_ROUTE: Final = f"/{DOOR_PATH}"
HUB_UNAVAILABLE: Final = 1011   # the close code a Node's leaf gets when the hub did not accept
# No cap on one message from the hub: a non-browser nats-server writes everything pending for a
# connection as ONE WebSocket frame (ns:server/websocket.go wsCollapsePtoNB), up to its max_pending
# (the hub's default, 64 MiB), so any per-frame cap under that closes the leaf on a burst (a WALL
# catch-up, a run of pull replies). The Node's frames are capped by node-bus.conf's max_pending (2 MiB),
# under uvicorn's ws_max_size (16 MiB). RAM is not a limiting factor (erratum E-E3D-S3-2).
HUB_MAX_MESSAGE: Final = None

_log = logging.getLogger(__name__)


def hub_door_url(hub_leaf_url: str) -> str:
    """The hub's client WebSocket: `hub_leaf_url` (e.g. ws://photo-wall-hub:8080/leafnode) less its
    /leafnode. ValueError("hub_leaf_url") for a URL that does not end so."""
    if not hub_leaf_url.endswith(_LEAF_SUFFIX):
        raise ValueError("hub_leaf_url")
    return hub_leaf_url[:-len(_LEAF_SUFFIX)]


def mount_bus_bridges(app: FastAPI, hub_leaf_url: str) -> None:
    """Accept a WebSocket at LEAF_ROUTE and relay it to `hub_leaf_url` (the hub's leaf WebSocket), and
    one at DOOR_ROUTE and relay it to `hub_door_url(hub_leaf_url)`, with the `websockets` client: every
    binary and text message in both directions, unchanged, until either side closes, which closes the
    other. A hub that does not accept closes the client's socket (code 1011) at once; a leaf redials.
    No authentication, no inspection, no buffering beyond one message; no compression. A malformed URL
    fails here, at boot."""
    door_url = hub_door_url(hub_leaf_url)
    parse_uri(hub_leaf_url)
    parse_uri(door_url)
    app.add_api_websocket_route(LEAF_ROUTE, _bridge(hub_leaf_url))
    app.add_api_websocket_route(DOOR_ROUTE, _bridge(door_url))


def _bridge(hub_url: str):
    """The WebSocket endpoint relaying one client to `hub_url`."""
    async def bridge(client: WebSocket) -> None:
        try:
            hub = await connect(hub_url, compression=None, max_size=HUB_MAX_MESSAGE, max_queue=1, proxy=None)
        except (OSError, TimeoutError, InvalidHandshake) as error:
            _log.debug("bus_bridge_hub_unavailable: %s", type(error).__name__)
            await client.accept()
            await client.close(code=HUB_UNAVAILABLE)
            return
        try:
            await client.accept()
            await _relay(client, hub)
        finally:
            await hub.close()
            open_ = WebSocketState.CONNECTED
            if client.client_state is open_ and client.application_state is open_:
                with suppress(OSError, WebSocketDisconnect):   # the client may leave between check and close
                    await client.close()
    return bridge


async def _relay(node: WebSocket, hub: ClientConnection) -> None:
    """Pump both directions until either ends; the other is cancelled. A pump's own error re-raises."""
    pumps = {asyncio.create_task(_node_to_hub(node, hub)), asyncio.create_task(_hub_to_node(hub, node))}
    try:
        done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for pump in pumps:
            pump.cancel()
        await asyncio.wait(pumps)
    for pump in done:
        pump.result()


async def _node_to_hub(node: WebSocket, hub: ClientConnection) -> None:
    with suppress(ConnectionClosed):
        while True:
            message = await node.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            await hub.send(data if data is not None else message["text"])


async def _hub_to_node(hub: ClientConnection, node: WebSocket) -> None:
    with suppress(ConnectionClosed, OSError, WebSocketDisconnect):
        async for data in hub:
            if isinstance(data, bytes):
                await node.send_bytes(data)
            else:
                await node.send_text(data)
