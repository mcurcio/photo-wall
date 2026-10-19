"""HTTP-boundary behavioural checks for the guarded operator frame routes.

PATCH /v1/operator/frames/{id} (reposition, Bead 5).

Bead 6 (B-DELETE): DELETE /v1/operator/frames/{id} removes a clear Frame and
refuses (409) while a live Run targets it (`frame_in_use`) or while it is bound
(`frame_bound`) or while stored Scenes/queued activations refer to it
(`frame_referenced`). Runtime references and deletion serialize with Scene writes;
the binding guard remains atomic in the store transaction. Missing auth is 401;
an unknown id is 404.
"""

import base64
import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from support.frames import frame_create
from support.house_time import window_program

from central.app import create_app
from central.registry import (
    Enrollment,
    FrameCreate,
    OutputReport,
    enrollment_message,
)
from central.runtime import Child, Contribution, Scene
from contracts.models import FrameProfile

ADMIN = "test-operator-" + "x" * 40
AUTH = {"Authorization": "Bearer " + ADMIN}


def _frame(registry, frame_id="portrait"):
    registry.create_frame(frame_create(frame_id))


def _portrait(registry, frame_id="portrait"):
    """A 300 x 500 mm Frame with a 1080 x 1920 profile: the wall size, profile and orientation
    cases' own subject (fb-14 changes them), so built here rather than by the test seed."""
    registry.create_frame(FrameCreate(id=frame_id, width_mm=300, height_mm=500,
                                      profile=FrameProfile(width_px=1080, height_px=1920,
                                                           diagonal_inches=24)))


def enroll(registry, count=2):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw().hex()
    device_id = "device-" + hashlib.sha256(bytes.fromhex(public)).hexdigest()
    boot_id = str(uuid.uuid4())
    nonce = registry.challenge(public)["nonce"]
    outputs = tuple(OutputReport(output_id=f"HDMI-A-{i+1}", width_px=1920, height_px=1080)
                    for i in range(count))
    request = Enrollment(public_key=public, nonce=nonce, outputs=outputs,
                         device_id=device_id, boot_id=boot_id,
                         signature=base64.b64encode(key.sign(enrollment_message(
                             nonce, outputs, device_id, boot_id))).decode())
    return registry.enroll(request)


def _scene_targeting(frame_id, scene_id):
    return Scene(
        scene_id=scene_id, loop=True, cycle_seconds=100,
        contributions=(Contribution(target=f"frame:{frame_id}", asset_refs=("a",), role="solo"),),
    )


def test_patch_reposition_merges_and_echoes(registry):
    _portrait(registry)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        response = client.patch("/v1/operator/frames/portrait", json={"x_mm": 10, "y_mm": 20},
                                headers=AUTH)
        assert response.status_code == 200
        assert response.json() == {"id": "portrait", "surface_id": "wall", "x_mm": 10,
                                   "y_mm": 20, "width_mm": 300, "height_mm": 500}


def test_patch_reposition_rejects_incoherent_merge(registry):
    _portrait(registry)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        response = client.patch("/v1/operator/frames/portrait", json={"width_mm": 600}, headers=AUTH)
        assert response.status_code == 422
        assert response.json() == {"error": "oriented_profile"}


def test_patch_reposition_unknown_frame_is_404(registry):
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        response = client.patch("/v1/operator/frames/nope", json={"x_mm": 1}, headers=AUTH)
        assert response.status_code == 404
        assert response.json() == {"error": "unknown_frame"}


def test_patch_reposition_without_authorization_is_401(registry):
    _portrait(registry)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        response = client.patch("/v1/operator/frames/portrait", json={"x_mm": 10})
        assert response.status_code == 401


def test_the_profile_route_is_gone(registry):
    """fb-13: a Frame's shape is its Position's box, so nothing replaces a Frame profile: the route
    answers as no route (404 or 405) and the Frame is unchanged."""
    _portrait(registry)
    before = registry.inventory().frames[0]
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        response = client.put("/v1/operator/frames/portrait/profile", headers=AUTH, json={
            "profile": {"width_px": 1200, "height_px": 2000, "diagonal_inches": 24, "video": True},
            "expected_generation": 0})
    assert response.status_code in (404, 405)
    assert registry.inventory().frames[0] == before


def test_delete_clear_frame_returns_200_deleted(registry):
    _frame(registry)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        response = client.delete("/v1/operator/frames/portrait", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"status": "deleted"}
    assert [f.id for f in registry.inventory().frames] == []


def test_delete_without_authorization_is_401(registry):
    _frame(registry)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        response = client.delete("/v1/operator/frames/portrait")
    assert response.status_code == 401
    # The unauthenticated request removed nothing.
    assert [f.id for f in registry.inventory().frames] == ["portrait"]


def test_delete_unknown_frame_is_404(registry):
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        response = client.delete("/v1/operator/frames/nope", headers=AUTH)
    assert response.status_code == 404
    assert response.json() == {"error": "unknown_frame"}


