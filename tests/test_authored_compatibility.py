"""PostgreSQL checks for Frame-aware authored media validation."""

import pytest

from central.coordination import Coordinator
from central.registry import FrameCreate, RegistryError
from central.runtime import Child, Contribution, Scene
from contracts.models import FrameProfile
from tests.test_authored_media import asset, setup_source


def add_frame(registry, frame_id: str, *, portrait: bool = True):
    width_px, height_px = (1080, 1920) if portrait else (1920, 1080)
    width_mm, height_mm = (300, 500) if portrait else (500, 300)
    return registry.create_frame(FrameCreate(
        id=frame_id, width_mm=width_mm, height_mm=height_mm,
        profile=FrameProfile(width_px=width_px, height_px=height_px, diagonal_inches=24),
    ))


def authored_scene(*contributions, scene_id="authored-scene"):
    return Scene(scene_id=scene_id, contributions=tuple(contributions))


def count_authored(registry):
    with registry.db.transaction() as conn:
        return conn.execute("SELECT count(*) AS n FROM authored_candidates").fetchone()["n"]


def test_source_candidates_filters_originals_with_the_planner_policy(registry):
    landscape = asset(1, width=2000, height=1000)
    portrait = asset(2, width=1000, height=2000)
    repo, spec, _ = setup_source(registry, assets=(landscape, portrait))
    portrait_profile = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)
    landscape_profile = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)

    assert [c["asset_id"] for c in repo.source_candidates(
        spec.source_ref, profile=portrait_profile)["candidates"]] == [portrait.asset_id]
    assert {c["asset_id"] for c in repo.source_candidates(
        spec.source_ref, profile=landscape_profile)["candidates"]} == {landscape.asset_id, portrait.asset_id}


def test_registry_reads_persistent_profiles_and_rejects_unknown_frames(registry):
    add_frame(registry, "left")
    profile = registry.frame_profile("left")
    assert profile.width_px == 1080 and profile.height_px == 1920
    with registry.db.transaction() as conn:
        assert registry.frame_profiles_in(conn, ("left",))["left"] == profile
    with pytest.raises(RegistryError, match="unknown_frame"):
        registry.frame_profile("missing")


def test_authored_scene_rejects_incompatible_nested_assignment_atomically(registry):
    landscape = asset(1, width=2000, height=1000)
    portrait = asset(2, width=1000, height=2000)
    repo, spec, originals = setup_source(registry, assets=(landscape, portrait))
    add_frame(registry, "left")
    add_frame(registry, "right")
    coordinator = Coordinator(registry.db, registry.clock)
    scene = Scene(
        scene_id="authored-scene",
        contributions=(Contribution(target="frame:left", asset_refs=(portrait.asset_id,)),),
        children=(Child(scene=authored_scene(
            Contribution(target="frame:right", asset_refs=(landscape.asset_id,)),
            scene_id="child")),),
    )

    with pytest.raises(RegistryError, match="authored_incompatible"):
        coordinator.configure_authored_scene(scene, spec.source_ref,
                                              tuple(item.asset_id for item in originals))
    assert count_authored(registry) == 0
    assert coordinator.runtime.read().export_state()["scenes"] == {}


def test_authored_scene_rejects_incompatible_outro_assignment(registry):
    landscape = asset(1, width=2000, height=1000)
    portrait = asset(2, width=1000, height=2000)
    repo, spec, originals = setup_source(registry, assets=(landscape, portrait))
    add_frame(registry, "left")
    add_frame(registry, "right")
    coordinator = Coordinator(registry.db, registry.clock)
    scene = Scene(
        scene_id="authored-scene", contributions=(Contribution(
            target="frame:left", asset_refs=(portrait.asset_id,)),),
        outro_seconds=1, outro_contributions=(Contribution(
            target="frame:right", asset_refs=(landscape.asset_id,)),),
    )

    with pytest.raises(RegistryError, match="authored_incompatible"):
        coordinator.configure_authored_scene(scene, spec.source_ref,
                                              tuple(item.asset_id for item in originals))
    assert count_authored(registry) == 0


def test_authored_scene_accepts_an_eligible_alternative_for_each_frame(registry):
    landscape = asset(1, width=2000, height=1000)
    portrait = asset(2, width=1000, height=2000)
    repo, spec, originals = setup_source(registry, assets=(landscape, portrait))
    add_frame(registry, "left")
    coordinator = Coordinator(registry.db, registry.clock)
    scene = authored_scene(Contribution(
        target="frame:left", asset_refs=(landscape.asset_id, portrait.asset_id)))

    result = coordinator.configure_authored_scene(scene, spec.source_ref,
                                                  tuple(item.asset_id for item in originals))
    assert result["created"] == 2
    assert count_authored(registry) == 2


def test_frame_filtered_api_and_atomic_save_share_original_eligibility(registry):
    from media.models import OriginalAsset
    from tests.test_authored_media import ADMIN, client_for

    landscape = asset(1, width=2000, height=1000)
    portrait = asset(2, width=1000, height=2000)
    low_video = OriginalAsset.model_validate(asset(3, width=1280, height=720).model_dump() |
                                              {"kind": "video", "duration": 5})
    _, spec, _ = setup_source(registry, assets=(landscape, portrait, low_video))
    registry.create_frame(FrameCreate(id="large", width_mm=300, height_mm=500,
        profile=FrameProfile(width_px=1080, height_px=1920, diagonal_inches=55)))
    with client_for(registry) as client:
        headers = {"Authorization": "Bearer " + ADMIN}
        route = f"/v1/operator/sources/{spec.source_ref}/candidates"
        response = client.get(route, params={"frame_id": "large"}, headers=headers)
        assert response.status_code == 200
        assert [c["asset_id"] for c in response.json()["candidates"]] == [portrait.asset_id]
        assert response.json()["candidates"][0]["variant"] is None
        assert client.get(route, params={"frame_id": "missing"}, headers=headers).status_code == 404
        assert client.get(route, params={"frame_id": "../invalid"}, headers=headers).status_code == 422
        scene = authored_scene(Contribution(target="frame:large", asset_refs=(low_video.asset_id,)))
        rejected = client.put(f"/v1/operator/scenes/{scene.scene_id}/authored", headers=headers,
            json=dict(scene=scene.model_dump(mode="json"), source_ref=spec.source_ref,
                      asset_ids=[low_video.asset_id]))
        assert rejected.status_code == 409
        assert rejected.json() == {"error": "authored_incompatible"}
    assert count_authored(registry) == 0
    assert Coordinator(registry.db, registry.clock).runtime.read().export_state()["scenes"] == {}
