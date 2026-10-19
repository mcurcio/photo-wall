"""Every production place that judges a photo against a saved Frame reads the Frame output, not
the Display (owner, 2026-10-11: compatibility "measuring against the Frame output and not the
Display"): the planning pass, the authoring check, the candidates route and the fit analysis.

The Frame is a mat opening: a Cover Position 1,600 x 900 output pixels on a Pi that reports a
3,840 x 2,160 Output, so its Frame output (1,600 x 900) differs from both its Display and its
profile (1,920 x 1,080). A 1,600 x 900 photo fills that opening and nothing larger. Mutation
probes: build any of the four FitTargets from the Display's size (or the profile's) and its
assertion here fails."""
from __future__ import annotations

from fastapi.testclient import TestClient
from support.frames import frame_create
from support.measured_photos import measure
from support.photo_pools import photo, seed_pool
from support.position import save_position
from test_display_identity import ADMIN, AUTH
from test_registry import enroll
from test_wall_plan_tracer import prepare

from central.app import create_app
from central.coordination import Coordinator
from central.photo_fit_routes import ANALYSIS_PATH
from central.runtime import Contribution, Scene
from contracts.models import Calibration, PositionBox

DISPLAY = (3840, 2160)
OPENING = (1600, 900)
POOL = "library:1"


def _matted_frame(registry) -> None:
    player, _, _ = enroll(registry, count=1, size=DISPLAY)
    registry.create_frame(frame_create("hall"))
    registry.bind("hall", player["player_id"], "HDMI-A-1", expected_generation=0)
    # The opening in the Display's top-left corner: a box of its size.
    save_position(registry, "hall", Calibration(fit=PositionBox(
        width=OPENING[0] / DISPLAY[0], height=OPENING[1] / DISPLAY[1])))


def test_a_photo_that_fills_a_mat_opening_is_judged_against_the_opening_everywhere(registry):
    _matted_frame(registry)
    opening = photo("opening", taken_at=900, width=OPENING[0], height=OPENING[1])
    seed_pool(registry, POOL, (opening,))
    measure(registry, opening)
    prepare(registry, (opening,))
    client = TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False))

    analysis = client.post(ANALYSIS_PATH, headers=AUTH, json={"selection": {"sources": [POOL]}})
    assert analysis.status_code == 200, analysis.text
    (hall,) = analysis.json()["frames"]
    assert (hall["output_px"], hall["fit_by_shape"]) == (list(OPENING), 1)

    candidates = client.get(f"/v1/operator/sources/{POOL}/candidates",
                            params={"frame_id": "hall"}, headers=AUTH)
    assert candidates.status_code == 200, candidates.text
    assert {c["asset_id"]: c["fit"] for c in candidates.json()["candidates"]} == {
        opening.asset_id: "fits"}

    scene = Scene(scene_id="hand-placed", contributions=(
        Contribution(target="frame:hall", asset_refs=(opening.asset_id,)),))
    authored = client.put(f"/v1/operator/scenes/{scene.scene_id}/authored", headers=AUTH, json={
        "scene": scene.model_dump(mode="json"), "source_ref": POOL,
        "asset_ids": [opening.asset_id]})
    assert authored.status_code == 200, authored.text

    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(
        scene_id="house", cycle_seconds=30, loop=True,
        contributions=(Contribution(target="frame:hall", source_refs=(POOL,)),)))
    coordinator.runtime.command("activate", "house", "calendar", 1000)
    assert {entry.asset_id for entry in coordinator.advance().plan} == {opening.asset_id}


def test_a_bound_frame_with_no_known_screen_is_judged_for_no_photo_anywhere(registry):
    """Frame box run fb-8: a Frame bound to an Output that enrolled at 0 x 0, with no Display
    seen there, has no screen size, so no Frame output and no shape. The planning pass gives it
    no photo, the candidates route answers "frame_size_unknown", authoring onto it is refused
    "authored_frame_size_unknown" and the photo sheet lists it under no photo's Frames."""
    player, _, _ = enroll(registry, count=1, size=(0, 0))
    registry.create_frame(frame_create("hall"))
    registry.bind("hall", player["player_id"], "HDMI-A-1", expected_generation=0)
    save_position(registry, "hall")
    fitting = photo("fitting", taken_at=900, width=3840, height=2160)
    seed_pool(registry, POOL, (fitting,))
    measure(registry, fitting)
    prepare(registry, (fitting,))
    client = TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False))

    candidates = client.get(f"/v1/operator/sources/{POOL}/candidates",
                            params={"frame_id": "hall"}, headers=AUTH)
    assert (candidates.status_code, candidates.json()) == (409, {"error": "frame_size_unknown"})
    scene = Scene(scene_id="hand-placed", contributions=(
        Contribution(target="frame:hall", asset_refs=(fitting.asset_id,)),))
    authored = client.put(f"/v1/operator/scenes/{scene.scene_id}/authored", headers=AUTH, json={
        "scene": scene.model_dump(mode="json"), "source_ref": POOL,
        "asset_ids": [fitting.asset_id]})
    assert (authored.status_code, authored.json()) == (409, {"error": "authored_frame_size_unknown"})
    sheet = client.get(f"/v1/operator/library/photos/{fitting.asset_id}", headers=AUTH)
    assert sheet.status_code == 200, sheet.text
    assert sheet.json()["frames"] == []

    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(
        scene_id="house", cycle_seconds=30, loop=True,
        contributions=(Contribution(target="frame:hall", source_refs=(POOL,)),)))
    coordinator.runtime.command("activate", "house", "calendar", 1000)
    plan = coordinator.advance().plan
    assert plan
    assert {entry.asset_id for entry in plan} == {None}
