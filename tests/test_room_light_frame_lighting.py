"""House > Lights and a Frame's lighting through the real app (decision 0028; owner, 2026-10-15: "each
frame. the whole wall might share a trigger, but each configuration will be unique"; "live on the
wall").

1. House > Lights round trip: a Sensor and the room light it reports are created; removing that
   Sensor is refused (sensor-reports-a-light), a fixed light with a reported colour is refused
   (reported-colour-needs-home-assistant), a light a Frame's lighting names cannot be removed
   (named-by-a-frame); GET shows each light's state, when it was heard and who names it.
2. A Frame's lighting through Position's trial: begin with a `lighting` draft, edit it, the Player
   presents the trial's tag (the candidate hash covers the lighting), Save writes frames.lighting
   and the binding carries it; a draft naming an unknown room light, or dimmer-at-full on a
   Sensor-reported light, is refused 422 with its code; a malformed one 422 trial_lighting_invalid.
"""
from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_node_calibration import setup_trial
from test_registry import ADMIN

from central.app import create_app
from central.fleet.node_sessions import NodeControlConfig
from central.infra.room_light_store import PgRoomLights
from contracts.frame_lighting import FrameLighting
from contracts.node_calibration import candidate_hash, parse_trial

FRAME = "node-f0"
HEADERS = {"Authorization": "Bearer " + ADMIN}
LIGHTS = "/v1/operator/house/lights"
SENSORS = "/v1/operator/house/sensors"
TRIALS = f"/v1/operator/frames/{FRAME}/calibration-trials"
SPOT = {"kind": "close", "light": "bookshelf-spot", "lux": 800.0, "reference": "dimmer-at-full",
        "across_mm": 0.0, "up_mm": 600.0, "out_mm": 900.0, "inner_beam_degrees": 20.0, "outer_beam_degrees": 40.0}
PAPER = {"whiteness": 0.9, "ink_density": 2.3, "profile": None}


def _lighting(*lights: dict, depth: float = 2.0) -> dict:
    return {"mat_depth_mm": depth, "lights": list(lights), "paper": PAPER}


def _client(registry) -> TestClient:
    return TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False,
                                 node_control=NodeControlConfig("node-test")))


def _present(request, exchange, sampled: int):
    """The Node is handed the trial and reports it presented (its frame tag on the receipt)."""
    request = replace(request, request_id=uuid4(), sampled_boottime_ms=sampled)
    candidate = parse_trial(exchange(request).trial)
    exchange(replace(request, request_id=uuid4(), sampled_boottime_ms=sampled + 100, receipt=replace(
        request.receipt, sampled_boottime_ms=sampled + 100, buffer_id=f"weston-{sampled}",
        frame_tag=candidate.frame_tag)))
    return candidate


def test_house_lights_round_trip(registry):
    with _client(registry) as client:
        sensor = client.put(f"{SENSORS}/hall-meter", headers=HEADERS, json={
            "name": "Hall meter", "at": {"kind": "home-assistant", "entity": "sensor.hall_lux"}})
        assert sensor.status_code == 200, sensor.text
        window = client.put(f"{LIGHTS}/window", headers=HEADERS, json={
            "name": "Window", "reporter": {"kind": "sensor", "sensor": "hall-meter"},
            "colour": {"kind": "fixed", "kelvin": 6500}})
        assert window.status_code == 200, window.text
        assert window.json()["state"] == {"kind": "not-reported"} and window.json()["heard_at"] is None
        refused = client.delete(f"{SENSORS}/hall-meter", headers=HEADERS)
        assert (refused.status_code, refused.json()) == (409, {"error": "sensor-reports-a-light"})
        coloured = client.put(f"{LIGHTS}/lamp", headers=HEADERS, json={
            "name": "Lamp", "reporter": {"kind": "fixed"},
            "colour": {"kind": "reported", "channel_shares": [0.3, 0.4, 0.3]}})
        assert (coloured.status_code, coloured.json()) == (409, {"error": "reported-colour-needs-home-assistant"})
        assert client.put(f"{LIGHTS}/Bad_Id", headers=HEADERS, json={
            "name": "x", "reporter": {"kind": "fixed"}, "colour": {"kind": "fixed", "kelvin": 6500}}).status_code == 422

        PgRoomLights(registry.db).record_illuminance("sensor.hall_lux", _lux(240.0))
        view = client.get(LIGHTS, headers=HEADERS).json()
        lights = {light["id"]: light for light in view["lights"]}
        assert lights["window"]["state"] == {"kind": "measured", "lux": 240.0}
        assert lights["window"]["heard_at"] is not None
        assert lights["room"]["state"]["kind"] == "dimmer" and lights["room"]["heard_at"] is None
        assert view["sensors"][0]["reading"] == 240.0 and view["sensors"][0]["reports"] == "window"
        assert view["home_assistant"] == {"state": "not-configured", "since": None}

        # A light a Frame's lighting names stays until that Frame stops naming it.
        frame_registry(registry)
        named = client.delete(f"{LIGHTS}/room", headers=HEADERS)
        assert (named.status_code, named.json()) == (409, {"error": "named-by-a-frame"})
        assert {light["id"]: light for light in client.get(LIGHTS, headers=HEADERS).json()["lights"]}[
            "room"]["named_by"] == ["hall"]
        assert client.delete(f"{LIGHTS}/window", headers=HEADERS).status_code == 204
        assert client.delete(f"{SENSORS}/hall-meter", headers=HEADERS).status_code == 204