def test_delete_bound_frame_is_refused_409_frame_bound(registry):
    # The bindings FK on frame_id would otherwise surface a raw 500; the explicit
    # store guard returns a clean 409 instead. (Mutation probe 1 targets this.)
    identity = enroll(registry)
    _frame(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        response = client.delete("/v1/operator/frames/portrait", headers=AUTH)
    assert response.status_code == 409
    assert response.json() == {"error": "frame_bound"}
    assert [f.id for f in registry.inventory().frames] == ["portrait"]


def test_delete_frame_a_live_run_targets_is_refused_409_frame_in_use(registry):
    # (Mutation probe 2 targets the route's runtime guard.)
    _frame(registry)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    now = registry.clock.utc()  # ManualClock(1000)
    app.state.coordinator.runtime.command("set_scene", _scene_targeting("portrait", "live"))
    admission = app.state.coordinator.runtime.command("activate", "live", "run-live", now)
    # Sanity: the projected runtime carries a live (body) run listing the frame,
    # read the SAME way the route reads it.
    view = app.state.coordinator.runtime.read().project(now)
    assert any(r.phase in ("body", "outro") and "frame:portrait" in r.participants
               for r in view.runs)
    with TestClient(app) as client:
        response = client.delete("/v1/operator/frames/portrait", headers=AUTH)
    assert response.status_code == 409
    assert response.json() == {"error": "frame_in_use", "run_ids": [admission.run_id]}
    assert [f.id for f in registry.inventory().frames] == ["portrait"]


def test_delete_frame_refuses_stored_nested_outro_scene_and_names_program(registry):
    _frame(registry)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    runtime = app.state.coordinator.runtime
    runtime.command("set_scene", Scene(
        scene_id="parent", children=(Child(scene=Scene(
            scene_id="child", outro_seconds=5,
            outro_contributions=(Contribution(target="frame:portrait", asset_refs=("a",)),),
        )),),
    ))
    runtime.command_current("create_program", window_program("evening", "parent", 1140, 1200))
    with TestClient(app) as client:
        response = client.delete("/v1/operator/frames/portrait", headers=AUTH)
    assert response.status_code == 409
    assert response.json() == {
        "error": "frame_referenced", "scene_ids": ["parent"],
        "program_ids": ["evening"], "queued_activation_ids": [],
    }
    assert [f.id for f in registry.inventory().frames] == ["portrait"]


def test_delete_frame_refuses_queued_snapshot_after_definition_moves_on(registry):
    _frame(registry)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    runtime = app.state.coordinator.runtime
    now = registry.clock.utc()
    runtime.command("set_scene", _scene_targeting("elsewhere", "show"))
    runtime.command("activate", "show", "active", now)
    runtime.command("set_scene", _scene_targeting("portrait", "show").model_copy(update={"revision": 2}))
    runtime.command("activate", "show", "queued", now, repeat="queue", expires_at=now + 50)
    runtime.command("set_scene", _scene_targeting("elsewhere", "show").model_copy(update={"revision": 3}))
    with TestClient(app) as client:
        response = client.delete("/v1/operator/frames/portrait", headers=AUTH)
    assert response.status_code == 409
    assert response.json() == {
        "error": "frame_referenced", "scene_ids": [],
        "program_ids": [], "queued_activation_ids": ["queued"],
    }
    assert [f.id for f in registry.inventory().frames] == ["portrait"]


def test_scene_save_serializes_after_locked_frame_delete(registry):
    _frame(registry)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    save_started = Event()
    coordinator = app.state.coordinator
    scene = _scene_targeting("portrait", "saved-after-delete")
    with ThreadPoolExecutor(max_workers=1) as pool:
        def save_scene():
            save_started.set()
            return coordinator.runtime.command("set_scene", scene)

        with coordinator.serialized_runtime_read() as (conn, runtime):
            references = runtime.frame_references("portrait", registry.clock.utc())
            deleted = registry.delete_frame("portrait", conn=conn, references=references)
            saving = pool.submit(save_scene)
            assert save_started.wait(3)
            assert not saving.done()  # blocked behind the delete's Runtime lock
        saving.result(timeout=3)

    assert deleted == {"status": "deleted"}
    assert [frame.id for frame in registry.inventory().frames] == []
    # Forward references remain allowed, but the save is ordered after the delete.
    stored = app.state.coordinator.runtime.read().export_state()
    assert "frame:portrait" in stored["scenes"][scene.scene_id]["contributions"][0]["target"]


def test_delete_of_unbound_frame_a_non_live_run_targets_is_benign(registry):
    # Historical terminal snapshots do not block deletion. Move the current
    # definition off the Frame so only the cancelled Run retains that target.
    _frame(registry)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    now = registry.clock.utc()
    coordinator = app.state.coordinator
    coordinator.runtime.command("set_scene", _scene_targeting("portrait", "doomed"))
    coordinator.runtime.command("activate", "doomed", "run-doomed", now)
    run_id = next(r.run_id for r in coordinator.runtime.read().project(now).runs
                  if "frame:portrait" in r.participants)
    coordinator.runtime.command("cancel", run_id, now)
    coordinator.runtime.command("set_scene", Scene(scene_id="doomed", revision=2))
    cancelled = next(r for r in coordinator.runtime.read().project(now).runs if r.run_id == run_id)
    assert cancelled.phase == "cancelled" and "frame:portrait" in cancelled.participants
    with TestClient(app) as client:
        response = client.delete("/v1/operator/frames/portrait", headers=AUTH)
        assert response.status_code == 200
        assert response.json() == {"status": "deleted"}
        # The still-dangling string reference does not crash a later projection.
        assert client.get("/v1/operator/runtime", headers=AUTH).status_code == 200
    assert [f.id for f in registry.inventory().frames] == []
