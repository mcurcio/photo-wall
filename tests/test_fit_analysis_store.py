"""The fit analysis over PostgreSQL and its route (follow-ups 2026-10-11, slice B2, case 8; run
ledger .claude/runs/photo-5a.md). Pools are seeded through the photo library store's worker side;
the analysis answers through `POST /v1/operator/photo-fit/analysis`."""
from __future__ import annotations

from fastapi.testclient import TestClient
from support.frames import frame_create
from support.measured_photos import measure_all
from support.photo_pools import fixture_connection, library_of, photo, seed_pool
from support.position import save_position
from test_display_identity import ADMIN, AUTH, _bind, enroll_pi
from test_display_size import _sized
from test_registry import enroll

from central.app import create_app
from central.db import ProcessTransactionClock
from central.infra.fit_analysis_store import PgFitAnalysisReader
from central.photo_fit_routes import ANALYSIS_PATH
from central.photo_library.model import InPool, SourceQuery
from central.photo_library.ports import CheckComplete, CheckStep
from central.wall_plan.fit_analysis import FitSelection


def _client(registry) -> TestClient:
    return TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False))


def _reader(registry) -> PgFitAnalysisReader:
    return PgFitAnalysisReader(registry.db, times=ProcessTransactionClock(registry.clock))


def _wide_frame(registry, frame_id: str = "hall") -> None:
    player, _, _ = enroll(registry, count=1)
    registry.create_frame(frame_create(frame_id))
    registry.bind(frame_id, player["player_id"], "HDMI-A-1", expected_generation=0)
    save_position(registry, frame_id)


def _draft(registry, photos) -> str:
    """An unsaved Photo source draft whose pool's check found `photos`."""
    library = library_of(registry)
    draft = library.create_draft(SourceQuery(connection_ref=fixture_connection(registry),
                                             albums=("00000000-0000-4000-8000-00000000d0af",)),
                                 None)
    with registry.db.transaction() as conn:
        key = conn.execute("SELECT pool_key FROM photo_drafts WHERE draft_ref=%s",
                           (draft,)).fetchone()["pool_key"]
    lease = library.begin_check(key)
    entries = tuple(InPool(asset=item) for item in photos)
    assert library.record_step(lease, CheckStep(progress={"page": 1}, marks=(), entries=entries,
                                                seen_so_far=len(entries), immich_up_to=None))
    assert library.finish_check(lease, CheckComplete())
    return draft


def test_a_photo_in_two_sources_counts_once(registry):
    """B2 case 8: the selection's photos are its pools' in-pool revisions, each once."""
    shared = photo("shared", taken_at=900, width=3840, height=2160)
    family = (photo("family", taken_at=901, width=3840, height=2160), shared)
    garden = (shared, photo("garden", taken_at=902, width=2160, height=3840))
    seed_pool(registry, "family:1", family)
    seed_pool(registry, "garden:1", garden)
    measure_all(registry, (*family, garden[1]))
    sizes = _reader(registry).selection_sizes(FitSelection(sources=("family:1", "garden:1")))
    assert {(g.kind, g.width, g.height): (g.photos, g.measured) for g in sizes.groups} == {
        ("image", 3840, 2160): (2, 2), ("image", 2160, 3840): (1, 1)}
    assert sizes.reading is False
    sample = _reader(registry).sample(
        FitSelection(sources=("family:1", "garden:1")), frozenset({("image", 3840, 2160)}),
        seed=3, limit=10, measured_only=True)
    assert sorted(item.asset_id for item in sample) == sorted((family[0].asset_id,
                                                               shared.asset_id))
    assert all(item.key is not None for item in sample)


def test_a_photo_due_a_new_measurement_is_not_counted_as_checked_until_it_is_measured(registry):
    """Settings › Photo fit's "checked N of M" reads the key-content store's due rule: after an
    analysis version bump the count dips, and it climbs back as each photo is measured again.
    The sample still judges the due photo by the key the Planner places it with meanwhile.
    Mutation probes: count any measured row, whatever its version -> 2 checked at once; leave
    due keys out of the sample -> 1 photo sampled."""
    photos = tuple(photo(name, taken_at=900 + index, width=3840, height=2160)
                   for index, name in enumerate(("older", "current")))
    seed_pool(registry, "family:1", photos)
    measure_all(registry, photos)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE photo_key_content SET analysis_version='2' WHERE asset_id=%s",
                     (photos[0].asset_id,))
    selection = FitSelection(sources=("family:1",))
    (group,) = _reader(registry).selection_sizes(selection).groups
    assert (group.photos, group.measured) == (2, 1)
    sample = _reader(registry).sample(selection, frozenset({("image", 3840, 2160)}), seed=3,
                                      limit=10, measured_only=True)
    assert sorted(item.asset_id for item in sample) == sorted(p.asset_id for p in photos)
    measure_all(registry, photos[:1])
    (group,) = _reader(registry).selection_sizes(selection).groups
    assert (group.photos, group.measured) == (2, 2)


