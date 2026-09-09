"""PostgreSQL atomicity checks for authored Scene adoption."""

import pytest

from central.coordination import Coordinator
from central.media_repository import StoreLimits
from central.registry import RegistryError
from central.runtime import Contribution, Runtime, Scene
from tests.test_authored_media import setup_source


@pytest.fixture(autouse=True)
def persistent_frame(registry):
    from central.registry import FrameCreate
    from contracts.models import FrameProfile

    registry.create_frame(FrameCreate(id="left", width_mm=300, height_mm=500,
        profile=FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)))


def authored_scene(asset_id: str, *, source_refs=(), revision=1):
    return Scene(scene_id="authored-scene", revision=revision, contributions=(Contribution(
        target="frame:left", asset_refs=(asset_id,) if not source_refs else (),
        source_refs=source_refs),))


def count_authored(registry):
    with registry.db.transaction() as conn:
        return conn.execute("SELECT count(*) AS n FROM authored_candidates").fetchone()["n"]


def test_authored_scene_commit_is_atomic_and_idempotent(registry):
    repo, spec, originals = setup_source(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    scene = authored_scene(originals[0].asset_id)

    result = coordinator.configure_authored_scene(scene, spec.source_ref, (originals[0].asset_id,))
    assert result == {"status": "configured", "scene_id": scene.scene_id,
                      "revision": 1, "asset_refs": [originals[0].asset_id], "created": 1}
    assert coordinator.runtime.read().export_state()["scenes"][scene.scene_id] == scene.model_dump(mode="json")
    assert count_authored(registry) == 1

    retry = coordinator.configure_authored_scene(scene, spec.source_ref, (originals[0].asset_id,))
    assert retry["created"] == 0
    assert count_authored(registry) == 1


def test_runtime_failure_rolls_back_authored_rows_and_snapshot(registry, monkeypatch):
    repo, spec, originals = setup_source(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    baseline = Scene(scene_id="baseline", contributions=(Contribution(target="frame:left",
                                                                        asset_refs=("old",)),))
    coordinator.runtime.command("set_scene", baseline)
    before = coordinator.runtime.read().export_state()

    def fail_set_scene(self, scene):
        raise RuntimeError("injected runtime failure")

    monkeypatch.setattr(Runtime, "set_scene", fail_set_scene)
    with pytest.raises(RuntimeError, match="injected runtime failure"):
        coordinator.configure_authored_scene(authored_scene(originals[0].asset_id),
                                             spec.source_ref, (originals[0].asset_id,))
    assert count_authored(registry) == 0
    assert coordinator.runtime.read().export_state() == before


def test_authored_scene_requires_exact_asset_refs_and_no_live_refs(registry):
    repo, spec, originals = setup_source(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    with pytest.raises(RegistryError, match="invalid_authored_scene"):
        coordinator.configure_authored_scene(authored_scene(originals[0].asset_id), spec.source_ref, ())
    with pytest.raises(RegistryError, match="invalid_authored_scene"):
        coordinator.configure_authored_scene(
            authored_scene(originals[0].asset_id, source_refs=(spec.source_ref,)),
            spec.source_ref, (originals[0].asset_id,))
    assert count_authored(registry) == 0


def test_authored_scene_preserves_media_freshness_membership_and_capacity_checks(registry):
    repo, spec, originals = setup_source(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    with repo.transaction() as conn:
        conn.execute("UPDATE media_sources SET status='permission' WHERE source_ref=%s", (spec.source_ref,))
    with pytest.raises(RegistryError, match="source_not_fresh"):
        coordinator.configure_authored_scene(authored_scene(originals[0].asset_id),
                                             spec.source_ref, (originals[0].asset_id,))
    assert count_authored(registry) == 0

    with repo.transaction() as conn:
        conn.execute("UPDATE media_sources SET status='ok' WHERE source_ref=%s", (spec.source_ref,))
    missing = "asset-never-seen"
    with pytest.raises(RegistryError, match="authored_asset_not_found"):
        coordinator.configure_authored_scene(authored_scene(missing), spec.source_ref, (missing,))
    assert count_authored(registry) == 0

    limited = StoreLimits(max_authored_candidates=1)
    coordinator.media.limits = limited
    with pytest.raises(RegistryError, match="authored_candidate_limit"):
        coordinator.configure_authored_scene(authored_scene(originals[0].asset_id),
                                             spec.source_ref, (originals[0].asset_id,))
    assert count_authored(registry) == 0


def test_atomic_scene_api_validates_authority_and_commits_complete_request(registry):
    from tests.test_authored_media import ADMIN, client_for

    _, spec, originals = setup_source(registry)
    scene = authored_scene(originals[0].asset_id)
    body = dict(scene=scene.model_dump(mode="json"), source_ref=spec.source_ref,
                asset_ids=[originals[0].asset_id])
    route = f"/v1/operator/scenes/{scene.scene_id}/authored"
    with client_for(registry) as client:
        assert client.put(route, json=body).status_code == 401
        assert count_authored(registry) == 0
        headers = {"Authorization": "Bearer " + ADMIN}
        assert client.put(route.replace(scene.scene_id, "wrong-id"), headers=headers,
                          json=body).status_code == 422
        assert count_authored(registry) == 0
        response = client.put(route, headers=headers, json=body)
        assert response.status_code == 200
        assert response.json()["created"] == 1
        assert Coordinator(registry.db, registry.clock).runtime.read().export_state()["scenes"][scene.scene_id] == body["scene"]


def test_refresh_membership_change_serializes_before_atomic_scene(registry, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from contextlib import contextmanager
    from threading import Event

    repo, spec, originals = setup_source(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    entered = Event()
    original_edit = coordinator.runtime.edit

    @contextmanager
    def observed_edit(conn):
        with original_edit(conn) as runtime:
            entered.set()
            yield runtime

    monkeypatch.setattr(coordinator.runtime, "edit", observed_edit)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with repo.transaction() as conn:
            future = executor.submit(coordinator.configure_authored_scene,
                                     authored_scene(originals[0].asset_id),
                                     spec.source_ref, (originals[0].asset_id,))
            assert entered.wait(2), "composite operation did not enter its Runtime transaction"
            with pytest.raises(TimeoutError):
                future.result(timeout=.1)
            conn.execute("DELETE FROM source_members WHERE source_ref=%s", (spec.source_ref,))
        with pytest.raises(RegistryError, match="authored_asset_not_member"):
            future.result(timeout=3)
    assert count_authored(registry) == 0
    assert coordinator.runtime.read().export_state()["scenes"] == {}
