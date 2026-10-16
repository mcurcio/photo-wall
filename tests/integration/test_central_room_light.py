"""Room light's tracer, Central's half (decision 0028; owner, 2026-10-15: "i want home assistant to
trigger "the spotlight turned on" or "the overhead lights turned off" and leave it to the node
renderer").

The pinned nats-server, started with its MQTT listener and JetStream, is the house's broker: it keeps
retained messages, as Home Assistant's Mosquitto does. A hub and a Node come from bus_servers.py; the
Node's `display` session reads WALL. A room light `bookshelf-spot` (Home Assistant's
`light.bookshelf_spot`, fixed 4000 K) exists and a Frame's lighting names it.

1. A retained `{"state":"on","brightness":255}` is published BEFORE the worker's link starts; the
   link starts and the Node's WALL mirror decodes a document whose `bookshelf-spot` is on at full and
   whose fixed `room` is FULL_ON.
2. Ten alternating off/on messages raise the document's change ten times while the Frame's
   configuration revision, the Player's stored configurations and its plan offers do not move (the
   control State never carries a light). The same state again changes nothing.
3. `{"state":"unavailable"}` gives Unavailable().
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import aiomqtt
import pytest
from integration.bus_servers import (
    _free_port,
    hub_server,
    line_slices,
    nats_server_binary,
    node_server,
    until,
)
from test_registry import frame

from central.infra.home_assistant import Broker, HomeAssistantLink
from central.infra.node_link_store import PgWallMarks
from central.infra.room_light_store import PgRoomLights
from central.node_bus_wiring import WALL_TABLE, RoomLightsWall
from central.room_light.model import HomeAssistantLight, RoomLight
from contracts.frame_lighting import CloseLight
from contracts.room_light import (
    FULL_ON,
    ROOM_LIGHTS_KEY,
    Dimmer,
    FixedColour,
    RoomLightsDocument,
    Unavailable,
    decode_room_lights,
)
from nodeapi.hub import WallWriter
from nodeapi.node import NodeSession, Release

SERIAL = "serial-room-light"
RELEASE = Release("1.0.0", "sha256:display-release", {"display": 1})
TOPIC = "photowall/light/light.bookshelf_spot"
SECONDS = 30      # a Node's first WALL mirror; every later step is well under a second


class _Broker:
    """The pinned nats-server as an MQTT broker (JetStream keeps the retained messages)."""

    def __init__(self, tmp: Path) -> None:
        self.port = _free_port()
        directory = tmp / "broker"
        directory.mkdir()
        self.config = directory / "broker.conf"
        self.config.write_text(f'server_name: house\nlisten: 127.0.0.1:{_free_port()}\n'
                               f'jetstream {{ store_dir: "{directory / "js"}", max_memory_store: 16MB, '
                               'max_file_store: 64MB }\n'
                               f'mqtt {{ listen: 127.0.0.1:{self.port} }}\n')
        self.log = directory / "broker.log"
        self._process: subprocess.Popen | None = None

    def start(self) -> None:
        with self.log.open("ab") as log:
            self._process = subprocess.Popen([str(nats_server_binary()), "-c", str(self.config)],
                                             stdout=log, stderr=subprocess.STDOUT)

    def stop(self) -> None:
        if self._process is not None:
            self._process.terminate()
            self._process.wait(10)

    async def publish(self, topic: str, payload: bytes) -> None:
        async def connected():
            try:
                async with aiomqtt.Client("127.0.0.1", self.port, identifier="home-assistant") as client:
                    await client.publish(topic, payload, qos=1, retain=True)
                return True
            except aiomqtt.MqttError:
                return False
        await until(connected, 10, f"the broker takes {topic}")


def _control(database) -> list:
    """What the control State is built from: each Frame's binding revisions, every stored Player
    configuration and every plan offer."""
    with database.transaction() as conn:
        return [conn.execute("SELECT id, generation, configuration_revision FROM frames ORDER BY id").fetchall(),
                conn.execute("SELECT * FROM player_configurations ORDER BY 1").fetchall(),
                conn.execute("SELECT * FROM plan_offers ORDER BY 1").fetchall()]


def test_a_retained_home_assistant_state_reaches_the_node_without_moving_the_control_state(registry, tmp_path):
    database = registry.db
    nats_server_binary()
    lights = PgRoomLights(database)
    assert lights.save_room_light(RoomLight(id="bookshelf-spot", name="Bookshelf spot",
                                            reporter=HomeAssistantLight(entity="light.bookshelf_spot"),
                                            colour=FixedColour(kelvin=4000))) is None
    frame(registry, "hall")
    spot = CloseLight(light="bookshelf-spot", lux=800.0, reference="dimmer-at-full", across_mm=0, up_mm=600,
                      out_mm=900, inner_beam_degrees=20, outer_beam_degrees=40)
    with database.transaction() as conn:
        conn.execute("UPDATE frames SET lighting = jsonb_set(lighting, '{lights}', lighting->'lights' || %s::jsonb) "
                     "WHERE id='hall'", (f"[{spot.model_dump_json()}]",))
    broker, hub = _Broker(tmp_path), hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    session = NodeSession("display", line_slices()["display"], RELEASE, url=node.client_url, reads_wall=True)

    def document() -> RoomLightsDocument | None:
        raw = session.wall.get(ROOM_LIGHTS_KEY)
        return None if raw is None else decode_room_lights(raw)

    def seen(check):
        async def poll():
            current = document()
            return current is not None and check(current)
        return poll

    async def run() -> None:
        stop = asyncio.Event()
        writers: list[WallWriter] = []
        wall = RoomLightsWall(lights, lambda: writers[0] if writers else None)
        writer = await WallWriter.connect(hub.client_url, WALL_TABLE, PgWallMarks(database, wall))
        writers.append(writer)
        await writer.ensure()
        await broker.publish(TOPIC, b'{"state":"on","brightness":255}')   # retained before the link starts
        link = HomeAssistantLink(Broker("127.0.0.1", broker.port, None, None, False), lights, wall.wake)
        running = [asyncio.create_task(wall.run(stop)), asyncio.create_task(link.run(stop))]
        try:
            # 1. The retained state reaches the Node's WALL mirror.
            await until(seen(lambda d: d.lights["bookshelf-spot"].state == Dimmer(on=True, brightness=1.0)),
                        SECONDS, "the Node sees the spot on at full")
            first = document()
            assert first.lights["room"].state == FULL_ON
            assert lights.lights().home_assistant.state == "connected"
            control = _control(database)

            # 2. Ten switches: ten changes on WALL, none in the control State; a repeat is no change.
            for index in range(10):
                await broker.publish(TOPIC, b'{"state":"off"}' if index % 2 == 0 else b'{"state":"on","brightness":255}')
                await until(seen(lambda d, n=index + 1: d.change == first.change + n), 2, f"switch {index + 1}")
            assert document().lights["bookshelf-spot"].state == Dimmer(on=True, brightness=1.0)
            await broker.publish(TOPIC, b'{"state":"on","brightness":255}')
            await broker.publish("photowall/light/light.no_such_light", b'{"state":"on"}')
            await until(lambda: asyncio.to_thread(lambda: bool(lights.refusals())), 5, "the unknown light's refusal")
            assert document().change == first.change + 10 == lights.current().change
            assert _control(database) == control

            # 3. Home Assistant cannot tell: no light from the spot.
            await broker.publish(TOPIC, b'{"state":"unavailable"}')
            await until(seen(lambda d: d.lights["bookshelf-spot"].state == Unavailable()), 2, "the spot unavailable")
        finally:
            stop.set()
            await asyncio.wait_for(asyncio.gather(*running), 10)
            await writer.close()
        assert lights.lights().home_assistant.state == "away"

    try:
        hub.start()
        node.start()
        broker.start()
        session.start()
        asyncio.run(run())
    finally:
        session.stop()
        broker.stop()
        node.stop()
        hub.stop()


@pytest.mark.parametrize("payload", [b'{"state":"dim"}', b'{"state":"on","brightness":300}', b"not json"])
def test_a_message_outside_its_shape_is_refused_and_kept(registry, payload):
    lights = PgRoomLights(registry.db)
    lights.save_room_light(RoomLight(id="bookshelf-spot", name="Bookshelf spot",
                                     reporter=HomeAssistantLight(entity="light.bookshelf_spot"),
                                     colour=FixedColour(kelvin=4000)))
    woken = []
    link = HomeAssistantLink(Broker("127.0.0.1", 1, None, None, False), lights, lambda: woken.append(1))
    before = lights.current()
    link.record(TOPIC, payload)
    assert lights.refusals()[0] == (TOPIC, "out-of-range") and lights.current() == before and woken == []
    link.record(TOPIC, b'{"state":"on","brightness":128}')
    assert woken == [1] and lights.current().lights["bookshelf-spot"].state == Dimmer(on=True, brightness=128 / 255)
