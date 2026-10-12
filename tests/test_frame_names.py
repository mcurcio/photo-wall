"""Frame names and the wall's name and room label (Power and the week, roadmap 2b; #92, #94),
through the operator API: a new Frame takes the smallest free "Frame N"; a rename stores the name;
a name another Frame has, in any case, is refused 409 name_taken; the wall's name and room are
stored and read back in the inventory, and a Frame placed on a new surface makes a new named wall.

Mutation probe (P-case): make the uniqueness case-sensitive (the index on `name`, not
`lower(name)`) -> "frame 1" is accepted.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from test_operator_frames import ADMIN, AUTH

from central.app import create_app

PROFILE = {"width_px": 1080, "height_px": 1920, "diagonal_inches": 24}


def _create(client, frame_id: str, **fields) -> dict:
    response = client.post("/v1/operator/frames", headers=AUTH, json={
        "id": frame_id, "width_mm": 300, "height_mm": 500, "profile": PROFILE, **fields})
    assert response.status_code == 201, response.text
    return response.json()


def _inventory(client) -> dict:
    return client.get("/v1/operator/inventory", headers=AUTH).json()


def test_frames_take_the_smallest_free_name_and_rename_refuses_a_taken_one(registry):
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        assert _create(client, "hall")["name"] == "Frame 1"
        assert _create(client, "study", name="Study")["name"] == "Study"
        assert _create(client, "attic")["name"] == "Frame 2"
        renamed = client.put("/v1/operator/frames/hall/name", headers=AUTH, json={"name": "Hall"})
        assert (renamed.status_code, renamed.json()) == (200, {"frame_id": "hall", "name": "Hall"})
        assert _create(client, "porch")["name"] == "Frame 1"          # the smallest free again

        for taken in ("frame 1", "STUDY", "Frame 2"):
            refused = client.put("/v1/operator/frames/hall/name", headers=AUTH, json={"name": taken})
            assert (refused.status_code, refused.json()) == (409, {"error": "name_taken"}), taken
        clash = client.post("/v1/operator/frames", headers=AUTH, json={
            "id": "cellar", "name": "hall", "width_mm": 300, "height_mm": 500, "profile": PROFILE})
        assert (clash.status_code, clash.json()) == (409, {"error": "name_taken"})
        again = client.post("/v1/operator/frames", headers=AUTH, json={
            "id": "hall", "width_mm": 300, "height_mm": 500, "profile": PROFILE})
        assert (again.status_code, again.json()) == (409, {"error": "frame_exists"})

        missing = client.put("/v1/operator/frames/nowhere/name", headers=AUTH, json={"name": "X"})
        assert (missing.status_code, missing.json()) == (404, {"error": "unknown_frame"})
        assert client.put("/v1/operator/frames/hall/name", headers=AUTH, json={"name": ""}).status_code == 422
        assert client.put("/v1/operator/frames/hall/name", headers=AUTH, json={"name": "x" * 61}).status_code == 422
        assert client.put("/v1/operator/frames/hall/name", json={"name": "Y"}).status_code == 401

        names = {frame["id"]: frame["name"] for frame in _inventory(client)["frames"]}
        assert names == {"attic": "Frame 2", "hall": "Hall", "porch": "Frame 1", "study": "Study"}


def test_the_wall_has_a_name_and_a_room_label(registry):
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        _create(client, "hall")
        assert _inventory(client)["walls"] == [{"surface_id": "wall", "name": "Wall 1", "room": ""}]
        named = client.put("/v1/operator/walls/wall", headers=AUTH, json={"name": "Gallery", "room": "Living room"})
        assert (named.status_code, named.json()) == (
            200, {"surface_id": "wall", "name": "Gallery", "room": "Living room"})
        assert _inventory(client)["walls"] == [named.json()]
        _create(client, "study", surface_id="landing")
        assert client.patch("/v1/operator/frames/hall", headers=AUTH, json={"surface_id": "stairs"}).status_code == 200
        assert _inventory(client)["walls"] == [
            {"surface_id": "landing", "name": "Wall 1", "room": ""},
            {"surface_id": "stairs", "name": "Wall 2", "room": ""},
            named.json()]
        missing = client.put("/v1/operator/walls/nowhere", headers=AUTH, json={"name": "X"})
        assert (missing.status_code, missing.json()) == (404, {"error": "unknown_wall"})
        assert client.put("/v1/operator/walls/wall", headers=AUTH, json={"name": "X", "room": "r" * 61}).status_code == 422
        assert client.put("/v1/operator/walls/wall", json={"name": "X"}).status_code == 401
