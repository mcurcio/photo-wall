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
from central.everyday_routes import FRAME_SKIPS, NEVER_SHOW, SHOW_AGAIN, WALL_SKIPS
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
    for key in (("POST", WALL_SKIPS), ("POST", FRAME_SKIPS), ("POST", NEVER_SHOW), ("DELETE", SHOW_AGAIN)):
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
