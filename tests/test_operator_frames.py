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

from central.app import create_app
from central.displays.model import Readiness
from central.registry import (
    Enrollment,
    FrameCreate,
    OutputReport,
    enrollment_message,
)
from central.runtime import Child, Contribution, Program, Scene
from contracts.models import FrameProfile

ADMIN = "test-operator-" + "x" * 40
AUTH = {"Authorization": "Bearer " + ADMIN}


def _portrait(registry, frame_id="portrait"):
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
                         device_id=device_id, boot_id=boot_id, ticket_id=None,
                         signature=base64.b64encode(key.sign(enrollment_message(
                             nonce, outputs, device_id, boot_id, None))).decode())
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


def _landscape_profile():
    return {"width_px": 1920, "height_px": 1080, "diagonal_inches": 24, "video": True}


def _replacement_portrait_profile():
    return {"width_px": 1200, "height_px": 2000, "diagonal_inches": 24, "video": True}


def test_put_profile_preserves_frame_identity_geometry_and_invalidates_calibration(registry):
    _portrait(registry)
    # Seed the persisted state to prove the operation clears both calibration
    # validity (its Position, now at a stale generation) and an active preview, and fences in-flight calibration requests.
    with registry.db.transaction() as conn:
        conn.execute("UPDATE frames SET position_generation=generation,preview=calibration,"
                     "preview_expires=2000 WHERE id='portrait'")
    before = registry.inventory().frames[0]
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        response = client.put("/v1/operator/frames/portrait/profile",
                              json={"profile": _replacement_portrait_profile(), "expected_generation": 0},
                              headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"profile": _replacement_portrait_profile(), "generation": 1, "changed": True}
    after = registry.inventory().frames[0]
    assert (after.id, after.surface_id, after.x_mm, after.y_mm, after.width_mm, after.height_mm) == (
        before.id, before.surface_id, before.x_mm, before.y_mm, before.width_mm, before.height_mm)
    assert after.profile.model_dump() == _replacement_portrait_profile()
    assert after.readiness is Readiness.UNBOUND and after.generation == 1   # its Position (at 0) is stale
    assert after.preview is None and after.preview_expires is None
    assert after.calibration.revision == before.calibration.revision + 1
    assert after.configuration_revision == before.configuration_revision + 1
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        stale = client.post("/v1/operator/frames/portrait/calibration", headers=AUTH,
                            json={"operation": "revert", "expected_revision": before.calibration.revision,
                                  "expected_generation": 1})
    assert stale.status_code == 409
    assert stale.json() == {"error": "calibration_revision_conflict"}


def test_put_profile_guards_stale_generation_and_allows_identical_retry(registry):
    _portrait(registry)
    request = {"profile": {"width_px": 1080, "height_px": 1920,
                            "diagonal_inches": 24, "video": True}, "expected_generation": 0}
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        same = client.put("/v1/operator/frames/portrait/profile", json=request, headers=AUTH)
        stale = client.put("/v1/operator/frames/portrait/profile",
                           json={**request, "expected_generation": 1}, headers=AUTH)
    assert same.status_code == 200 and same.json()["changed"] is False
    assert stale.status_code == 409 and stale.json() == {"error": "binding_generation_conflict"}
    unchanged = registry.inventory().frames[0]
    assert unchanged.generation == 0
    assert unchanged.configuration_revision == 1


def test_put_profile_refuses_bound_live_run_bad_orientation_and_missing_auth(registry):
    identity = enroll(registry)
    _portrait(registry)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(app) as client:
        unauthed = client.put("/v1/operator/frames/portrait/profile",
                             json={"profile": _landscape_profile(), "expected_generation": 0})
        assert unauthed.status_code == 401
        registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
        bound = client.put("/v1/operator/frames/portrait/profile",
                           json={"profile": _landscape_profile(), "expected_generation": 1}, headers=AUTH)
    assert bound.status_code == 409 and bound.json() == {"error": "frame_bound"}

    # Unbound Frame with a live target is protected by the same route guard as delete.
    registry.unbind("portrait", expected_generation=1)
    now = registry.clock.utc()
    app.state.coordinator.runtime.command("set_scene", _scene_targeting("portrait", "profile-live"))
    admission = app.state.coordinator.runtime.command("activate", "profile-live", "profile-run", now)
    with TestClient(app) as client:
        live = client.put("/v1/operator/frames/portrait/profile",
                          json={"profile": _landscape_profile(), "expected_generation": 2}, headers=AUTH)
    assert live.status_code == 409 and live.json() == {"error": "frame_in_use"}

    app.state.coordinator.runtime.command("cancel", admission.run_id, now)

    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(app) as client:
        oriented = client.put("/v1/operator/frames/portrait/profile",
                              json={"profile": _landscape_profile(), "expected_generation": 2}, headers=AUTH)
    assert oriented.status_code == 422 and oriented.json() == {"error": "oriented_profile"}