def test_the_route_answers_for_saved_and_draft_selections_without_an_immich_id(registry):
    """B2 case 8: an unsaved draft works as a selection (alone or with a Source); the answer
    names no Immich id."""
    _wide_frame(registry)
    saved = (photo("a", taken_at=900, width=3840, height=2160),
             photo("b", taken_at=901, width=640, height=360))
    seed_pool(registry, "family:1", saved)
    drafted = (photo("c", taken_at=902, width=4000, height=3000),)
    draft = _draft(registry, drafted)
    measure_all(registry, (*saved, *drafted))
    client = _client(registry)
    answer = client.post(ANALYSIS_PATH, headers=AUTH, json={
        "selection": {"sources": ["family:1"], "drafts": [draft]}})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    (hall,) = body["frames"]
    assert (body["photos"], hall["frame_id"], hall["output_px"]) == (3, "hall", [1920, 1080])
    assert hall["fit_by_shape"] == 2          # the 640 x 360 photo is under the Frame output
    assert {c["reason"]: c["photos"] for c in hall["refused_by_shape"]}["below_frame_pixels"] == 1
    assert hall["checked"] == 2 and hall["lead"] is not None
    assert hall["lead"]["thumbnail"] == f"/v1/operator/library/thumbnails/{hall['lead']['asset_id']}"
    for item in (*saved, *drafted):
        assert str(item.upstream_id) not in answer.text
    alone = client.post(ANALYSIS_PATH, headers=AUTH, json={
        "selection": {"drafts": [draft]}, "tier": "shape"}).json()
    assert alone["photos"] == 1 and alone["frames"][0]["fit_by_shape"] == 1


def test_unknown_sources_drafts_and_frames_are_refused(registry):
    _wide_frame(registry)
    seed_pool(registry, "family:1", (photo("a", taken_at=900, width=3840, height=2160),))
    client = _client(registry)
    cases = (
        ({"selection": {"sources": ["nowhere:1"]}}, "source_unknown"),
        ({"selection": {"drafts": ["00000000-0000-4000-8000-000000000001"]}}, "draft_unknown"),
        ({"selection": {"sources": ["family:1"]}, "frames": ["attic"]}, "frame_unknown"),
    )
    for body, code in cases:
        response = client.post(ANALYSIS_PATH, headers=AUTH, json=body)
        assert (response.status_code, response.json()) == (404, {"error": code})
    empty = client.post(ANALYSIS_PATH, headers=AUTH, json={"selection": {}})
    assert empty.status_code == 422
    assert client.post(ANALYSIS_PATH, json={"selection": {"sources": ["family:1"]}}).status_code == 401


def test_a_frame_draft_is_judged_by_its_box_alone_and_an_unbound_frame_is_unsized(registry):
    """Frame box run fb-10: a draft carries only its Position. On a 1,920 x 1,080 screen of a
    950 x 540 mm Display, a box a quarter wide and half high is judged at 480 x 540 and is no
    large Frame (21.5 inches across against the house's 40), so the 720-row video fits it; an
    unbound Frame (no known screen) is listed in `unsized` and has no answer. A draft carrying a
    profile is refused (no such field)."""
    pi = enroll_pi(registry, "pi-a")
    registry.create_frame(frame_create("lounge"))
    _bind(registry, "lounge", pi, "HDMI-A-1")
    save_position(registry, "lounge")
    _sized(registry, "pi-a", "HDMI-A-1", (950, 540))
    registry.create_frame(frame_create("attic"))
    seed_pool(registry, "family:1", (photo("tall", taken_at=900, width=1200, height=1350),
                                     photo("clip", taken_at=901, width=640, height=720,
                                           kind="video")))
    client = _client(registry)
    box = {"fit": {"mode": "box", "left": 0.5, "top": 0.25, "width": 0.25, "height": 0.5}}
    answer = client.post(ANALYSIS_PATH, headers=AUTH, json={
        "selection": {"sources": ["family:1"]}, "tier": "shape",
        "frame_drafts": [{"frame_id": "lounge", "calibration": box}]})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    (lounge,) = body["frames"]
    assert (lounge["frame_id"], lounge["output_px"], lounge["draft"]) == ("lounge", [480, 540], True)
    assert lounge["fit_by_shape"] == 2
    assert body["unsized"] == ["attic"]
    with_profile = client.post(ANALYSIS_PATH, headers=AUTH, json={
        "selection": {"sources": ["family:1"]}, "frame_drafts": [{
            "frame_id": "lounge", "calibration": box,
            "profile": {"width_px": 1920, "height_px": 1080, "diagonal_inches": 24}}]})
    assert with_profile.status_code == 422
