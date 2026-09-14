"""HTTP surface for PATCH /v1/operator/frames/{id} (reposition, Bead 5)."""

from fastapi.testclient import TestClient

from central.app import create_app
from central.registry import FrameCreate
from contracts.models import FrameProfile

ADMIN = "test-operator-" + "x" * 40
AUTH = {"Authorization": "Bearer " + ADMIN}


def _portrait(registry, frame_id="portrait"):
    registry.create_frame(FrameCreate(id=frame_id, width_mm=300, height_mm=500,
                                      profile=FrameProfile(width_px=1080, height_px=1920,
                                                           diagonal_inches=24)))


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
