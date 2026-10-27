"""The Player on its Node's bus, on real servers: its read-only WALL follower (nodeapi.node.WallReader,
player/room_lights.py) and its own line (player/player_line.py).

A hub and a Node on the shipped configurations, Central's room-lights document on the hub's WALL: a
WallReader on the Node sees it through the Node's mirror (which it creates itself) and reads following; the hub stops and the reader's view still holds it; a new
WallReader started with the hub down gets it from the Node's mirror; the Node's bus stopping reads
not following, and restarting empty (its memory store) while the hub is still down leaves the first
reader's view holding the last good document and following again once the new mirror is read. A
reader on a bus where no mirror can be created never reads following. The Player's wiring
(`start_room_lights`) hands its sink each document and the reader's health.

The Player's line: a PlayerService started on the Node (`start_node_bus`, as `start` does) declares
its line and writes `birth`, `frames` (one report per bound Frame with its binding generation, the
light on it rising when a wash comes on), `impeded` (the Frame named while a light Central doubts
blacks it, under the grant it was drawn under, and empty once the light is on) and
`room-lights-drawn` (the drawn document's digest, put again unchanged once the repeat period passes),
each read through the Node's read-only door; after a bus crash all four are back with no help from
Central.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import threading
from collections.abc import Callable

from integration.bus_servers import (
    declare_wall,
    door,
    door_get,
    door_message,
    hub_server,
    node_server,
    plain_server,
    until,
    wall_writer,
)
from test_executor import binding
from test_player_service import close, rig

from contracts.frame_lighting import AT_FULL, FrameLighting, Paper, WashLight
from contracts.node_impeded import decode_impeded
from contracts.node_state import (
    PLAYER_SCHEMA_MAJOR,
    FrameReports,
    decode_birth,
    decode_frame_reports,
)
from contracts.room_light import (
    ROOM_LIGHTS_BYTES,
    ROOM_LIGHTS_DRAWN_REPEAT_SECONDS,
    ROOM_LIGHTS_KEY,
    CentralWords,
    Dimmer,
    FixedEntry,
    KelvinColour,
    ReportedEntry,
    RoomLightsDocument,
    RoomLightsDrawn,
    decode_room_lights,
    encode_room_lights,
    room_lights_digest,
)
from nodeapi.buffers import WALL_PREFIX, KeyTable
from nodeapi.node import WallReader
from player.room_lights import start_room_lights

SERIAL = "serial-w"
DOCUMENT = RoomLightsDocument(change=3, lights={"room": FixedEntry(colour=KelvinColour(kelvin=6500))})


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


PLAYER_STATE = "KV_state_player"
PLAYER_KEYS = "$KV.state_player."
LIGHT = "bookshelf-spot"
WASHED = FrameLighting(mat_depth_mm=0, lights=(WashLight(light=LIGHT, lux=150, reference=AT_FULL),),
                       paper=Paper(whiteness=.9, ink_density=2))


DOUBT = CentralWords(line1="Photo Wall can't read Bookshelf Spot · since 29 Oct 18:05",
                     line2="Its last word: off (26 Oct 15:35) · see Needs attention in Photo Wall")


def _wash(change: int, on: bool, doubt: CentralWords | None = None) -> RoomLightsDocument:
    return RoomLightsDocument(change=change, lights={
        LIGHT: ReportedEntry(colour=KelvinColour(kelvin=2700), state=Dimmer(on=on, brightness=1.0), doubt=doubt)})


async def _line(node) -> dict:
    """The Player line's four keys as a home-network reader gets them through the door: birth, frames,
    impeded and room-lights-drawn decoded (absent: not written), and room-lights-drawn's sequence."""
    reader = await door(node)
    try:
        held: dict = {}
        if (birth := await door_get(reader, PLAYER_STATE, PLAYER_KEYS + "birth")) is not None:
            held["birth"] = decode_birth(birth)
        if (frames := await door_get(reader, PLAYER_STATE, PLAYER_KEYS + "frames")) is not None:
            held["frames"] = decode_frame_reports(frames)
        if (impeded := await door_get(reader, PLAYER_STATE, PLAYER_KEYS + "impeded")) is not None:
            held["impeded"] = decode_impeded(impeded)
        if (drawn := await door_message(reader, PLAYER_STATE, PLAYER_KEYS + "room-lights-drawn")) is not None:
            held["drawn"] = RoomLightsDrawn.model_validate_json(base64.b64decode(drawn["data"]))
            held["drawn_seq"] = drawn["seq"]
        return held
    finally:
        await reader.close()


