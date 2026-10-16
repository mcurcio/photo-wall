"""The Player's read-only WALL follower on real servers (decision 0028; nodeapi.node.WallReader): Central
away keeps the last room-lights document.

A hub and a Node on the shipped configurations, Central's room-lights document on the hub's WALL: a
WallReader on the Node sees it through the Node's mirror (which it creates itself, as the Player holds
no store line); the hub stops and the reader's view still holds it; a new WallReader started with the
hub down gets it from the Node's mirror; and the Node's bus restarting empty (its memory store) while
the hub is still down leaves the first reader's view holding the last good document.
"""
from __future__ import annotations

import asyncio

from integration.bus_servers import declare_wall, hub_server, node_server, until, wall_writer

from contracts.room_light import (
    FULL_ON,
    ROOM_LIGHTS_BYTES,
    ROOM_LIGHTS_KEY,
    FixedColour,
    RoomLightEntry,
    RoomLightsDocument,
    decode_room_lights,
    encode_room_lights,
)
from nodeapi.buffers import WALL_PREFIX, KeyTable
from nodeapi.node import WallReader

SERIAL = "serial-w"
DOCUMENT = RoomLightsDocument(change=3, lights={"room": RoomLightEntry(FixedColour(6500), FULL_ON)})


async def _holds(reader: WallReader, what: str) -> None:
    async def check():
        value = reader.wall.get(ROOM_LIGHTS_KEY)
        return value is not None and decode_room_lights(value) == DOCUMENT
    await until(check, 20, what)


def test_central_away_keeps_the_last_room_lights(tmp_path):
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    hub.start()
    node.start()
    readers: list[WallReader] = []

    async def run():
        walls = await wall_writer(hub)
        try:
            await declare_wall(walls, KeyTable({ROOM_LIGHTS_KEY: ROOM_LIGHTS_BYTES}))
            await walls.jetstream().publish(WALL_PREFIX + ROOM_LIGHTS_KEY, encode_room_lights(DOCUMENT))
        finally:
            await walls.close()
        first = WallReader("player", url=node.client_url)
        readers.append(first)
        first.start()
        await _holds(first, "the reader sees Central's document through the Node's mirror")

        hub.stop()
        assert decode_room_lights(first.wall.get(ROOM_LIGHTS_KEY)) == DOCUMENT
        second = WallReader("player", url=node.client_url)
        readers.append(second)
        second.start()
        await _holds(second, "a reader started with the hub down reads the Node's mirror")

        node.stop()          # the Node's bus restarts empty, the hub still away
        node.start()
        reconnected = WallReader("player", url=node.client_url)
        readers.append(reconnected)
        reconnected.start()

        async def reattached():   # both readers reached the restarted bus's new mirror
            return first._attaches >= 2 and reconnected._attaches >= 1
        await until(reattached, 20, "the readers reach the restarted bus")
        assert decode_room_lights(first.wall.get(ROOM_LIGHTS_KEY)) == DOCUMENT   # last good stays
        assert reconnected.wall.get(ROOM_LIGHTS_KEY) is None                     # nothing to replay

    try:
        asyncio.run(run())
    finally:
        for reader in readers:
            reader.stop(5)
        node.stop()
        hub.stop()
