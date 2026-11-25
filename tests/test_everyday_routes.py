"""The everyday actions' routes (decision 0036, slice 3; central/everyday_routes.py) through the
real app over PostgreSQL: each change asks for `operate`, a repeated request answers what it did,
and the refusals are closed codes."""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from test_registry import ADMIN
from test_skip_never_show import Wall

from central.app import create_app
from central.everyday.policy import photo_of
from central.everyday_routes import (
    CARRY_ON,
    CARRY_ON_FRAME,
    FRAME_SKIPS,
    NEVER_SHOW,
    PAUSES,
    SHOW_AGAIN,
    WALL_SKIPS,
)
from central.home_routes import HOME

pytestmark = pytest.mark.db

HEADERS = {"Authorization": "Bearer " + ADMIN}


def _guarded(dependant, call) -> bool:
    return any(sub.call == call or _guarded(sub, call) for sub in dependant.dependencies)


def test_every_everyday_route_asks_for_operate(registry):
    """Each change asks for `operate` (an everyday action: decision 0035's permission), never
    `admin`; the list is a read. Mutation probe: guard a route with `admin`."""
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    auth = app.state.operator_auth
    routes = {(method, route.path): route for route in app.routes if isinstance(route, APIRoute)
              for method in route.methods}
    for key in (("POST", WALL_SKIPS), ("POST", FRAME_SKIPS), ("POST", NEVER_SHOW), ("DELETE", SHOW_AGAIN),
                ("POST", PAUSES), ("DELETE", CARRY_ON), ("DELETE", CARRY_ON_FRAME),
                ("POST", "/v1/operator/activations"), ("POST", "/v1/operator/runs/{run_id}/end")):
        assert _guarded(routes[key].dependant, auth.operate) and not _guarded(routes[key].dependant, auth.admin)
    assert _guarded(routes[("GET", NEVER_SHOW)].dependant, auth.admin)
    with TestClient(app) as client:
        assert client.post(WALL_SKIPS.format(surface_id="wall"), json={"request_id": "r"}).status_code in (401, 403)


def test_a_repeated_request_answers_the_stored_answer(registry):
    wall = Wall(registry, {"hall": "wall", "desk": "wall"})
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(app) as client:
        home = client.get(HOME, headers=HEADERS).json()
        assert "skip" in home["walls"][0]["can"]
        first = client.post(WALL_SKIPS.format(surface_id="wall"), headers=HEADERS,
                            json={"request_id": "press-1", "kind": "next"})
        assert first.status_code == 200, first.text
        assert {each["frame_id"] for each in first.json()["moved"]} == {"hall", "desk"}
        again = client.post(WALL_SKIPS.format(surface_id="wall"), headers=HEADERS,
                            json={"request_id": "press-1", "kind": "next"})
        assert again.json()["duplicate"] is True and again.json()["at"] == first.json()["at"]
        listed = photo_of(wall.showing("hall"))
        body = {"request_id": "never-1", "asset_id": listed, "frame_id": "hall"}
        said = client.post(NEVER_SHOW, headers=HEADERS, json=body)
        assert said.status_code == 200, said.text
        assert client.post(NEVER_SHOW, headers=HEADERS, json=body).json()["duplicate"] is True
        assert [each["asset_id"] for each in client.get(NEVER_SHOW, headers=HEADERS).json()["photos"]] == [listed]
        undo = client.delete(SHOW_AGAIN.format(asset_id=listed), headers=HEADERS)
        assert undo.json() == {"asset_id": listed, "was_listed": True}
        assert client.get(NEVER_SHOW, headers=HEADERS).json()["photos"] == []


def test_the_refusals_are_closed_codes(registry):
    Wall(registry, {"desk": "wall"}, hand_placed=("desk",))
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(app) as client:
        bad = client.post(FRAME_SKIPS.format(frame_id="desk"), headers=HEADERS, json={"kind": "sideways"})
        assert (bad.status_code, bad.json()["error"]) == (422, "invalid_operator_skip")
        missing = client.post(WALL_SKIPS.format(surface_id="nowhere"), headers=HEADERS, json={"request_id": "r"})
        assert missing.status_code == 404
        stayed = client.post(WALL_SKIPS.format(surface_id="wall"), headers=HEADERS, json={"request_id": "r"})
        assert stayed.status_code == 409
        assert stayed.json() == {"error": "nothing-moved", "stayed": [{"frame_id": "desk", "reason": "hand-placed"}]}
        unknown = client.post(NEVER_SHOW, headers=HEADERS, json={"request_id": "n", "asset_id": "asset-none"})
        assert (unknown.status_code, unknown.json()["error"]) == (404, "unknown_photo")


def test_back_to_normal_ends_the_takeover_at_once(registry):
    """S3 through the routes: a Show now on one chosen Frame with an end leaves the other Frame
    to its schedule, and Back to normal ends it at once: the next offer cuts its layer `END_LEAD`
    and its fade after the press, mid-photo, and what is beneath is the schedule's. Pause and
    Carry on answer through their routes too. Mutation probe: route `end` to `finish`."""
    from central.everyday.model import END_LEAD
    from central.everyday.policy import showing_at
    from central.runtime import Contribution, Scene, Transition
    wall = Wall(registry, {"hall": "wall", "desk": "wall"})
    wall.coordinator.runtime.command("set_scene", Scene(
        scene_id="holidays", cycle_seconds=600, loop=True, transition=Transition(seconds=2), contributions=tuple(
            Contribution(target=f"frame:{frame}", source_refs=("library:1",)) for frame in ("hall", "desk"))),
        by="console")
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(app) as client:
        refused = client.post("/v1/operator/activations", headers=HEADERS, json={
            "scene_id": "holidays", "activation_id": "nope", "frame_ids": ["attic"]})
        assert (refused.status_code, refused.json()["error"]) == (422, "frame-not-in-scene")
        started = client.post("/v1/operator/activations", headers=HEADERS, json={
            "scene_id": "holidays", "activation_id": "show-1", "priority": 2, "frame_ids": ["hall"],
            "ends": {"kind": "in", "seconds": 3600}})
        assert started.status_code == 200, started.text
        run_id = started.json()["run_id"]
        wall.tick(5)
        assert showing_at(wall.layers("hall"), registry.clock.utc()).run_id == run_id
        assert showing_at(wall.layers("desk"), registry.clock.utc()).run_id != run_id
        paused = client.post(PAUSES, headers=HEADERS, json={"request_id": "p-1", "frame_ids": ["desk"]})
        assert paused.status_code == 200, paused.text
        carried = client.delete(CARRY_ON.format(pause_id="p-1"), headers=HEADERS)
        assert carried.status_code == 200 and carried.json()["ended"] == [{"frame_id": "desk"}]
        assert client.delete(CARRY_ON.format(pause_id="p-9"), headers=HEADERS).json()["error"] == "unknown_pause"
        wall.tick(100)
        pressed = registry.clock.utc()
        ended = client.post(f"/v1/operator/runs/{run_id}/end", headers=HEADERS, json={"request_id": "back-1"})
        assert ended.status_code == 200, ended.text
        assert (ended.json()["at"], ended.json()["until"]) == (pressed + END_LEAD, pressed + END_LEAD + 2)
        wall.tick()
        (cut,) = [layer for layer in wall.layers("hall") if layer.run_id == run_id]
        assert cut.cut_at == pressed + END_LEAD + 2 and cut.end > cut.cut_at
        assert showing_at(wall.layers("hall"), cut.cut_at).run_id != run_id