def _frame(held: dict):
    reports: FrameReports | None = held.get("frames")
    return reports.frames[0] if reports is not None and reports.frames else None


def test_a_started_player_writes_its_own_line_and_refills_it_after_a_bus_crash(tmp_path):
    """Mutation probes: report the Frame's generation as 1 always -> the rebound Frame reads 1; skip
    the new document's light -> light_lux never rises; stop re-putting an unchanged drawn report ->
    its sequence never moves; never put an empty `impeded` -> the lit Frame still reads impeded."""
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    hub.start()
    node.start()

    async def run():
        await _publish(hub, _wash(1, False, DOUBT))
        service, server = await rig(tmp_path, node_bus_url=node.client_url)
        hops: list[Callable[[], None]] = []
        service._room_lights_dispatch = hops.append   # the GLib hop, run here on the "main loop"

        def turn() -> None:
            while hops:
                hops.pop(0)()
            service.tick_main()

        def reads(wanted: Callable[[dict], bool]):
            async def check():
                turn()
                held = await _line(node)
                return held if wanted(held) else None
            return check

        try:
            # Bound twice: the report carries the binding generation of the second.
            server.offer(bindings=(binding().model_copy(update={"generation": 2, "lighting": WASHED}),),
                         configuration_revision=2, revision=2, layers=())
            await service.poll_state()
            service.start_node_bus()

            dark = await until(reads(lambda held: "birth" in held and "drawn" in held and _frame(held) is not None
                                     and _frame(held).light_lux is not None and held.get("impeded") is not None
                                     and held["impeded"].frames), 15,
                               "the Player's birth, its Frame drawn dark and impeded, and its drawn report, "
                               "through the door")
            birth = dark["birth"]
            assert (birth.component, birth.pipe, birth.schema_majors) == (
                "player", "show", {"player": PLAYER_SCHEMA_MAJOR})
            assert (_frame(dark).frame_id, _frame(dark).generation, _frame(dark).output_id) == ("frame1", 2, "hdmi1")
            assert _frame(dark).light_lux == 0.0
            (stopped,) = dark["impeded"].frames
            assert (stopped.frame_id, stopped.output_id, stopped.lights, stopped.words) == (
                "frame1", "hdmi1", (LIGHT,), DOUBT)
            assert stopped.grant_id == service.renderer.drawn_grant("hdmi1")

            lit = _wash(2, True)
            await _publish(hub, lit)
            held = await until(reads(lambda held: (_frame(held).light_lux or 0) > 0
                                     and held["drawn"].drawn == room_lights_digest(lit)), 15,
                               "the wash raising the light on the Frame, and the drawn report naming it")
            assert abs(_frame(held).light_lux - 150) < 1e-6 and held["drawn"].following is True
            assert held["impeded"].frames == ()                    # lit: said not impeded

            # Unchanged, the drawn report is put again once the repeat period passes.
            service.clock.advance(ROOM_LIGHTS_DRAWN_REPEAT_SECONDS)
            repeated = await until(reads(lambda again: again.get("drawn_seq", 0) > held["drawn_seq"]), 15,
                                   "the unchanged drawn report put again")
            assert repeated["drawn"] == held["drawn"]

            # The bus crashes and starts empty: the line refills itself, Central never asked.
            node.crash()
            node.start()

            async def refilled():
                try:
                    again = await _line(node)
                except Exception:   # the door is still coming back
                    return None
                return again if {"birth", "frames", "impeded", "drawn"} <= again.keys() else None
            again = await until(refilled, 15, "birth, frames, impeded and the drawn report back after the crash")
            assert again["birth"] == birth and again["frames"] == held["frames"]
            assert again["impeded"] == held["impeded"]
            assert again["drawn"] == held["drawn"]
        finally:
            service.stop()
            await close(service)

    try:
        asyncio.run(run())
    finally:
        node.stop()
        hub.stop()
