"""The Player's read-only WALL follower on real servers (nodeapi.node.WallReader, player/room_lights.py).

A hub and a Node on the shipped configurations, Central's room-lights document on the hub's WALL: a
WallReader on the Node sees it through the Node's mirror (which it creates itself, as the Player holds
no store line) and reads following; the hub stops and the reader's view still holds it; a new
WallReader started with the hub down gets it from the Node's mirror; the Node's bus stopping reads
not following, and restarting empty (its memory store) while the hub is still down leaves the first
reader's view holding the last good document and following again once the new mirror is read. A
reader on a bus where no mirror can be created never reads following. The Player's wiring
(`start_room_lights`) hands its sink each document and the reader's health.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable

from integration.bus_servers import (
    declare_wall,
    hub_server,
    node_server,
    plain_server,
    until,
    wall_writer,
)

from contracts.room_light import (
    ROOM_LIGHTS_BYTES,
    ROOM_LIGHTS_KEY,
    FixedColour,
    FixedEntry,
    RoomLightsDocument,
    decode_room_lights,
    encode_room_lights,
)
from nodeapi.buffers import WALL_PREFIX, KeyTable
from nodeapi.node import WallReader
from player.room_lights import start_room_lights

SERIAL = "serial-w"
DOCUMENT = RoomLightsDocument(change=3, lights={"room": FixedEntry(colour=FixedColour(kelvin=6500))})


async def _holds(reader: WallReader, what: str) -> None:
    async def check():
        value = reader.wall.get(ROOM_LIGHTS_KEY)
        return value is not None and decode_room_lights(value) == DOCUMENT
    await until(check, 20, what)


async def _following(readers: list[WallReader], wanted: bool, what: str) -> None:
    async def check():
        return all(reader.following() is wanted for reader in readers)
    await until(check, 20, what)


async def _publish(hub, document: RoomLightsDocument) -> None:
    walls = await wall_writer(hub)
    try:
        await declare_wall(walls, KeyTable({ROOM_LIGHTS_KEY: ROOM_LIGHTS_BYTES}))
        await walls.jetstream().publish(WALL_PREFIX + ROOM_LIGHTS_KEY, encode_room_lights(document))
    finally:
        await walls.close()


def test_a_reader_follows_across_central_away_and_a_node_bus_restart(tmp_path):
    """Mutation probe: following() always True -> it reads True while the Node's bus is down."""
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    hub.start()
    node.start()
    readers: list[WallReader] = []

    async def run():
        await _publish(hub, DOCUMENT)
        first = WallReader("player", url=node.client_url)
        readers.append(first)
        assert first.following() is False                    # before it starts
        first.start()
        await _holds(first, "the reader sees Central's document through the Node's mirror")
        await _following([first], True, "the reader follows the mirror")

        hub.stop()
        assert decode_room_lights(first.wall.get(ROOM_LIGHTS_KEY)) == DOCUMENT
        second = WallReader("player", url=node.client_url)
        readers.append(second)
        second.start()
        await _holds(second, "a reader started with the hub down reads the Node's mirror")

        node.stop()          # the Node's bus restarts empty, the hub still away
        await _following([first, second], False, "no reader follows while the Node's bus is down")
        assert decode_room_lights(first.wall.get(ROOM_LIGHTS_KEY)) == DOCUMENT   # last good stays
        node.start()
        reconnected = WallReader("player", url=node.client_url)
        readers.append(reconnected)
        reconnected.start()
        await _following([first, second, reconnected], True, "every reader follows the restarted bus's mirror")
        assert decode_room_lights(first.wall.get(ROOM_LIGHTS_KEY)) == DOCUMENT   # last good stays
        assert reconnected.wall.get(ROOM_LIGHTS_KEY) is None                     # nothing to replay

    try:
        asyncio.run(run())
    finally:
        for reader in readers:
            reader.stop(5)
        node.stop()
        hub.stop()


def test_a_reader_whose_mirror_cannot_be_created_is_not_following(tmp_path, caplog):
    """A bus that answers but holds no JetStream: the mirror's create fails at every attach, and the
    reader never reads following."""
    caplog.set_level(logging.INFO, logger="nodeapi.node")
    bus = plain_server(tmp_path)
    bus.start()
    reader = WallReader("player", url=bus.client_url)

    async def run():
        reader.start()

        async def refused_twice():
            return sum("attach" in record.getMessage() for record in caplog.records) >= 2
        await until(refused_twice, 20, "the mirror's create is refused at attach, and again")
        assert reader.following() is False

    try:
        asyncio.run(run())
    finally:
        reader.stop(5)
        bus.stop()


class _Sink:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.documents: list[RoomLightsDocument] = []
        self.following: Callable[[], bool] | None = None

    def room_lights_changed(self, document: RoomLightsDocument) -> None:
        with self._lock:
            self.documents.append(document)

    def watch_room_lights_reader(self, following: Callable[[], bool]) -> None:
        self.following = following


def test_start_room_lights_hands_the_sink_each_wall_change_and_the_readers_health(tmp_path):
    """Mutation probes: skip `watch_room_lights_reader` -> the sink never gets the reader's health;
    skip the wall's on_change -> the change never reaches `room_lights_changed`."""
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    hub.start()
    node.start()
    sink = _Sink()
    readers: list[WallReader] = []

    async def run():
        await _publish(hub, DOCUMENT)
        readers.append(start_room_lights(sink, node.client_url))
        assert sink.following is not None

        def drawn(change: int):
            async def check():
                return [document.change for document in sink.documents][-1:] == [change]
            return check

        async def follows():
            return sink.following is not None and sink.following()

        await until(drawn(3), 20, "the document reaches the sink")
        assert sink.documents == [DOCUMENT]
        await until(follows, 20, "the sink reads the reader following")

        switched = DOCUMENT.model_copy(update={"change": 4})
        await _publish(hub, switched)
        await until(drawn(4), 20, "a WALL change reaches the sink")
        assert sink.documents == [DOCUMENT, switched]

    try:
        asyncio.run(run())
    finally:
        for reader in readers:
            reader.stop(5)
        node.stop()
        hub.stop()
