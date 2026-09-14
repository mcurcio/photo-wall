"""HTTP-boundary behavioural checks for the guarded operator frame routes.

PATCH /v1/operator/frames/{id} (reposition, Bead 5).

Bead 6 (B-DELETE): DELETE /v1/operator/frames/{id} removes a clear Frame and
refuses (409) while a live Run targets it (`frame_in_use`) or while it is bound
(`frame_bound`). The runtime guard lives in the ROUTE (in-memory), read exactly
as GET /v1/operator/runtime does; the binding guard is atomic in the store
transaction. Missing auth is 401; an unknown id is 404.
"""

import base64
import hashlib
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from central.app import create_app
from central.registry import (
    Enrollment,
    FrameCreate,
    OutputReport,
    enrollment_message,
)
from central.runtime import Contribution, Scene
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
    app.state.coordinator.runtime.command("activate", "live", "run-live", now)
    # Sanity: the projected runtime carries a live (body) run listing the frame,
    # read the SAME way the route reads it.
    view = app.state.coordinator.runtime.read().project(now)
    assert any(r.phase in ("body", "outro") and "frame:portrait" in r.participants
               for r in view.runs)
    with TestClient(app) as client:
        response = client.delete("/v1/operator/frames/portrait", headers=AUTH)
    assert response.status_code == 409
    assert response.json() == {"error": "frame_in_use"}
    assert [f.id for f in registry.inventory().frames] == ["portrait"]


def test_delete_of_unbound_frame_a_non_live_run_targets_is_benign(registry):
    # Benign TOCTOU (design 9a): a run that projects onto the frame by STRING but
    # is NOT live (cancelled) must NOT block the delete -- the phase filter admits
    # only body/outro -- and the resulting dangling "frame:<id>" reference must
    # not crash a later projection (runs reference frames by string, not FK).
    _portrait(registry)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    now = registry.clock.utc()
    coordinator = app.state.coordinator
    coordinator.runtime.command("set_scene", _scene_targeting("portrait", "doomed"))
    coordinator.runtime.command("activate", "doomed", "run-doomed", now)
    run_id = next(r.run_id for r in coordinator.runtime.read().project(now).runs
                  if "frame:portrait" in r.participants)
    coordinator.runtime.command("cancel", run_id, now)
    cancelled = next(r for r in coordinator.runtime.read().project(now).runs if r.run_id == run_id)
    assert cancelled.phase == "cancelled" and "frame:portrait" in cancelled.participants
    with TestClient(app) as client:
        response = client.delete("/v1/operator/frames/portrait", headers=AUTH)
        assert response.status_code == 200
        assert response.json() == {"status": "deleted"}
        # The still-dangling string reference does not crash a later projection.
        assert client.get("/v1/operator/runtime", headers=AUTH).status_code == 200
    assert [f.id for f in registry.inventory().frames] == []
