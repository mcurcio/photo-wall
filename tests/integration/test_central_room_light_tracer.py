"""Room light end to end, through the real composition roots: a Home Assistant state on Central's hub
and a Pi's light sensor reach the Player's room-lights callback, and never the Player's control State.

The pieces are each side's own: Central's app for House > Lights, the Home Assistant page and the
Player's control State; the worker as `media.worker` composes and runs it (`build_node_bus`,
`_run_workers`), with Central's hub session listening to Home Assistant's states and its Node bus on
a hub on free ports (bus_servers.py, configured by Fleet's generator as the worker configures it);
Home Assistant publishing its states on the hub's WebSocket listener as HOME_ASSISTANT_USER, as the
Galleria integration does (room_light_rig.HomeAssistant); a Node on the shipped configuration;
HostCore's bus as its `main()` starts it (`start_host_bus`: the host session and the wired light
sensor); and the Player as its `main()` starts it (`PlayerService.start`, which follows the room
lights), drawing on a recording renderer.

E1. A state published before the worker starts (while Central is away) reaches the callback.
E2. Ten switches reach the callback, and the Player's control State delivery does not move. Home
    Assistant's own "unavailable" keeps the last stated brightness and Central marks that it can't tell; a key that is not an entity id and a value that
    is not a state are refused (UNKNOWN_ADDRESS, UNREADABLE); the Home Assistant page shows when
    Central last heard the entity, by its name.
E3. A reading the Pi sent before its Sensor existed is that Sensor's light once the Sensor and the
    light it reports are set up, with no new reading.
E4. A change made while the hub is away (a House > Lights edit) reaches the callback once the hub is
    back; and after a hub restart with no change, a Player starting on a Node whose bus restarted
    empty gets the document (WALL holds what Central holds, whatever was put before).
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from integration.bus_servers import hub_server, node_server, until
from integration.room_light_rig import (
    RESTART_SECONDS,
    HomeAssistant,
    Player,
    Worker,
    light_sensor_root,
)
from test_registry import ADMIN, enroll, frame, hello

from appliance.host.host_runner import start_host_bus
from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.infra.home_assistant.references import light_reporter
from central.infra.light_sensor_judge import PI_SOURCE
from central.node_bus_wiring import HUB_CONFIG_ENV, HUB_URL_ENV, WALL_RECHECK_SECONDS
from central.room_light.model import MeasuredLight, ReportedLight, Sensor, SensorRef, TypedColour
from central.room_light.views import RoomLightsView
from central.room_light_routes import HOUSE_LIGHTS, HOUSE_SENSORS
from contracts.central_api import STATES_EPOCH_KEY
from contracts.home_assistant import HomeAssistantLight, HomeAssistantUnavailable
from contracts.room_light import (
    LIGHT_SENSOR_KEY,
    Dimmer,
    KelvinColour,
    Measured,
    MeasuredEntry,
    ReportedEntry,
    RoomLightsDocument,
)
from contracts.wire_model import Model
from nodeapi.buffers import state_prefix, state_stream

SERIAL = "serial-room-light"
ENTITY = "light.bookshelf_spot"
NAME = "Bookshelf spot"
ON = HomeAssistantLight(name=NAME, on=True, brightness=255, colour=None)
OFF = HomeAssistantLight(name=NAME, on=False, brightness=None, colour=None)
FULL = Dimmer(on=True, brightness=1.0)
LUX = 240.0
ADMIN_HEADERS = {"Authorization": "Bearer " + ADMIN}
FIRST_SECONDS = 30   # the worker reaches the hub, the Node links and its WALL mirror first syncs
SECONDS = WALL_RECHECK_SECONDS / 2   # every later step: a wake, never the WALL writer's own recheck


def _spot(document: RoomLightsDocument) -> Dimmer | None:
    entry = document.lights.get("bookshelf-spot")
    return entry.state if isinstance(entry, ReportedEntry) and isinstance(entry.state, Dimmer) else None


def test_room_light_reaches_the_player_through_the_real_composition(registry, tmp_path, monkeypatch):
    """Mutation probe: the listener records a light under SensorRef -> E1 never sees the spot."""
    monkeypatch.setenv("PHOTO_WALL_CACHE_ROOT", str(tmp_path / "worker-cache"))
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

    hub = hub_server(tmp_path, [SERIAL])
    house = HomeAssistant(f"ws://127.0.0.1:{hub.websocket_port}")
    node = node_server(tmp_path, SERIAL, hub)
    host = None
    stoppers: list = []
    env = {HUB_URL_ENV: hub.client_url, HUB_CONFIG_ENV: str(tmp_path / "worker-hub.json")}

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

        def latest(check, player):
            async def poll():
                document = player.latest()
                return document is not None and check(document)
            return poll

        async def run() -> None:
            # House > Lights: the spot Home Assistant reports.
            await asyncio.to_thread(call, "PUT", f"{HOUSE_LIGHTS}/bookshelf-spot", json=ReportedLight(
                name=NAME, reporter=light_reporter(ENTITY), colour=TypedColour(kelvin=4000)))
            await asyncio.to_thread(hub.start)
            await asyncio.to_thread(node.start)
            await house.__aenter__()
            stoppers.append(house.__aexit__)
            # A worker declares Home Assistant's bucket on the hub and goes away (a deploy): the state
            # published meanwhile waits there.
            first = Worker(database.dsn, env)
            first.start()

            async def epoch():
                return await house.central(STATES_EPOCH_KEY)
            await until(epoch, FIRST_SECONDS, "Central names the states epoch on its hub")
            await first.stop()
            await house.publish(ENTITY, ON)   # before the worker starts
            worker = Worker(database.dsn, env)
            worker.start()
            stoppers.append(worker.stop)
            nonlocal host
            host = start_host_bus("base-room-light", url=node.client_url, iio_root=light_sensor_root(tmp_path, LUX))
            followed = Player(tmp_path / "player", node.client_url)
            followed.start()
            stoppers.append(followed.stop)

            # E1: the retained state reaches the Player.
            await until(latest(lambda d: _spot(d) == FULL, followed), FIRST_SECONDS,
                        "the Player sees the spot on at full")

            # E2: ten switches reach the Player; its control State delivery does not move.
            before = await asyncio.to_thread(control_state)
            for index in range(10):
                on = index % 2 == 1
                await house.publish(ENTITY, ON if on else OFF)
                await until(latest(lambda d, on=on: _spot(d) is not None and _spot(d).on is on, followed), SECONDS,
                            f"switch {index + 1} reaches the Player")
            assert _spot(followed.latest()) == FULL
            assert await asyncio.to_thread(control_state) == before

            # Home Assistant's own unavailable: the spot keeps its last stated brightness and Central
            # marks that it can't tell; a key that is no entity id and a value that is no state.
            await house.publish(ENTITY, HomeAssistantUnavailable(entity_kind="light", name=NAME))

            async def cant_tell():
                view = RoomLightsView.model_validate(await asyncio.to_thread(call, "GET", HOUSE_LIGHTS))
                [spot] = [light for light in view.lights if light.id == "bookshelf-spot"]
                return spot.cant_tell_since is not None
            await until(cant_tell, SECONDS, "Central marks that the spot can't tell")
            assert _spot(followed.latest()) == FULL
            await house.publish("Not_An_Entity", ON)
            await house.publish("light.garbage", b"not a state")

            async def refused():
                answer = await asyncio.to_thread(call, "GET", "/v1/operator/house/refused-reports")
                return {(each["address"], each["reason"]) for each in answer["refused"]} >= {
                    ("Not_An_Entity", "unknown_address"), ("light.garbage", "unreadable")}
            await until(refused, SECONDS, "Central refuses the key and the value it cannot read")
            page = await asyncio.to_thread(call, "GET", "/v1/operator/integrations/home-assistant")
            [spot_entity] = [each for each in page["entities"] if each["entity"] == ENTITY]
            assert (spot_entity["name"], spot_entity["reports"]) == (NAME, "bookshelf-spot")
            assert page["heard_at"] is not None and spot_entity["heard_at"] is not None
            await house.publish(ENTITY, ON)
            await until(latest(lambda d: _spot(d) == FULL, followed), SECONDS, "the spot is back on")

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
            await asyncio.to_thread(host.meter.stop)   # no new reading from here on
            await asyncio.to_thread(call, "PUT", f"{HOUSE_SENSORS}/hall-meter", json=Sensor(
                name="Hall meter", at=SensorRef(source=PI_SOURCE, address=player["player_id"])))
            await asyncio.to_thread(call, "PUT", f"{HOUSE_LIGHTS}/window", json=MeasuredLight(
                name="Window", sensor="hall-meter", colour=TypedColour(kelvin=6500)))
            window = MeasuredEntry(colour=KelvinColour(kelvin=6500), state=Measured(lux=LUX))
            await until(latest(lambda d: d.lights.get("window") == window, followed), SECONDS,
                        "the Player sees the window at the Pi's reading")

            # E4a: a change while the hub is away (its WALL put fails) reaches the Player once it is back.
            await asyncio.to_thread(hub.stop)
            await asyncio.to_thread(call, "PUT", f"{HOUSE_LIGHTS}/bookshelf-spot", json=ReportedLight(
                name=NAME, reporter=light_reporter(ENTITY), colour=TypedColour(kelvin=3000)))

            def warmer(document):
                entry = document.lights.get("bookshelf-spot")
                return isinstance(entry, ReportedEntry) and entry.colour == KelvinColour(kelvin=3000)

            async def recorded_warmer():
                view = RoomLightsView.model_validate(await asyncio.to_thread(call, "GET", HOUSE_LIGHTS))
                [spot] = [light.light for light in view.lights if light.id == "bookshelf-spot"]
                return spot.colour == TypedColour(kelvin=3000)
            await until(recorded_warmer, SECONDS, "Central records the edit while the hub is away")
            await asyncio.to_thread(hub.start)
            await until(latest(warmer, followed), RESTART_SECONDS,
                        "the Player sees the change made while the hub was away")
            current = followed.latest()

            # E4b: a hub restart with no change, then the Node's bus restarted empty: a Player starting
            # now reads the current document from WALL.
            await asyncio.to_thread(hub.stop)
            await asyncio.to_thread(hub.start)
            await asyncio.to_thread(node.stop)
            await asyncio.to_thread(node.start)
            fresh = Player(tmp_path / "fresh-player", node.client_url)
            fresh.start()
            stoppers.append(fresh.stop)

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
            asyncio.run(main())
        finally:
            if host is not None:
                host.stop()
            node.stop()
            hub.stop()
