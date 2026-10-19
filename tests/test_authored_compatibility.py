"""PostgreSQL checks for Frame-aware authored media validation."""

import pytest
from support.frame_media import seed_variant
from support.frame_outputs import target_of
from support.key_content_builders import banded_grid
from support.measured_photos import measure, measure_all
from test_authored_media import asset, setup_source
from test_registry import enroll

from central.coordination import Coordinator
from central.db import ProcessTransactionClock
from central.kernel.job_types import PrepareMedia
from central.media_repository import MediaRepository, prepare_lock
from central.registry import FrameCreate, RegistryError
from central.runtime import Child, Contribution, Scene
from central.wall_plan.fit_settings import FitSettings
from contracts.models import FrameProfile, Variant

HOUSE = FitSettings()


PORTRAIT = target_of(1080, 1920, inches=24)
LANDSCAPE = target_of(1920, 1080, inches=24)


def add_frame(registry, frame_id: str, *, portrait: bool = True, bound: bool = True,
              diagonal_inches: float = 24) -> None:
    """A Frame, by default bound full screen to an Output of its own pixels (its Frame output:
    frame box run fb-8, a Frame with no known screen has no shape and takes no photo)."""
    width_px, height_px = (1080, 1920) if portrait else (1920, 1080)
    width_mm, height_mm = (300, 500) if portrait else (500, 300)
    registry.create_frame(FrameCreate(
        id=frame_id, width_mm=width_mm, height_mm=height_mm,
        profile=FrameProfile(width_px=width_px, height_px=height_px,
                             diagonal_inches=diagonal_inches),
    ))
    if bound:
        player, _, _ = enroll(registry, count=1, size=(width_px, height_px))
        registry.bind(frame_id, player["player_id"], "HDMI-A-1", expected_generation=0)


def authored_scene(*contributions, scene_id="authored-scene"):
    return Scene(scene_id=scene_id, contributions=tuple(contributions))


def count_authored(registry):
    with registry.db.transaction() as conn:
        return conn.execute("SELECT count(*) AS n FROM authored_candidates").fetchone()["n"]


def test_source_candidates_filters_originals_with_the_planner_policy(registry):
    landscape = asset(1, width=4000, height=2000)
    portrait = asset(2, width=2000, height=4000)
    repo, spec, _ = setup_source(registry, assets=(landscape, portrait))
    assert [c["asset_id"] for c in repo.source_candidates(
        spec.source_ref, target=PORTRAIT, settings=HOUSE)["candidates"]] == [
        portrait.asset_id]
    # Roadmap 5a: the ratio decides both ways, so a portrait photo does not fill a landscape
    # Frame either (owner, 2026-10-11: "a photo either fills the frame or it doesn't show").
    assert [c["asset_id"] for c in repo.source_candidates(
        spec.source_ref, target=LANDSCAPE, settings=HOUSE)["candidates"]] == [
        landscape.asset_id]


def _prepared(registry, asset_id, *, variant=None, failure=None, retry_not_before=None):
    """One photo's preparation under the wall's recipe, as the worker leaves it: a made
    variant's record, or the latest `PrepareMedia` outcome of a failure (terminal, or transient
    until `retry_not_before`)."""
    if variant is not None:
        seed_variant(registry, asset_id, variant.sha256, size=variant.size,
                     width=variant.width, height=variant.height, media_type=variant.media_type,
                     duration=variant.duration)
        return
    repository = MediaRepository(registry.db, registry.clock,
                                 times=ProcessTransactionClock(registry.clock))
    with registry.db.transaction() as conn:
        key = repository.variant_key_in(conn, asset_id)
        conn.execute("INSERT INTO job_outcomes(lock_key,job_name,status,reason,retry_not_before,"
                     "seq,updated_at) VALUES(%s,%s,%s,%s,%s,nextval('job_outcome_seq'),0)",
                     (prepare_lock(key), PrepareMedia.job_name,
                      "terminal" if retry_not_before is None else "transient", failure,
                      retry_not_before))


