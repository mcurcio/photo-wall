"""The photo fit settings, stored and served (follow-ups 2026-10-11, slice B1, case 8; run ledger
.claude/runs/photo-5a.md). Owner, 2026-10-11: "Agreed that these should be configurable, and not
hardcoded." One row (migration 086), validated by FitSettings alone; the Planner reads it on its
next pass, and a Scene without its own crop limit follows the house's."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from support.frames import frame_create
from support.measured_photos import measure
from support.photo_pools import photo, seed_pool
from support.position import save_position
from test_display_identity import ADMIN, AUTH
from test_registry import enroll
from test_wall_plan_tracer import prepare

from central.app import create_app
from central.coordination import Coordinator
from central.db import ProcessTransactionClock
from central.infra.fit_settings_store import PgFitSettings
from central.photo_fit_routes import SETTINGS_PATH
from central.runtime import Contribution, Scene
from central.wall_plan.fit_settings import FIT_SETTING_RANGES, FitSettings
from central.wall_plan.selection import SceneSelection

POOL = "library:1"


def _client(registry) -> TestClient:
    return TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False))


def _store(registry) -> PgFitSettings:
    return PgFitSettings(registry.db, times=ProcessTransactionClock(registry.clock))


def test_no_row_reads_as_the_defaults_with_their_ranges(registry):
    response = _client(registry).get(SETTINGS_PATH, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert FitSettings.model_validate(body["settings"]) == FitSettings()
    assert FitSettings.model_validate(body["defaults"]) == FitSettings()
    assert {name: tuple(bounds) for name, bounds in body["ranges"].items()} == dict(
        FIT_SETTING_RANGES)


def test_a_value_outside_its_range_or_an_incomplete_body_is_refused(registry):
    client = _client(registry)
    whole = FitSettings().model_dump(mode="json")
    response = client.put(SETTINGS_PATH, headers=AUTH, json=whole | {"crop_limit": 0.7})
    assert (response.status_code, response.json()) == (422, {"error": "invalid_request"})
    partial = {name: value for name, value in whole.items() if name != "face_padding"}
    assert client.put(SETTINGS_PATH, headers=AUTH, json=partial).status_code == 422
    assert _store(registry).fit_settings() == FitSettings()
    saved = client.put(SETTINGS_PATH, headers=AUTH, json=whole | {"face_padding": 0.1})
    assert saved.status_code == 200
    assert saved.json()["settings"]["face_padding"] == 0.1
    assert _store(registry).fit_settings().face_padding == 0.1


def test_a_row_that_no_longer_validates_reads_as_the_defaults(registry):
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO photo_fit_settings VALUES(TRUE,%s,0)",
                     (Jsonb(FitSettings().model_dump(mode="json") | {"crop_limit": 0.9}),))
    assert _store(registry).fit_settings() == FitSettings()


def test_a_row_saved_before_the_skew_existed_keeps_its_values_and_takes_the_skews_default(
        registry):
    """Subject detection: `interest_skew` is a new field with a default and no migration, so a
    row saved before it still reads as saved (here its crop limit), the skew at 0.33, and the
    console's read shows both.
    Mutation probe: make the field required -> the whole row reads as the defaults."""
    saved = FitSettings().model_dump(mode="json") | {"crop_limit": 0.5}
    del saved["interest_skew"]
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO photo_fit_settings VALUES(TRUE,%s,0)", (Jsonb(saved),))
    read = _store(registry).fit_settings()
    assert (read.crop_limit, read.interest_skew) == (0.5, 0.33)
    body = _client(registry).get(SETTINGS_PATH, headers=AUTH).json()
    assert (body["settings"]["crop_limit"], body["settings"]["interest_skew"]) == (0.5, 0.33)
    assert body["ranges"]["interest_skew"] == [0.0, 1.0]


def _one_wide_frame(registry) -> None:
    player, _, _ = enroll(registry)
    registry.create_frame(frame_create("frame-wide"))
    registry.bind("frame-wide", player["player_id"], "HDMI-A-1", expected_generation=0)
    save_position(registry, "frame-wide")


def _scene(crop_limit: float | None) -> Scene:
    return Scene(scene_id="house", cycle_seconds=30, loop=True,
                 selection=SceneSelection(crop_limit=crop_limit),
                 contributions=(Contribution(target="frame:frame-wide", source_refs=(POOL,)),))


@pytest.mark.parametrize(("scene_crop_limit", "placed"), [(None, False), (0.35, True)])
def test_the_house_crop_limit_moves_scenes_without_their_own_at_the_next_pass(
        registry, scene_crop_limit, placed):
    """B1 case 8: a 4:3 photo loses 25 % on a 16:9 Frame. With the house crop limit saved at
    20 %, the next planning pass refuses it for a Scene that sets none (its slots unfilled, "no
    photo fits"); a Scene with its own 35 % still takes it. Mutation probe: ignore a Scene's own
    crop limit -> the 35 % case fails."""
    _one_wide_frame(registry)
    four_three = photo("four-three", taken_at=900, width=4000, height=3000)
    seed_pool(registry, POOL, (four_three,))
    measure(registry, four_three)
    prepare(registry, (four_three,))
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", _scene(scene_crop_limit), by="console")
    coordinator.runtime.command("activate", "house", "calendar", 1000, by="console")
    assert {entry.asset_id for entry in coordinator.advance().projection.plan} == {four_three.asset_id}

    response = _client(registry).put(SETTINGS_PATH, headers=AUTH, json=FitSettings(
        crop_limit=0.2).model_dump(mode="json"))
    assert response.status_code == 200
    registry.clock.advance(3600)          # past the offered layers: the next pass plans anew
    following = coordinator.advance().projection
    fresh = [entry for entry in following.plan if entry.state != "locked"]
    assert fresh
    if placed:
        assert {entry.asset_id for entry in fresh} == {four_three.asset_id}
    else:
        assert {(entry.asset_id, entry.reason) for entry in fresh} == {(None, "no_photo_fits")}


def test_saved_settings_reach_the_analysis_the_candidates_and_the_authoring_check(registry):
    """The Fit board, the candidates route and the authoring check judge with the settings saved
    in Settings > Photo fit, as the Planner does: with the house crop limit saved at 20 %, a 4:3
    photo (25 % lost on a 16:9 Frame) fits under none of them, and fits again once it is back at
    the default. Mutation probes: read the defaults instead of the saved settings in any of the
    three -> its assertion fails."""
    from central.photo_fit_routes import ANALYSIS_PATH
    _one_wide_frame(registry)
    four_three = photo("four-three", taken_at=900, width=4000, height=3000)
    seed_pool(registry, POOL, (four_three,))
    measure(registry, four_three)
    client = _client(registry)

    def judged() -> tuple[int, int, set[str], int]:
        analysis = client.post(ANALYSIS_PATH, headers=AUTH, json={"selection": {"sources": [POOL]}})
        assert analysis.status_code == 200, analysis.text
        (frame,) = analysis.json()["frames"]
        candidates = client.get(f"/v1/operator/sources/{POOL}/candidates",
                                params={"frame_id": "frame-wide"}, headers=AUTH)
        assert candidates.status_code == 200, candidates.text
        scene = Scene(scene_id="hand-placed", contributions=(
            Contribution(target="frame:frame-wide", asset_refs=(four_three.asset_id,)),))
        authored = client.put(f"/v1/operator/scenes/{scene.scene_id}/authored", headers=AUTH,
                              json={"scene": scene.model_dump(mode="json"), "source_ref": POOL,
                                    "asset_ids": [four_three.asset_id]})
        return (frame["fit_by_shape"], frame["fit_checked"],
                {c["asset_id"] for c in candidates.json()["candidates"]}, authored.status_code)

    assert client.put(SETTINGS_PATH, headers=AUTH, json=FitSettings(
        crop_limit=0.2).model_dump(mode="json")).status_code == 200
    assert client.post(ANALYSIS_PATH, headers=AUTH, json={
        "selection": {"sources": [POOL]}}).json()["settings"]["crop_limit"] == 0.2
    assert judged() == (0, 0, set(), 409)
    assert client.put(SETTINGS_PATH, headers=AUTH, json=FitSettings().model_dump(
        mode="json")).status_code == 200
    assert judged() == (1, 1, {four_three.asset_id}, 200)