def frame_registry(registry) -> None:
    from test_registry import frame
    frame(registry, "hall")


def _lux(value: float):
    from central.room_light.model import IlluminanceMessage
    return IlluminanceMessage(value)


def test_a_frames_lighting_through_positions_trial(registry):
    """Mutation probe: check_frame_lighting passes a reference mismatch -> the window draft is begun."""
    trials, request, exchange = setup_trial(registry)
    lights = PgRoomLights(registry.db)
    from central.room_light.model import HomeAssistantLight, RoomLight, Sensor, SensorReporter
    from contracts.room_light import FixedColour
    assert lights.save_room_light(RoomLight(id="bookshelf-spot", name="Spot", colour=FixedColour(kelvin=4000),
                                            reporter=HomeAssistantLight(entity="light.bookshelf_spot"))) is None
    from central.room_light.model import HomeAssistantSensor
    assert lights.save_sensor(Sensor(id="meter", name="Meter", at=HomeAssistantSensor(entity="sensor.lux"))) is None
    assert lights.save_room_light(RoomLight(id="window", name="Window", colour=FixedColour(kelvin=6500),
                                            reporter=SensorReporter(sensor="meter"))) is None
    with _client(registry) as client:
        unknown = client.post(TRIALS, headers=HEADERS, json={"lighting": _lighting({**SPOT, "light": "cellar"})})
        assert (unknown.status_code, unknown.json()["error"]) == (422, "trial_lighting_unknown_room_light")
        mismatch = client.post(TRIALS, headers=HEADERS, json={"lighting": _lighting({**SPOT, "light": "window"})})
        assert (mismatch.status_code, mismatch.json()["error"]) == (422, "trial_lighting_reference_mismatch")
        bad = client.post(TRIALS, headers=HEADERS, json={"lighting": _lighting(depth=0.3)})
        assert (bad.status_code, bad.json()["error"]) == (422, "trial_lighting_invalid")
        assert client.post(TRIALS, headers=HEADERS, json={"mat": {"depth_mm": 0.0, "lights": []}}).status_code == 422

        begun = client.post(TRIALS, headers=HEADERS, json={"lighting": _lighting(SPOT)})
        assert begun.status_code == 200, begun.text
        trial = begun.json()
        assert FrameLighting.model_validate(trial["lighting"]) == FrameLighting.model_validate(_lighting(SPOT))
        path = f"{TRIALS}/{trial['trial_id']}"
        missing = client.post(path, headers=HEADERS, json={
            "operation": "edit", "expected_sequence": 1, "calibration": trial["calibration"]})
        assert (missing.status_code, missing.json()["error"]) == (422, "trial_lighting_required")
        draft = _lighting(SPOT, {"kind": "wash", "light": "window", "lux": 120.0, "reference": 240.0}, depth=3.0)
        edited = client.post(path, headers=HEADERS, json={
            "operation": "edit", "expected_sequence": 1, "calibration": trial["calibration"], "lighting": draft})
        assert edited.status_code == 200, edited.text

        candidate = _present(request, exchange, 1500)
        assert FrameLighting.model_validate_json(candidate.lighting_json) == FrameLighting.model_validate(draft)
        other = candidate_hash(candidate.trial_id, candidate.generation, candidate.sequence, candidate.baseline,
                               candidate.calibration_json, FrameLighting.model_validate(_lighting(SPOT)).model_dump_json())
        assert other != candidate.candidate_sha256          # the hash covers the lighting
        saved = client.post(path, headers=HEADERS, json={"operation": "save", "expected_sequence": 2})
        assert saved.status_code == 200, saved.text
        assert FrameLighting.model_validate(saved.json()["saved_lighting"]) == FrameLighting.model_validate(draft)

        frames = {frame.id: frame for frame in registry.inventory().frames}
        assert frames[FRAME].lighting == FrameLighting.model_validate(draft)
        removed = client.delete(f"{LIGHTS}/bookshelf-spot", headers=HEADERS)
        assert (removed.status_code, removed.json()) == (409, {"error": "named-by-a-frame"})


@pytest.mark.parametrize("lights", [5])
def test_more_frame_lights_than_the_cap_are_refused(registry, lights):
    setup_trial(registry)
    with _client(registry) as client:
        wash = {"kind": "wash", "light": "room", "lux": 100.0, "reference": "dimmer-at-full"}
        refused = client.post(TRIALS, headers=HEADERS, json={"lighting": _lighting(*([wash] * lights))})
        assert (refused.status_code, refused.json()["error"]) == (422, "trial_lighting_invalid")
