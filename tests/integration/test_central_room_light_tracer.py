"""Room light end to end, through the real composition roots: a retained Home Assistant message and a
Pi's light sensor reach the Player's room-lights callback, and never the Player's control State.

The pieces are each side's own: Central's app for House > Lights and the Player's control State; the
worker as `media.worker` composes and runs it (`build_bus_and_link`, `_run_workers`), with its Home
Assistant link on the pinned nats-server's MQTT listener (the house's broker) and its Node bus on a
hub on free ports (bus_servers.py, configured by Fleet's generator as the worker configures it); a
Node on the shipped configuration; the Pi's host session with the light sensor HostCore starts
(`start_light_meter`); and the Player's `follow_room_lights`, whose callback records every document
it is handed.

E1. A retained light state published before the worker starts reaches the callback.
E2. Ten switches reach the callback, and the Player's control State delivery does not move.
E3. A reading the Pi sent before its Sensor existed is that Sensor's light once the Sensor and the
    light it reports are set up, with no new reading.
E4. A change made while the hub is away reaches the callback once the hub is back; and after a hub
    restart with no change, a Player starting on a Node whose bus restarted empty gets the document
    (WALL holds what Central holds, whatever was put before).
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from integration.bus_servers import hub_server, node_server, until
from integration.room_light_rig import (
    RESTART_SECONDS,
    Broker,
    Followed,
    Worker,
    light_sensor_root,
)
from test_registry import ADMIN, enroll, frame, hello

from appliance.host.bus import host_session
from appliance.host.light_sensor import start_light_meter
from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.infra.home_assistant.link import MQTT_URL_ENV
from central.infra.home_assistant.topics import LIGHT_TOPIC_PREFIX, light_reporter
from central.infra.light_sensor_judge import PI_SOURCE
from central.node_bus_wiring import HUB_CONFIG_ENV, HUB_URL_ENV, WALL_RECHECK_SECONDS
from central.room_light.model import MeasuredLight, ReportedLight, Sensor, SensorRef
from central.room_light.views import RoomLightsView
from central.room_light_routes import HOUSE_LIGHTS, HOUSE_SENSORS
from contracts.room_light import (
    LIGHT_SENSOR_KEY,
    Dimmer,
    FixedColour,
    Measured,
    MeasuredEntry,
    RoomLightsDocument,
    WhiteEntry,
)
from contracts.wire_model import Model
from nodeapi.buffers import state_prefix, state_stream
from player.room_lights import follow_room_lights

SERIAL = "serial-room-light"
ENTITY = "light.bookshelf_spot"
TOPIC = LIGHT_TOPIC_PREFIX + ENTITY
ON, OFF = b'{"state":"on","brightness":255}', b'{"state":"off"}'
FULL = Dimmer(on=True, brightness=1.0)
LUX = 240.0
ADMIN_HEADERS = {"Authorization": "Bearer " + ADMIN}
FIRST_SECONDS = 30   # the worker reaches the hub, the Node links and its WALL mirror first syncs
SECONDS = WALL_RECHECK_SECONDS / 2   # every later step: a wake, never the WALL writer's own recheck


def _spot(document: RoomLightsDocument) -> Dimmer | None:
    entry = document.lights.get("bookshelf-spot")
    return entry.state if isinstance(entry, WhiteEntry) and isinstance(entry.state, Dimmer) else None


def test_room_light_reaches_the_player_through_the_real_composition(registry, tmp_path):
    database = registry.db
    device_id = device_id_for_serial(SERIAL)
    with database.transaction() as conn:   # the Node's device, as its boot offer records it
        conn.execute("INSERT INTO devices(device_id, serial, first_seen, last_seen) VALUES (%s, %s, 1, 1)",
                     (device_id, SERIAL))
    player, _, _ = enroll(registry, device_id=device_id)
    hello(registry, player)
    frame(registry, "hall")
    registry.bind("hall", player["player_id"], "HDMI-A-1", expected_generation=0)
    player_headers = {"Authorization": "Bearer " + player["token"]}

    broker = Broker(tmp_path)
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    host = host_session("base-room-light", url=node.client_url)
    followed = Followed()
    stoppers: list = []
    env = {HUB_URL_ENV: hub.client_url, HUB_CONFIG_ENV: str(tmp_path / "worker-hub.json"), MQTT_URL_ENV: broker.url}

    with TestClient(create_app(database, registry.clock, ADMIN)) as client:
        def call(method: str, path: str, *, json: dict | Model | None = None, headers=ADMIN_HEADERS) -> dict:
            body = json.model_dump(mode="json") if isinstance(json, Model) else json
            answer = client.request(method, path, json=body, headers=headers)
            assert answer.status_code < 300, answer.text
            return answer.json() if answer.content else {}

        def control_state() -> dict:
            """The Player's control State, as its control link reads it, and acknowledged applied."""
            state = call("GET", "/v1/player/state", headers=player_headers)
            call("POST", "/v1/player/control-acks", headers=player_headers, json={
                "authority_epoch": player["authority_epoch"], "delivery_id": state["delivery_id"],
                "result": "applied"})
            return state

        def latest(check):
            async def poll():
                document = followed.latest()
                return document is not None and check(document)
            return poll

        async def run() -> None:
            # House > Lights: the spot Home Assistant reports.
            await asyncio.to_thread(call, "PUT", f"{HOUSE_LIGHTS}/bookshelf-spot", json=ReportedLight(
                name="Bookshelf spot", reporter=light_reporter(ENTITY), colour=FixedColour(kelvin=4000)))
            await broker.publish(TOPIC, ON)   # retained, before the worker starts
            await asyncio.to_thread(hub.start)
            await asyncio.to_thread(node.start)
            worker = Worker(database.dsn, env)
            worker.start()
            stoppers.append(worker.stop)
            host.start()
            meter = start_light_meter(host.state.put, root=light_sensor_root(tmp_path, LUX))
            stoppers.append(lambda: asyncio.to_thread(meter.stop))
            reader, _ = follow_room_lights(node.client_url, followed)
            stoppers.append(lambda: asyncio.to_thread(reader.stop))

            # E1: the retained state reaches the Player.
            await until(latest(lambda d: _spot(d) == FULL), FIRST_SECONDS, "the Player sees the spot on at full")

            # E2: ten switches reach the Player; its control State delivery does not move.
            before = await asyncio.to_thread(control_state)
            for index in range(10):
                on = index % 2 == 1
                await broker.publish(TOPIC, ON if on else OFF)
                await until(latest(lambda d, on=on: _spot(d) is not None and _spot(d).on is on), SECONDS,
                            f"switch {index + 1} reaches the Player")
            assert _spot(followed.latest()) == FULL
            assert await asyncio.to_thread(control_state) == before

            # E3: the Pi's reading, recorded before its Sensor exists, is the Sensor's light once set up.
            async def reading_recorded():
                def read() -> bool:
                    with database.transaction() as conn:
                        return conn.execute(
                            "SELECT 1 FROM node_link_records WHERE device_id=%s AND stream=%s AND subject=%s",
                            (device_id, state_stream("host"), state_prefix("host") + LIGHT_SENSOR_KEY),
                        ).fetchone() is not None
                return await asyncio.to_thread(read)
            await until(reading_recorded, SECONDS, "Central records the Pi's light reading")
            await asyncio.to_thread(meter.stop)   # no new reading from here on
            await asyncio.to_thread(call, "PUT", f"{HOUSE_SENSORS}/hall-meter", json=Sensor(
                name="Hall meter", at=SensorRef(source=PI_SOURCE, address=player["player_id"])))
            await asyncio.to_thread(call, "PUT", f"{HOUSE_LIGHTS}/window", json=MeasuredLight(
                name="Window", sensor="hall-meter", colour=FixedColour(kelvin=6500)))
            window = MeasuredEntry(colour=FixedColour(kelvin=6500), state=Measured(lux=LUX))
            await until(latest(lambda d: d.lights.get("window") == window), SECONDS,
                        "the Player sees the window at the Pi's reading")

            # E4a: a change while the hub is away (its WALL put fails) reaches the Player once it is back.
            await asyncio.to_thread(hub.stop)
            await broker.publish(TOPIC, OFF)

            async def recorded_off():
                view = RoomLightsView.model_validate(await asyncio.to_thread(call, "GET", HOUSE_LIGHTS))
                [spot] = [light.entry for light in view.lights if light.id == "bookshelf-spot"]
                return isinstance(spot, WhiteEntry) and isinstance(spot.state, Dimmer) and not spot.state.on
            await until(recorded_off, SECONDS, "Central records the spot off while the hub is away")
            await asyncio.to_thread(hub.start)
            await until(latest(lambda d: _spot(d) is not None and not _spot(d).on), RESTART_SECONDS,
                        "the Player sees the change made while the hub was away")
            current = followed.latest()

            # E4b: a hub restart with no change, then the Node's bus restarted empty: a Player starting
            # now reads the current document from WALL.
            await asyncio.to_thread(hub.stop)
            await asyncio.to_thread(hub.start)
            await asyncio.to_thread(node.stop)
            await asyncio.to_thread(node.start)
            fresh = Followed()
            again, _ = follow_room_lights(node.client_url, fresh)
            stoppers.append(lambda: asyncio.to_thread(again.stop))

            async def fresh_current():
                return fresh.latest() is not None and fresh.latest().lights == current.lights
            await until(fresh_current, RESTART_SECONDS, "a Player starting after both restarts reads the document")
            assert worker.running()

        async def main() -> None:
            try:
                await run()
            finally:
                for stop in reversed(stoppers):
                    await stop()

        try:
            broker.start()
            asyncio.run(main())
        finally:
            host.stop()
            node.stop()
            hub.stop()
            broker.stop()
