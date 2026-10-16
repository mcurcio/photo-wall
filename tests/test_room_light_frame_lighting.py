"""A Frame's lighting through Position's trial on the real app: begin with a draft lighting, edit it,
the Player presents the trial's tag (the candidate hash covers the lighting), Save writes
frames.lighting and the binding carries it; a draft naming an unknown room light, or a reference its
room light does not take, is refused 422 with the Frame-lighting code; a body outside its shape is
refused 422 node_trial_operation_invalid; a new Frame views a fixed room light that is there."""
from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from fastapi.testclient import TestClient
from test_node_calibration import setup_trial
from test_registry import ADMIN, frame

from central.app import create_app
from central.fleet.node_sessions import NodeControlConfig
from central.infra.room_light_store import PgRoomLights
from central.room_light.model import (
    FixedLight,
    MeasuredLight,
    ReportedLight,
    ReporterRef,
    Sensor,
    SensorRef,
)
from contracts.frame_lighting import FrameLighting
from contracts.node_calibration import candidate_hash, parse_trial
from contracts.room_light import FixedColour

FRAME = "node-f0"
HEADERS = {"Authorization": "Bearer " + ADMIN}
LIGHTS = "/v1/operator/house/lights"
TRIALS = f"/v1/operator/frames/{FRAME}/calibration-trials"
SPOT = {"kind": "close", "light": "bookshelf-spot", "lux": 800.0, "reference": {"kind": "at-full"},
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


def test_a_frames_lighting_through_positions_trial(registry):
    """Mutation probe: the check accepting a reference mismatch -> the window at-full draft is begun."""
    _, request, exchange = setup_trial(registry)
    lights = PgRoomLights(registry.db)
    lights.save_room_light("bookshelf-spot", ReportedLight(
        name="Spot", colour=FixedColour(kelvin=4000), reporter=ReporterRef(source="home-assistant", address="light.spot")))
    lights.save_sensor("meter", Sensor(name="Meter", at=SensorRef(source="home-assistant", address="sensor.lux")))
    lights.save_room_light("window", MeasuredLight(name="Window", colour=FixedColour(kelvin=6500), sensor="meter"))
    with _client(registry) as client:
        for body, code in (({"lighting": _lighting({**SPOT, "light": "cellar"})}, "unknown_room_light"),
                           ({"lighting": _lighting({**SPOT, "light": "window"})}, "reference_mismatch"),
                           ({"lighting": _lighting(depth=0.3)}, "node_trial_operation_invalid"),
                           ({"mat": {"depth_mm": 0.0, "lights": []}}, "node_trial_operation_invalid")):
            refused = client.post(TRIALS, headers=HEADERS, json=body)
            assert (refused.status_code, refused.json()["error"]) == (422, code)

        begun = client.post(TRIALS, headers=HEADERS, json={"lighting": _lighting(SPOT)})
        assert begun.status_code == 200, begun.text
        trial = begun.json()
        assert FrameLighting.model_validate(trial["lighting"]) == FrameLighting.model_validate(_lighting(SPOT))
        path = f"{TRIALS}/{trial['trial_id']}"
        missing = client.post(path, headers=HEADERS, json={
            "operation": "edit", "expected_sequence": 1, "calibration": trial["calibration"]})
        assert (missing.status_code, missing.json()["error"]) == (422, "node_trial_operation_invalid")
        stringly = client.post(path, headers=HEADERS, json={"operation": "status", "expected_sequence": "1"})
        assert (stringly.status_code, stringly.json()["error"]) == (422, "node_trial_operation_invalid")
        draft = _lighting(SPOT, {"kind": "wash", "light": "window", "lux": 120.0,
                                 "reference": {"kind": "at-reading", "lux": 240.0}}, depth=3.0)
        edited = client.post(path, headers=HEADERS, json={
            "operation": "edit", "expected_sequence": 1, "calibration": trial["calibration"], "lighting": draft})
        assert edited.status_code == 200, edited.text

        candidate = _present(request, exchange, 1500)
        assert candidate.lighting == FrameLighting.model_validate(draft)
        other = candidate_hash(candidate.trial_id, candidate.generation, candidate.sequence, candidate.baseline,
                               candidate.calibration_json, FrameLighting.model_validate(_lighting(SPOT)))
        assert other != candidate.candidate_sha256          # the hash covers the lighting
        saved = client.post(path, headers=HEADERS, json={"operation": "save", "expected_sequence": 2})
        assert saved.status_code == 200, saved.text
        assert FrameLighting.model_validate(saved.json()["saved_lighting"]) == FrameLighting.model_validate(draft)

        frames = {each.id: each for each in registry.inventory().frames}
        assert frames[FRAME].lighting == FrameLighting.model_validate(draft)
        removed = client.delete(f"{LIGHTS}/bookshelf-spot", headers=HEADERS)
        assert (removed.status_code, removed.json()) == (409, {"error": "named_by_a_frame"})
        assert client.get(LIGHTS, headers=HEADERS).json()["lights"][1]["named_by"] == [FRAME]
        remeasured = client.put(f"{LIGHTS}/window", headers=HEADERS, json={
            "kind": "fixed", "name": "Window", "colour": {"kind": "fixed", "kelvin": 6500}})
        assert (remeasured.status_code, remeasured.json()) == (409, {"error": "named_by_a_frame"})


def test_a_new_frame_names_a_fixed_room_light_that_is_there(registry):
    """A new Frame never names a room light that may be gone: the first fixed light in the house's
    order, or none. Mutation probe: insert the upgrade's lighting (naming "room") -> the bare Frame
    names a light the house no longer has."""
    lights = PgRoomLights(registry.db)
    lights.remove_room_light("room")
    frame(registry, "bare")
    lights.save_room_light("lamp", FixedLight(name="Lamp", colour=FixedColour(kelvin=3000)))
    frame(registry, "lit")
    frames = {each.id: each for each in registry.inventory().frames}
    assert frames["bare"].lighting.lights == ()
    assert [light.light for light in frames["lit"].lighting.lights] == ["lamp"]
