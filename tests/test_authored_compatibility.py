"""PostgreSQL checks for Frame-aware authored media validation."""

import pytest
from psycopg.types.json import Jsonb
from support.key_content_builders import banded_grid
from support.measured_photos import measure, measure_all
from test_authored_media import RECIPE, asset, setup_source

from central.coordination import Coordinator
from central.registry import FrameCreate, RegistryError
from central.runtime import Child, Contribution, Scene
from contracts.models import FrameProfile, Variant


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
    landscape = asset(1, width=4000, height=2000)
    portrait = asset(2, width=2000, height=4000)
    repo, spec, _ = setup_source(registry, assets=(landscape, portrait))
    portrait_profile = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)
    landscape_profile = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)

    assert [c["asset_id"] for c in repo.source_candidates(
        spec.source_ref, profile=portrait_profile)["candidates"]] == [portrait.asset_id]
    # Roadmap 5a: the ratio decides both ways, so a portrait photo does not fill a landscape
    # Frame either (owner, 2026-10-11: "a photo either fills the frame or it doesn't show").
    assert [c["asset_id"] for c in repo.source_candidates(
        spec.source_ref, profile=landscape_profile)["candidates"]] == [landscape.asset_id]


def _prepared(conn, asset_id, *, state="ready", variant=None, failure=None, retry_at=0):
    """One current-recipe job for an asset, as the worker leaves it: a ready job holds a
    ready blob of `variant`; any other state holds none."""
    digest = None
    if variant is not None:
        digest = variant.sha256
        conn.execute("INSERT INTO media_blobs VALUES(%s,%s,%s,'ready',0)",
                     (digest, Jsonb(variant.model_dump(mode="json")), variant.size))
    conn.execute("INSERT INTO media_jobs(id,asset_id,recipe_id,state,retry_at,failure_code,"
                 "earliest_start,variant_sha,updated_at) VALUES(%s,%s,%s,%s,%s,%s,0,%s,0)",
                 ("job-" + asset_id[-8:], asset_id, RECIPE, state, retry_at, failure, digest))


def test_source_candidates_serve_the_planners_standing_for_the_frame(registry):
    """Pass 2 slice 3 §14 "Check this frame": each candidate carries what planning
    does with it for this Frame (planner.candidate_standing) — the console tallies it
    and restates no rule. Mutation probe: serve "usable" for every candidate."""
    usable, wrong_shape, failed, retrying, waiting = (
        asset(i, width=2160, height=3840) for i in range(1, 6))
    repo, spec, _ = setup_source(registry, assets=(usable, wrong_shape, failed, retrying, waiting))
    portrait = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)
    still = dict(size=10, media_type="image/jpeg")
    with registry.db.transaction() as conn:
        _prepared(conn, usable.asset_id,
                  variant=Variant(sha256="1" * 64, width=30, height=50, **still))
        # A still prepared as a video: no version this Frame can draw.
        _prepared(conn, wrong_shape.asset_id,
                  variant=Variant(sha256="2" * 64, width=30, height=50, size=10,
                                  media_type="video/mp4", duration=10))
        _prepared(conn, failed.asset_id, state="failed", failure="asset_missing")
        # A retry not yet due is a failure to planning until then (catalog hydration).
        _prepared(conn, retrying.asset_id, state="retry", failure="asset_missing",
                  retry_at=registry.clock.utc() + 60)

    served = repo.source_candidates(spec.source_ref, profile=portrait)["candidates"]
    assert {c["asset_id"]: c["standing"] for c in served} == {
        usable.asset_id: "usable",
        wrong_shape.asset_id: "no_compatible_variant",
        failed.asset_id: "failed_to_prepare",
        retrying.asset_id: "failed_to_prepare",
        waiting.asset_id: "preparing",
    }
    # Without a frame there is no profile to stand against.
    assert all("standing" not in c for c in repo.source_candidates(spec.source_ref)["candidates"])


def test_registry_reads_persistent_profiles_and_rejects_unknown_frames(registry):
    add_frame(registry, "left")
    profile = registry.frame_profile("left")
    assert profile.width_px == 1080 and profile.height_px == 1920
    with registry.db.transaction() as conn:
        assert registry.frame_profiles_in(conn, ("left",))["left"] == profile
    with pytest.raises(RegistryError, match="unknown_frame"):
        registry.frame_profile("missing")


def test_authored_scene_rejects_incompatible_nested_assignment_atomically(registry):
    landscape = asset(1, width=4000, height=2000)
    portrait = asset(2, width=2000, height=4000)
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
    landscape = asset(1, width=4000, height=2000)
    portrait = asset(2, width=2000, height=4000)
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
    landscape = asset(1, width=4000, height=2000)
    portrait = asset(2, width=2000, height=4000)
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
    from test_authored_media import ADMIN, client_for

    landscape = asset(1, width=4000, height=2000)
    portrait = asset(2, width=2000, height=4000)
    low_video = asset(3, width=1280, height=720, kind="video")
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


def test_the_candidates_route_lists_what_fits_and_counts_what_it_skips(registry):
    """Roadmap 5a (slice W1): with a Frame, the route lists the photos the fit rule accepts at
    the given crop limit, or that pass on their size and are not measured yet, each with its
    `fit`, and counts every skipped photo by its reason; a crop limit outside 5-60 % answers
    422. Owner, 2026-10-11: "It's about the ratio, not strict labels"."""
    from test_authored_media import ADMIN, client_for

    wide = asset(1, width=3840, height=2160)
    tall = asset(2, width=2000, height=3000)          # 2:3 on 16:9 crops 62.5 %
    banded = asset(3, width=4000, height=3000)        # interest in its top and bottom bands
    unmeasured = asset(4, width=3840, height=2160)
    small = asset(5, width=960, height=540)           # fewer pixels than the Frame
    square = asset(6, width=3000, height=3000)        # 1:1 on 16:9 crops 43.75 %
    _, spec, _ = setup_source(registry, assets=(wide, tall, banded, unmeasured, small, square))
    measure_all(registry, (wide, tall, small, square))
    measure(registry, banded, grid=banded_grid(columns=16, rows=12, top=0.2, bottom=0.2))
    add_frame(registry, "wide", portrait=False)
    with client_for(registry) as client:
        headers = {"Authorization": "Bearer " + ADMIN}
        route = f"/v1/operator/sources/{spec.source_ref}/candidates"
        answer = client.get(route, params={"frame_id": "wide"}, headers=headers).json()
        assert {c["asset_id"]: c["fit"] for c in answer["candidates"]} == {
            wide.asset_id: "fits", unmeasured.asset_id: "not_measured"}
        assert answer["count"] == 2
        assert answer["skipped"] == {"beyond_crop_limit": 2, "below_frame_pixels": 1,
                                     "face_cut": 0, "interest_cut": 1, "no_video_here": 0,
                                     "not_measured": 1}
        looser = client.get(route, params={"frame_id": "wide", "crop_limit": 0.5},
                            headers=headers).json()
        assert {c["asset_id"] for c in looser["candidates"]} == {
            wide.asset_id, unmeasured.asset_id, square.asset_id}
        assert looser["skipped"]["beyond_crop_limit"] == 1
        for outside in (0.9, 0.04):
            assert client.get(route, params={"frame_id": "wide", "crop_limit": outside},
                              headers=headers).status_code == 422
        whole = client.get(route, headers=headers).json()
        assert whole["count"] == 6 and "skipped" not in whole
