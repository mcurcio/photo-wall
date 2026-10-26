"""House › Lights, the refused reports, each Player's drawn report and the Home Assistant settings
page through the real app (create_app on the test database): one refusal vocabulary at its status,
references whose address form the source's adapter judges, and the Player's report (from its bus
line, tests/test_drawn_report_judge.py) judged against the document now."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError

import pytest
from fastapi.testclient import TestClient
from test_registry import ADMIN, enroll

from central.app import create_app
from central.fleet.node_sessions import NodeControlConfig
from central.infra.frame_lighting_store import PgHouseLock
from central.infra.room_light_store import PgRoomLights
from central.room_light.model import (
    ColourSensorRef,
    LightStated,
    ReporterRef,
    ReportRefusal,
    SensorRef,
)
from contracts.room_light import (
    ROOM_LIGHTS_DRAWN_PATH,
    Dimmer,
    KelvinColour,
    Measured,
    RoomLightsDrawn,
    room_lights_digest,
)

AUTH = {"Authorization": "Bearer " + ADMIN}
LIGHTS = "/v1/operator/house/lights"
SENSORS = "/v1/operator/house/sensors"
COLOUR_SENSORS = "/v1/operator/house/colour-sensors"
SKY = {"name": "Sky", "at": {"source": "home-assistant", "address": "sensor.sun_sky_color_temperature"}}
WHITE = {"kind": "typed", "kelvin": 4000}


def _client(registry) -> TestClient:
    return TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False,
                                 node_control=NodeControlConfig("node-test")))


def _reported(address: str, source: str = "home-assistant") -> dict:
    return {"kind": "reported", "name": "Light", "reporter": {"source": source, "address": address}, "colour": WHITE}


def test_house_lights_refuse_with_one_vocabulary_at_its_status(registry):
    """A1. Mutation probe M6: unknown_address mapped to 409 -> the bad entity answers 409."""
    with _client(registry) as client:
        saved = client.put(f"{LIGHTS}/x", headers=AUTH, json=_reported("light.x"))
        assert saved.status_code == 200, saved.text
        assert saved.json()["entry"] == {"kind": "reported", "colour": {"kind": "kelvin", "kelvin": 4000.0}, "state": {"kind": "not-reported"}}
        assert saved.json()["light"]["reporter"] == {"source": "home-assistant", "address": "light.x"}
        for path, body, status, code in (
                (f"{LIGHTS}/y", _reported("light.x"), 409, "reference_taken"),
                (f"{LIGHTS}/y", _reported("Not An Entity"), 422, "unknown_address"),
                (f"{LIGHTS}/y", _reported("light.y", source="zigbee"), 422, "unknown_address"),
                (f"{LIGHTS}/y", {"kind": "measured", "name": "Y", "sensor": "nope", "colour": WHITE}, 404,
                 "unknown_sensor"),
                (f"{SENSORS}/m", {"name": "M", "at": {"source": "pi", "address": "not a player"}}, 422, "unknown_address")):
            answer = client.put(path, headers=AUTH, json=body)
            assert (answer.status_code, answer.json()) == (status, {"error": code}), path
        assert client.put(f"{SENSORS}/m", headers=AUTH, json={"name": "M", "at": {"source": "pi", "address": "p-1"}}
                          ).status_code == 200
        second = client.put(f"{SENSORS}/n", headers=AUTH, json={"name": "N", "at": {"source": "pi", "address": "p-1"}})
        assert (second.status_code, second.json()) == (409, {"error": "reference_taken"})   # a second Sensor on one Pi
        measured = client.put(f"{LIGHTS}/w", headers=AUTH,
                              json={"kind": "measured", "name": "W", "sensor": "m", "colour": WHITE})
        assert measured.status_code == 200, measured.text
        held = client.delete(f"{SENSORS}/m", headers=AUTH)
        assert (held.status_code, held.json()) == (409, {"error": "sensor_reports_a_light"})
        for path, code in ((f"{LIGHTS}/nope", "unknown_room_light"), (f"{SENSORS}/nope", "unknown_sensor")):
            gone = client.delete(path, headers=AUTH)
            assert (gone.status_code, gone.json()) == (404, {"error": code})
        assert client.put(f"{LIGHTS}/Bad_Id", headers=AUTH, json=_reported("light.z")).status_code == 422
        assert client.put(f"{LIGHTS}/z", headers=AUTH, json={**_reported("light.z"), "colour": {
            "kind": "typed", "kelvin": 4050}}).status_code == 422
        named = client.delete(f"{LIGHTS}/room", headers=AUTH)
        assert named.status_code == 204                       # no Frame names it here
        assert client.delete(f"{LIGHTS}/w", headers=AUTH).status_code == 204
        assert client.delete(f"{SENSORS}/m", headers=AUTH).status_code == 204
        view = client.get(LIGHTS, headers=AUTH).json()
        assert [light["id"] for light in view["lights"]] == ["x"] and view["sensors"] == []


def test_refused_reports_and_the_home_assistant_page(registry):
    lights = PgRoomLights(registry.db, PgHouseLock())
    lights.record_refused("home-assistant", "Cellar.Light", ReportRefusal.UNKNOWN_ADDRESS)
    lights.record_refused("pi", "p-1", ReportRefusal.UNREADABLE)
    lights.record_light_state(ReporterRef(source="home-assistant", address="light.heard"),
                              LightStated(state=Dimmer(on=True, brightness=1.0), colour=None))
    lights.record_heard("light.heard", "light", "Heard light")
    lights.record_reading(SensorRef(source="home-assistant", address="sensor.lux"), Measured(lux=80.0))
    lights.record_heard("sensor.lux", "illuminance", "Lux")
    with _client(registry) as client:
        assert client.put(f"{LIGHTS}/x", headers=AUTH, json=_reported("light.x")).status_code == 200
        refused = client.get("/v1/operator/house/refused-reports", headers=AUTH).json()["refused"]
        assert [(each["source"], each["address"], each["reason"]) for each in refused] == [
            ("pi", "p-1", "unreadable"), ("home-assistant", "Cellar.Light", "unknown_address")]
        page = client.get("/v1/operator/integrations/home-assistant", headers=AUTH).json()
        entities = {each["entity"]: each for each in page["entities"]}
        assert page["heard_at"] == max(each["heard_at"] for each in entities.values() if each["heard_at"])
        assert entities["light.x"]["reports"] == "x" and entities["light.x"]["heard_at"] is None
        assert (entities["light.heard"]["reports"], entities["light.heard"]["name"]) == (None, "Heard light")
        assert entities["light.heard"]["heard_at"] is not None
        assert {entity: each["kind"] for entity, each in entities.items()} == {
            "light.x": "light", "light.heard": "light", "sensor.lux": "illuminance"}   # what each reports, said
        assert [(each["source"], each["address"], each["reason"]) for each in page["refused"]] == [
            ("home-assistant", "Cellar.Light", "unknown_address")]   # one view shape, its source only


def test_the_app_builds_without_the_distribution_and_states_the_version_it_was_given(registry, monkeypatch):
    """A caller running from source (the browser leg: no photo-wall distribution installed) passes
    Central's version; building the app then reads no distribution metadata, and the Home
    Assistant page states the version given. With none given, the installed one is read."""
    def no_distribution(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr("central.version.distribution_version", no_distribution)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False,
                               node_control=NodeControlConfig("node-test"), central_version="9.8.7")) as client:
        page = client.get("/v1/operator/integrations/home-assistant", headers=AUTH).json()
    assert page["galleria"]["central_version"] == "9.8.7"
    with pytest.raises(PackageNotFoundError):
        create_app(registry.db, registry.clock, ADMIN, run_scheduler=False, node_control=NodeControlConfig("node-test"))


def test_a_players_drawn_report_is_judged_against_the_document_now(registry):
    player, _, _ = enroll(registry)
    lights = PgRoomLights(registry.db, PgHouseLock())
    now = room_lights_digest(lights.current())
    with _client(registry) as client:
        assert client.get(f"{LIGHTS}/drawn", headers=AUTH).json() == {"players": []}
        lights.record_drawn(player["player_id"], RoomLightsDrawn(drawn=now, following=True))   # as its line's judge
        [drawn] = client.get(f"{LIGHTS}/drawn", headers=AUTH).json()["players"]
        assert (drawn["player_id"], drawn["state"]) == (player["player_id"], "drawn")
        assert client.put(f"{LIGHTS}/x", headers=AUTH, json={"kind": "fixed", "name": "X", "colour": WHITE}
                          ).status_code == 200
        assert client.get(f"{LIGHTS}/drawn", headers=AUTH).json()["players"][0]["state"] == "behind"
        bearer = {"Authorization": "Bearer " + player["token"]}   # the Player's former route is gone
        assert client.put(ROOM_LIGHTS_DRAWN_PATH, headers=bearer, json={"drawn": now, "following": True}
                          ).status_code in (404, 405)


def test_colour_sensors_and_the_lights_that_follow_them(registry):
    """A colour sensor is a Home Assistant entity, never a Pi's light meter; a light follows it; it
    stays while a light follows it; the light's line and entry carry its last stated kelvin as is."""
    lights = PgRoomLights(registry.db, PgHouseLock())
    lights.record_colour_reading(ColourSensorRef(source="home-assistant", address="sensor.sun_sky_color_temperature"),
                                 KelvinColour(kelvin=3456.5))
    follows = {"kind": "fixed", "name": "Daylight", "colour": {"kind": "colour-sensor", "sensor": "sky",
                                                                "typed_kelvin": 5500}}
    with _client(registry) as client:
        unknown = client.put(f"{LIGHTS}/daylight", headers=AUTH, json=follows)
        assert (unknown.status_code, unknown.json()) == (404, {"error": "unknown_colour_sensor"})
        for body in ({"name": "Pi", "at": {"source": "pi", "address": "p-1"}},                 # a Pi's light meter
                     {"name": "Bad", "at": {"source": "home-assistant", "address": "Not An Entity"}},
                     {"name": "Zig", "at": {"source": "zigbee", "address": "sensor.k"}}):
            refused = client.put(f"{COLOUR_SENSORS}/c", headers=AUTH, json=body)
            assert (refused.status_code, refused.json()) == (422, {"error": "unknown_address"}), body
        saved = client.put(f"{COLOUR_SENSORS}/sky", headers=AUTH, json=SKY)
        assert saved.status_code == 200, saved.text
        assert (saved.json()["colour"], saved.json()["followed_by"]) == ({"kind": "kelvin", "kelvin": 3456.5}, [])
        taken = client.put(f"{COLOUR_SENSORS}/sun", headers=AUTH, json=SKY)
        assert (taken.status_code, taken.json()) == (409, {"error": "reference_taken"})
        light = client.put(f"{LIGHTS}/daylight", headers=AUTH, json=follows)
        assert light.status_code == 200, light.text
        assert light.json()["entry"]["colour"] == {"kind": "kelvin", "kelvin": 3456.5}
        assert {key: light.json()["colour"][key] for key in ("source", "sensor", "colour_from")} == {
            "source": "colour-sensor", "sensor": "sky", "colour_from": "following"}
        followed = client.delete(f"{COLOUR_SENSORS}/sky", headers=AUTH)
        assert (followed.status_code, followed.json()) == (409, {"error": "colour_sensor_followed"})
        view = client.get(LIGHTS, headers=AUTH).json()
        assert [(each["id"], each["followed_by"]) for each in view["colour_sensors"]] == [("sky", ["daylight"])]
        assert client.delete(f"{LIGHTS}/daylight", headers=AUTH).status_code == 204
        assert client.delete(f"{COLOUR_SENSORS}/sky", headers=AUTH).status_code == 204
        gone = client.delete(f"{COLOUR_SENSORS}/sky", headers=AUTH)
        assert (gone.status_code, gone.json()) == (404, {"error": "unknown_colour_sensor"})
        assert client.put(f"{COLOUR_SENSORS}/Bad_Id", headers=AUTH, json=SKY).status_code == 422
