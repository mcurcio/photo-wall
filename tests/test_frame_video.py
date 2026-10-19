"""Shows videos is the Frame's own setting (frame box run, .claude/runs/frame-box/page.md, bead fb-5;
owner, 2026-10-19: the profile "shrinks to what is not geometry (whether video is allowed)").

`PUT /v1/operator/frames/{frame_id}/video` saves at once, last write wins: a bound, ready Frame stays
ready (no generation, configuration revision or Position check), and the Planner's next plan puts
no video on a Frame switched off while its photos play; switched on again, videos come back.
Migration 105 carries every stored profile's `video` onto the Frame.

Mutation probe: make `Registry.set_frame_video` skip the write when the Frame is bound ->
`test_switching_off_a_bound_ready_frame_saves_at_once_and_keeps_it_ready` goes RED."""
from __future__ import annotations

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from support.frame_media import seed_variant
from support.frames import frame_create
from support.migrations import migrate_before
from support.photo_pools import photo, seed_pool
from support.position import save_position
from test_display_identity import ADMIN, AUTH
from test_registry import enroll
from test_wall_plan_tracer import prepare

from central.app import create_app
from central.coordination import Coordinator
from central.displays.model import Readiness
from central.runtime import Contribution, Scene

DISPLAY = (1920, 1080)
POOL = "library:1"


def _client(registry) -> TestClient:
    return TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False))


def _ready_frame(registry) -> None:
    player, _, _ = enroll(registry, count=1, size=DISPLAY)
    registry.create_frame(frame_create("hall"))
    registry.bind("hall", player["player_id"], "HDMI-A-1", expected_generation=0)
    save_position(registry, "hall")


def _hall(registry):
    (hall,) = registry.inventory().frames
    return hall


def _audits(registry) -> list[dict]:
    with registry.db.transaction() as conn:
        return [row["detail"] for row in conn.execute(
            "SELECT detail FROM audit_events WHERE kind='frame_video_changed' AND subject='hall' "
            "ORDER BY occurred_at").fetchall()]


def test_switching_off_a_bound_ready_frame_saves_at_once_and_keeps_it_ready(registry):
    _ready_frame(registry)
    before = _hall(registry)
    assert (before.readiness, before.video) == (Readiness.READY, True)

    answer = _client(registry).put("/v1/operator/frames/hall/video", headers=AUTH, json={"video": False})

    assert answer.status_code == 200, answer.text
    assert answer.json() == {"frame_id": "hall", "video": False, "changed": True}
    after = _hall(registry)
    assert after.video is False
    assert (after.readiness, after.generation, after.configuration_revision, after.calibration) == (
        Readiness.READY, before.generation, before.configuration_revision, before.calibration)
    assert _audits(registry) == [{"video": False}]


def test_the_same_value_changes_nothing_and_audits_nothing(registry):
    _ready_frame(registry)
    answer = _client(registry).put("/v1/operator/frames/hall/video", headers=AUTH, json={"video": True})
    assert answer.json() == {"frame_id": "hall", "video": True, "changed": False}
    assert _audits(registry) == []


def test_an_unknown_frame_is_404_and_a_non_boolean_is_422(registry):
    client = _client(registry)
    missing = client.put("/v1/operator/frames/nowhere/video", headers=AUTH, json={"video": False})
    assert (missing.status_code, missing.json()["error"]) == (404, "unknown_frame")
    _ready_frame(registry)
    loose = client.put("/v1/operator/frames/hall/video", headers=AUTH, json={"video": "false"})
    assert loose.status_code == 422


def _planned_kinds(registry, *, switches: tuple[bool, ...]) -> set[str]:
    """The kinds the Planner's first plan puts on the hall Frame, after the operator flips
    Shows videos through `switches` in order. The pool holds a photo and a newer video, both of
    the Frame's shape and prepared; with no shown history the line puts its first on the Frame."""
    _ready_frame(registry)
    still = photo("still", taken_at=900, width=DISPLAY[0], height=DISPLAY[1])
    clip = photo("clip", taken_at=901, width=DISPLAY[0], height=DISPLAY[1], kind="video")
    seed_pool(registry, POOL, (still, clip))
    prepare(registry, (still,))
    seed_variant(registry, clip.asset_id, "f" * 64, width=DISPLAY[0], height=DISPLAY[1],
                 media_type="video/mp4", duration=10.0)
    client = _client(registry)
    for video in switches:
        assert client.put("/v1/operator/frames/hall/video", headers=AUTH, json={"video": video}).status_code == 200
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(
        scene_id="house", cycle_seconds=30, loop=True,
        contributions=(Contribution(target="frame:hall", source_refs=(POOL,)),)))
    coordinator.runtime.command("activate", "house", "calendar", 1000)
    kinds = {still.asset_id: "image", clip.asset_id: "video"}
    return {kinds[entry.asset_id] for entry in coordinator.advance().plan}


def test_switched_off_the_next_plan_puts_no_video_on_the_frame_while_its_photos_play(registry):
    assert _planned_kinds(registry, switches=(False,)) == {"image"}


def test_switched_on_again_videos_come_back(registry):
    assert "video" in _planned_kinds(registry, switches=(False, True))


def before_105(registry, profiles: dict[str, dict]) -> None:
    """Undo 105 on the real tables: no frames.video, each Frame's profile JSON back (108 dropped
    it), and 105 not in the ledger."""
    with registry.db.transaction() as conn:
        conn.execute("ALTER TABLE frames DROP COLUMN video, ADD COLUMN profile JSONB")
        for frame_id, profile in profiles.items():
            conn.execute("UPDATE frames SET profile=%s WHERE id=%s", (Jsonb(profile), frame_id))
        conn.execute("DELETE FROM schema_migrations WHERE name = '105_frame_video.sql'")


def test_105_carries_each_profiles_video_onto_its_frame(registry):
    registry.create_frame(frame_create("still-only", video=True))
    registry.create_frame(frame_create("moving", video=False))
    shape = {"width_px": 1920, "height_px": 1080, "diagonal_inches": 24}
    # The profiles as stored before 105; one stored before `video` was written: shows videos.
    before_105(registry, {"still-only": {**shape, "video": False}, "moving": shape})

    migrate_before(registry.db, "106")

    assert {frame.id: frame.video for frame in registry.inventory().frames} == {
        "still-only": False, "moving": True}
