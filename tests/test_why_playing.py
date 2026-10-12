"""Why is this playing (Power and the week, A5), through GET /v1/operator/frames/{id}/why on a
test clock: at 17:30 every Frame's Why names the Schedule "Evenings", which beats the Default
Scene on priority; Show now over it at the same rank wins, and Evenings is beaten because it was
admitted earlier; at 03:00 the Default Scene plays and beats nothing."""
from __future__ import annotations

from fastapi.testclient import TestClient
from support.house_time import EVERY_DAY, LONDON, at, set_house
from test_display_identity import _frame
from test_operator_frames import ADMIN, AUTH

from central.app import create_app
from central.runtime import Contribution, Scene

FRAMES = ("hall", "study")


def _scene(scene_id: str, **settings) -> Scene:
    return Scene(scene_id=scene_id, cycle_seconds=600, contributions=tuple(
        Contribution(target=f"frame:{frame}", source_refs=("library:1",)) for frame in FRAMES), **settings)


def _why(client, frame_id):
    response = client.get(f"/v1/operator/frames/{frame_id}/why", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def _named(request):
    return (request["kind"], request["name"])


def test_why_names_the_winner_and_what_it_beat(registry):
    """Mutation probe (P-tie): compare rank only -> the Show now case names "lower-priority"."""
    set_house(registry.db, LONDON)
    for frame in FRAMES:
        _frame(registry, frame)
    registry.clock.advance(at(LONDON, "2026-10-12T12:00") - registry.clock.utc())
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    store = app.state.coordinator.runtime
    store.command("set_scene", _scene("evening", loop=True))
    store.command("set_scene", _scene("calm", loop=True))
    store.command("set_scene", _scene("party"))
    with TestClient(app) as client:
        created = client.post("/v1/operator/programs", headers=AUTH, json={
            "name": "Evenings", "scene_id": "evening", "when": {
                "days": list(EVERY_DAY), "start": {"at": "clock", "time": "17:00"},
                "end": {"at": "clock", "time": "23:00"}}})
        assert created.status_code == 201, created.text
        assert client.put("/v1/operator/default-scene", headers=AUTH, json={"scene_id": "calm"}).status_code == 204

        registry.clock.advance(at(LONDON, "2026-10-12T17:30") - registry.clock.utc())
        for frame in FRAMES:
            why = _why(client, frame)
            assert why["frame_id"] == frame and why["power"] is None          # bound to no Output
            assert _named(why["content"]["winner"]) == ("program", "Evenings")
            assert why["content"]["winner"]["program_id"] == created.json()["program_id"]
            assert [(_named(b["request"]), b["because"]) for b in why["content"]["beat"]] == [
                (("default-scene", "calm"), "lower-priority")]

        shown = client.post("/v1/operator/activations", headers=AUTH, json={
            "scene_id": "party", "activation_id": "party-now", "priority": 1})
        assert shown.json()["status"] == "admitted"
        why = _why(client, "hall")
        assert _named(why["content"]["winner"]) == ("show-now", "party")
        assert [(_named(b["request"]), b["because"]) for b in why["content"]["beat"]] == [
            (("program", "Evenings"), "admitted-earlier"), (("default-scene", "calm"), "lower-priority")]

        registry.clock.advance(at(LONDON, "2026-10-13T03:00") - registry.clock.utc())
        why = _why(client, "study")
        assert _named(why["content"]["winner"]) == ("default-scene", "calm")
        assert why["content"]["beat"] == []
        missing = client.get("/v1/operator/frames/nowhere/why", headers=AUTH)
        assert (missing.status_code, missing.json()) == (404, {"error": "unknown_frame"})