def test_put_profile_unknown_frame_is_404(registry):
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        response = client.put("/v1/operator/frames/nope/profile",
                              json={"profile": _landscape_profile(), "expected_generation": 0}, headers=AUTH)
    assert response.status_code == 404 and response.json() == {"error": "unknown_frame"}


def test_profile_replacement_serializes_with_concurrent_run_activation(registry):
    _portrait(registry)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    runtime = app.state.coordinator.runtime
    runtime.command("set_scene", _scene_targeting("portrait", "profile-race"))
    entered_registry = Event()
    release_registry = Event()
    activation_started = Event()
    activation_done = Event()
    replace = app.state.registry.replace_frame_profile

    def pause_after_runtime_guard(*args, **kwargs):
        entered_registry.set()
        assert release_registry.wait(5)
        return replace(*args, **kwargs)

    app.state.registry.replace_frame_profile = pause_after_runtime_guard
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        profile_future = pool.submit(
            client.put,
            "/v1/operator/frames/portrait/profile",
            json={"profile": _replacement_portrait_profile(), "expected_generation": 0},
            headers=AUTH,
        )
        assert entered_registry.wait(5)

        def activate():
            activation_started.set()
            result = runtime.command("activate", "profile-race", "profile-race-run", registry.clock.utc())
            activation_done.set()
            return result

        activation_future = pool.submit(activate)
        assert activation_started.wait(5)
        # The profile route holds both coordination and Runtime locks while it
        # pauses between the live-Run check and the Frame write. Activation must
        # wait for that transaction to finish.
        assert not activation_done.wait(0.2)
        release_registry.set()
        response = profile_future.result(timeout=5)
        admission = activation_future.result(timeout=5)

    assert response.status_code == 200
    assert admission.status == "admitted"
    assert activation_done.is_set()
    assert registry.inventory().frames[0].generation == 1


def test_delete_clear_frame_returns_200_deleted(registry):
    _portrait(registry)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        response = client.delete("/v1/operator/frames/portrait", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"status": "deleted"}
    assert [f.id for f in registry.inventory().frames] == []


def test_delete_without_authorization_is_401(registry):
    _portrait(registry)
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
    _portrait(registry)
    registry.bind("portrait", identity["player_id"], "HDMI-A-1", expected_generation=0)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        response = client.delete("/v1/operator/frames/portrait", headers=AUTH)
    assert response.status_code == 409
    assert response.json() == {"error": "frame_bound"}
    assert [f.id for f in registry.inventory().frames] == ["portrait"]


def test_delete_frame_a_live_run_targets_is_refused_409_frame_in_use(registry):
    # (Mutation probe 2 targets the route's runtime guard.)
    _portrait(registry)
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
    _portrait(registry)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    runtime = app.state.coordinator.runtime
    runtime.command("set_scene", Scene(
        scene_id="parent", children=(Child(scene=Scene(
            scene_id="child", outro_seconds=5,
            outro_contributions=(Contribution(target="frame:portrait", asset_refs=("a",)),),
        )),),
    ))
    runtime.command("set_program", Program(
        program_id="evening", scene_id="parent", starts_at=1100, ends_at=1200,
    ))
    with TestClient(app) as client:
        response = client.delete("/v1/operator/frames/portrait", headers=AUTH)
    assert response.status_code == 409
    assert response.json() == {
        "error": "frame_referenced", "scene_ids": ["parent"],
        "program_ids": ["evening"], "queued_activation_ids": [],
    }
    assert [f.id for f in registry.inventory().frames] == ["portrait"]


def test_delete_frame_refuses_queued_snapshot_after_definition_moves_on(registry):
    _portrait(registry)
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
    _portrait(registry)
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
    _portrait(registry)
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