def test_source_candidates_serve_the_planners_standing_for_the_frame(registry):
    """Pass 2 slice 3 §14 "Check this frame": each candidate carries what planning
    does with it for this Frame (planner.candidate_standing) — the console tallies it
    and restates no rule. Mutation probe: serve "usable" for every candidate."""
    usable, wrong_shape, failed, retrying, waiting = (
        asset(i, width=2160, height=3840) for i in range(1, 6))
    repo, spec, _ = setup_source(registry, assets=(usable, wrong_shape, failed, retrying, waiting))
    still = dict(size=10, media_type="image/jpeg")
    add_frame(registry, "need")   # the wall's need names the recipe
    _prepared(registry, usable.asset_id,
              variant=Variant(asset_id="asset-1", recipe_id="e" * 64, sha256="1" * 64, width=30, height=50, **still))
    # A still prepared as a video: no version this Frame can draw.
    _prepared(registry, wrong_shape.asset_id,
              variant=Variant(asset_id="asset-2", recipe_id="e" * 64, sha256="2" * 64, width=30, height=50, size=10,
                              media_type="video/mp4", duration=10))
    _prepared(registry, failed.asset_id, failure="asset_missing")
    # A retry not yet due is a failure to planning until then (catalog hydration).
    _prepared(registry, retrying.asset_id, failure="asset_missing",
              retry_not_before=registry.clock.utc() + 60)

    served = repo.source_candidates(spec.source_ref, target=PORTRAIT,
                                    settings=HOUSE)["candidates"]
    assert {c["asset_id"]: c["standing"] for c in served} == {
        usable.asset_id: "usable",
        wrong_shape.asset_id: "no_compatible_variant",
        failed.asset_id: "failed_to_prepare",
        retrying.asset_id: "failed_to_prepare",
        waiting.asset_id: "preparing",
    }
    # Without a frame there is no profile to stand against.
    assert all("standing" not in c
               for c in repo.source_candidates(spec.source_ref, settings=HOUSE)["candidates"])


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
    add_frame(registry, "large", diagonal_inches=55)   # its Display states no size: large
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
    422. Owner, 2026-10-11: "It's about the ratio, not strict labels". Follow-ups B1: the
    Frame's pixels are its Frame output (here full screen on the enrolled 1920 x 1080 Output);
    before it is bound it has none, so it has no shape and nothing is judged for it (frame box
    run fb-8: "frame_size_unknown")."""
    from support.position import save_position
    from test_authored_media import ADMIN, client_for
    from test_registry import enroll

    wide = asset(1, width=3840, height=2160)
    tall = asset(2, width=2000, height=3000)          # 2:3 on 16:9 crops 62.5 %
    banded = asset(3, width=4000, height=3000)        # interest in its top and bottom bands
    unmeasured = asset(4, width=3840, height=2160)
    small = asset(5, width=960, height=540)           # fewer pixels than the Frame
    square = asset(6, width=3000, height=3000)        # 1:1 on 16:9 crops 43.75 %
    _, spec, _ = setup_source(registry, assets=(wide, tall, banded, unmeasured, small, square))
    measure_all(registry, (wide, tall, small, square))
    measure(registry, banded, grid=banded_grid(columns=16, rows=12, top=0.2, bottom=0.2))
    add_frame(registry, "wide", portrait=False, bound=False)
    with client_for(registry) as client:
        headers = {"Authorization": "Bearer " + ADMIN}
        route = f"/v1/operator/sources/{spec.source_ref}/candidates"
        unbound = client.get(route, params={"frame_id": "wide"}, headers=headers)
        assert (unbound.status_code, unbound.json()) == (409, {"error": "frame_size_unknown"})
        player, _, _ = enroll(registry)
        registry.bind("wide", player["player_id"], "HDMI-A-1", expected_generation=0)
        save_position(registry, "wide")
        answer = client.get(route, params={"frame_id": "wide"}, headers=headers).json()
        assert {c["asset_id"]: c["fit"] for c in answer["candidates"]} == {
            wide.asset_id: "fits", unmeasured.asset_id: "not_measured"}
        assert answer["count"] == 2
        assert answer["skipped"] == {"beyond_crop_limit": 2, "below_frame_pixels": 1,
                                     "above_player_limit": 0, "face_cut": 0, "interest_cut": 1, "no_video_here": 0,
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


def test_authoring_onto_a_frame_with_no_known_screen_is_refused(registry):
    """Frame box run fb-8: a Frame whose screen size is not known has no shape, so no photo can
    be judged for it; authoring one onto it is refused and nothing is written."""
    portrait = asset(2, width=2000, height=4000)
    _, spec, _ = setup_source(registry, assets=(portrait,))
    add_frame(registry, "left", bound=False)
    coordinator = Coordinator(registry.db, registry.clock)
    scene = authored_scene(Contribution(target="frame:left", asset_refs=(portrait.asset_id,)))
    with pytest.raises(RegistryError, match="authored_frame_size_unknown") as refused:
        coordinator.configure_authored_scene(scene, spec.source_ref, (portrait.asset_id,))
    assert refused.value.status == 409
    assert count_authored(registry) == 0
    assert coordinator.runtime.read().export_state()["scenes"] == {}
