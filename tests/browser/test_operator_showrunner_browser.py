"""Behavioral browser checks for the Showrunner shell at /console (Bead 12).

Reuses the existing operator browser harness (real Chromium against the
production create_app on an ephemeral loopback listener, the disposable-schema
`registry` fixture, and the autouse `page_errors` guard). Frames are seeded
through the registry so the console renders real /inventory state.

Every assertion is BEHAVIORAL — role/text/visible state — never SVG coordinates
or DOM structure (design §1c). This is a NEW /console test file; the legacy
tests/browser/test_operator_content_browser.py (the OLD flat page) is untouched.

The R4 rule (design §2 R4, J4) is the load-bearing check: the Commissioning
facet — the home of every Display CONTROL — is UNREACHABLE in Showrunner mode.
This is the now-fully-enforceable version of Bead 4's placeholder probe: with
Showrunner mode existing, "Commissioning is Wall-only" is a real, red-able
assertion.
"""

import os

import pytest
from media_queue import RecordingMediaQueue
from playwright.sync_api import expect
from test_operator_browser import operator_server
from test_registry import ADMIN, enroll

from central.catalog import CatalogSnapshot
from central.media_repository import MediaRepository
from central.registry import FrameCreate
from contracts.models import Calibration, FrameProfile
from media.models import RefreshResult, SourceSpec
from tests.public_media import public_photo

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

PORTRAIT = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)

VALID_FRAME = "valid-frame"
INVALID_FRAME = "invalid-frame"
OUTPUT = "HDMI-A-1"

# A Source is a saved live QUERY named `name:rev` (design D-e) — the ref itself
# is the `name:rev` string the operator chose, NOT a downloaded album.
SOURCE = "holiday:1"

# The scene_id an operator authors below; a Scene is identified by its id, never
# a name (design J4).
SCENE_ID = "holiday-scene"


def _seed_source(registry, photos=()):
    """Configure ONE saved live query through the shared DB so the console's
    /v1/operator/media renders it, wiring a RecordingMediaQueue so the Refresh
    POST is accepted (202). No worker, upstream, or renderer runs.

    When `photos` are supplied, publish ONE successful refresh so the source is
    `ok` and its candidate membership is populated — this is what the authored
    candidates route (`GET …/sources/{ref}/candidates?frame_id=`) reads and
    hard-filters by profile. No variants are prepared: an authored save needs
    only source membership + asset metadata, and unprepared candidates are still
    offered (they render "Awaiting preparation" in the legacy page).

    Returns the queue to hand to operator_server so the app's own media
    repository (built in create_app) shares it and accepts the refresh.
    """
    queue = RecordingMediaQueue()
    repository = MediaRepository(registry.db, registry.clock, queue=queue)
    repository.configure_source(
        SourceSpec(source_ref=SOURCE, connection_ref="fixture-library"))
    if photos:
        lease = repository.begin_scheduled_refresh()
        assert lease is not None and lease.source.source_ref == SOURCE
        assert repository.publish_refresh(lease, RefreshResult(
            snapshot=CatalogSnapshot(
                source_ref=SOURCE, refreshed_at=registry.clock.utc(),
                candidates=tuple(photo.asset.candidate for photo in photos)),
            assets=tuple(photo.asset for photo in photos)))
    return queue


def _authored_photos(registry):
    """Two PORTRAIT-eligible candidates and one LANDSCAPE candidate.

    Both target Frames use the PORTRAIT profile, so the two portrait candidates
    pass central's per-Frame profile hard filter while the landscape candidate is
    rejected for a portrait Frame (planner.eligible: a portrait profile refuses a
    landscape original). The landscape candidate is therefore never offered in a
    portrait Frame's chooser — the load-bearing hard-filter invariant.
    """
    now = registry.clock.utc()
    return (
        public_photo(number=1, width=108, height=192, captured_at=now),
        public_photo(number=2, width=120, height=200, captured_at=now),
        public_photo(number=3, width=192, height=108, captured_at=now),
    )


def _seed(registry):
    """Seed two placed frames with KNOWN calibration_valid:

    - VALID_FRAME is bound then committed, so calibration_valid=true.
    - INVALID_FRAME is bound only (bind sets calibration_valid=false) and never
      committed, so calibration_valid=false.

    Two separate players so each frame binds a connected HDMI-A-1 of its own.
    """
    id_a, _key_a, _req_a = enroll(registry, count=1)
    id_b, _key_b, _req_b = enroll(registry, count=1)

    registry.create_frame(FrameCreate(
        id=VALID_FRAME, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    registry.create_frame(FrameCreate(
        id=INVALID_FRAME, surface_id="wall", x_mm=500, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))

    # VALID_FRAME: bind (generation 0->1, calibration_valid=false) then commit
    # (calibration_valid=true, revision 2).
    registry.bind(VALID_FRAME, id_a["player_id"], OUTPUT, expected_generation=0)
    registry.calibrate(
        VALID_FRAME, "commit", expected_revision=1,
        calibration=Calibration(gain=1.5), expected_generation=1)

    # INVALID_FRAME: bind only -> calibration_valid stays false.
    registry.bind(INVALID_FRAME, id_b["player_id"], OUTPUT, expected_generation=0)


def _connect(page, origin):
    page.goto(origin + "/console")
    page.get_by_label("Operator token").fill(ADMIN)
    page.get_by_role("button", name="Connect", exact=True).click()


def _to_showrunner(page):
    page.get_by_role("button", name="Showrunner", exact=True).click()


def test_showrunner_hides_wall_surfaces_and_shows_regions(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)

        # Wall mode is the default: the wall plan and equipment rail are present.
        expect(page.get_by_role("group", name="Wall plan for surface wall")).to_be_visible()
        expect(page.get_by_role("group", name="Pending players")).to_be_visible()

        _to_showrunner(page)

        # The four Showrunner regions appear.
        for region in ("Sources", "Scenes", "Programs", "Runs"):
            expect(page.get_by_role("region", name=region, exact=True)).to_be_visible()

        # The Wall-only surfaces are gone (not rendered in Showrunner mode).
        expect(page.get_by_role("group", name="Wall plan for surface wall")).to_have_count(0)
        expect(page.get_by_role("group", name="Pending players")).to_have_count(0)
        expect(page.get_by_role("group", name="Unplaced frames")).to_have_count(0)
        expect(page.get_by_label("Surface")).to_have_count(0)


def test_showrunner_renders_calibration_valid_badge(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        _to_showrunner(page)

        health = page.get_by_role("group", name="Frame health", exact=True)
        expect(health).to_be_visible()

        # Each Frame's calibration_valid renders as a STATUS badge, located by its
        # accessible identity label — a committed frame is valid, a bound-only
        # frame is invalid.
        expect(
            health.get_by_label(f"Frame {VALID_FRAME} calibration valid", exact=True)
        ).to_be_visible()
        expect(
            health.get_by_label(f"Frame {INVALID_FRAME} calibration invalid", exact=True)
        ).to_be_visible()


def test_r4_commissioning_unreachable_in_showrunner(page, registry):
    """R4: the Commissioning facet — the only home of Display CONTROLS — cannot
    be reached in the show layer. Showrunner never mounts the Inspector, so there
    is no Commissioning tab and no committed-calibration control anywhere in the
    show-mode DOM (design §2 R4 / J4).
    """
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)

        # Sanity: in Wall mode the Commissioning facet IS reachable (proves the
        # assertion below is meaningful, not vacuously true).
        page.get_by_role("button", name=f"Frame {VALID_FRAME}", exact=True).click()
        expect(page.get_by_role("tab", name="Commissioning", exact=True)).to_be_visible()

        _to_showrunner(page)

        # No Commissioning tab, no committed-calibration control, no editor — the
        # facet is composed out of the show layer entirely.
        expect(page.get_by_role("tab", name="Commissioning", exact=True)).to_have_count(0)
        expect(page.get_by_role("group", name="Committed calibration")).to_have_count(0)
        expect(page.get_by_role("group", name="Adjust calibration")).to_have_count(0)


def test_sources_render_name_rev_with_refresh(page, registry):
    """A Source renders by its `name:rev` identity with a Refresh button that
    POSTs …/sources/{ref}/refresh; the mutate-driven snapshot refresh keeps the
    Source listed (design J4).
    """
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)

        sources = page.get_by_role("region", name="Sources", exact=True)
        expect(sources).to_be_visible()
        # The Source is identified by its `name:rev` string, not an album title.
        expect(sources.get_by_text(SOURCE, exact=True)).to_be_visible()

        refresh = sources.get_by_role(
            "button", name=f"Refresh {SOURCE}", exact=True)
        with page.expect_response(
            lambda r: r.url.endswith("/refresh")
            and "/v1/operator/sources/" in r.url
            and r.request.method == "POST"
        ) as info:
            refresh.click()
        assert info.value.status == 202

        # After the useMutate() refresh, the Source is still listed (Plane A —
        # including the media catalog — was re-fetched, not dropped).
        expect(sources.get_by_text(SOURCE, exact=True)).to_be_visible()


def test_sources_have_no_immich_or_album_language(page, registry):
    """Immich boundary (design D-e): a Source is a saved live query, never a
    downloaded album, and no UI element may imply a Player browses or links to
    Immich. The Sources region carries NO "Immich" / "album" / "open in" copy.
    """
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)

        sources = page.get_by_role("region", name="Sources", exact=True)
        expect(sources).to_be_visible()
        # The Source must be present, so this is not vacuously true.
        expect(sources.get_by_text(SOURCE, exact=True)).to_be_visible()

        copy = sources.inner_text().lower()
        assert "immich" not in copy
        assert "album" not in copy
        assert "open in" not in copy


def test_author_live_source_scene_saves_and_appears_by_id(page, registry):
    """Bead 14a: author a LIVE-source Scene backed by a Source, targeting explicit
    Frames on a cycle interval; it saves in ONE request and then appears in the
    Scenes list by its scene_id (design J4).

    The single-request save is asserted on the ONE PUT to the plain scene route
    and its body: one media Contribution per target Frame, each driven by the
    chosen Source. This is the load-bearing invariant the mutation probe attacks
    (drop the target frames from the payload -> the target assertion goes red).
    """
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)

        scenes = page.get_by_role("region", name="Scenes", exact=True)
        expect(scenes).to_be_visible()
        # No scenes exist yet, so the appearance below is not vacuously true.
        expect(scenes.get_by_label(f"Scene {SCENE_ID}", exact=True)).to_have_count(0)

        form = scenes.get_by_role("form", name="Author a Scene", exact=True)
        form.get_by_label("Scene ID", exact=True).fill(SCENE_ID)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        # Target one or more EXPLICIT Frames.
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        form.get_by_label(f"Target frame {INVALID_FRAME}", exact=True).check()
        form.get_by_label("Seconds per cycle", exact=True).fill("45")

        with page.expect_response(
            lambda r: r.url.endswith(f"/v1/operator/scenes/{SCENE_ID}")
            and r.request.method == "PUT"
        ) as info:
            form.get_by_role("button", name="Save Scene", exact=True).click()

        response = info.value
        assert response.status == 200
        # The scene saved in ONE request whose body carries the live source and
        # every explicit target Frame as a media Contribution.
        body = response.request.post_data_json
        assert body["scene_id"] == SCENE_ID
        assert body["cycle_seconds"] == 45
        contributions = {c["target"]: c for c in body["contributions"]}
        assert set(contributions) == {
            f"frame:{VALID_FRAME}", f"frame:{INVALID_FRAME}"}
        for target in (VALID_FRAME, INVALID_FRAME):
            assert contributions[f"frame:{target}"]["source_refs"] == [SOURCE]

        # After the useMutate() refresh, the saved Scene appears by its scene_id.
        expect(scenes.get_by_label(f"Scene {SCENE_ID}", exact=True)).to_be_visible()


AUTHORED_SCENE_ID = "authored-scene"


def test_author_authored_scene_saves_per_frame_choices_in_one_request(page, registry):
    """Bead 14b: author a Scene with a DISTINCT asset chosen per target Frame,
    saved in ONE request to `PUT …/scenes/{id}/authored` (design J4).

    Each Frame's candidates come from `GET …/candidates?frame_id=`, already
    hard-filtered by that Frame's profile server-side. The single-request save is
    asserted on the ONE PUT to the authored route and its {scene, source_ref,
    asset_ids} body: one media Contribution per Frame carrying that Frame's chosen
    asset ref (never a live source_ref).
    """
    _seed(registry)
    portrait_a, portrait_b, _landscape = _authored_photos(registry)
    queue = _seed_source(registry, (portrait_a, portrait_b, _landscape))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)

        scenes = page.get_by_role("region", name="Scenes", exact=True)
        form = scenes.get_by_role("form", name="Author a Scene", exact=True)
        # Not vacuously true: the scene must not already exist.
        expect(scenes.get_by_label(f"Scene {AUTHORED_SCENE_ID}", exact=True)).to_have_count(0)

        form.get_by_label("Scene ID", exact=True).fill(AUTHORED_SCENE_ID)
        form.get_by_label("Authored per-frame", exact=True).check()
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        form.get_by_label(f"Target frame {INVALID_FRAME}", exact=True).check()

        # Each Frame's chooser loads its (profile-filtered) candidates: two
        # portrait candidates each, plus the placeholder option.
        valid_choice = form.get_by_label(f"Media for frame {VALID_FRAME}", exact=True)
        invalid_choice = form.get_by_label(f"Media for frame {INVALID_FRAME}", exact=True)
        expect(valid_choice.get_by_role("option")).to_have_count(3)
        expect(invalid_choice.get_by_role("option")).to_have_count(3)

        # A DISTINCT asset per Frame — the essence of per-frame authoring.
        valid_choice.select_option(portrait_a.asset.asset_id)
        invalid_choice.select_option(portrait_b.asset.asset_id)
        form.get_by_label("Seconds per cycle", exact=True).fill("20")

        with page.expect_response(
            lambda r: r.url.endswith(f"/v1/operator/scenes/{AUTHORED_SCENE_ID}/authored")
            and r.request.method == "PUT"
        ) as info:
            form.get_by_role("button", name="Save Scene", exact=True).click()

        response = info.value
        assert response.status == 200
        # ONE request carries the whole authored Scene: source_ref, the exact set
        # of chosen asset ids, and one Contribution per Frame with its asset ref.
        body = response.request.post_data_json
        assert body["source_ref"] == SOURCE
        assert set(body["asset_ids"]) == {
            portrait_a.asset.asset_id, portrait_b.asset.asset_id}
        scene = body["scene"]
        assert scene["scene_id"] == AUTHORED_SCENE_ID
        assert scene["cycle_seconds"] == 20
        by_target = {c["target"]: c for c in scene["contributions"]}
        assert by_target[f"frame:{VALID_FRAME}"]["asset_refs"] == [portrait_a.asset.asset_id]
        assert by_target[f"frame:{INVALID_FRAME}"]["asset_refs"] == [portrait_b.asset.asset_id]
        # Authored contributions carry NO live source_ref.
        for contribution in scene["contributions"]:
            assert "source_refs" not in contribution or not contribution["source_refs"]

        # After the useMutate() refresh, the saved Scene appears by its scene_id.
        expect(scenes.get_by_label(f"Scene {AUTHORED_SCENE_ID}", exact=True)).to_be_visible()


def test_authored_chooser_hard_filters_incompatible_candidate(page, registry):
    """Bead 14b: the profile HARD FILTER. A landscape candidate is ineligible for
    a portrait Frame, so `GET …/candidates?frame_id=` never returns it and the
    Frame's chooser never offers it (design J4).

    The two portrait candidates ARE offered (so the assertion is not vacuously
    true); the landscape candidate is NOT. This is the invariant the mutation
    probe attacks: dropping `frame_id` from the candidates request drops the
    profile filter, the landscape candidate reappears, and this test goes red.
    """
    _seed(registry)
    portrait_a, portrait_b, landscape = _authored_photos(registry)
    queue = _seed_source(registry, (portrait_a, portrait_b, landscape))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)

        scenes = page.get_by_role("region", name="Scenes", exact=True)
        form = scenes.get_by_role("form", name="Author a Scene", exact=True)
        form.get_by_label("Authored per-frame", exact=True).check()
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()

        choice = form.get_by_label(f"Media for frame {VALID_FRAME}", exact=True)
        # Both portrait candidates are eligible and offered.
        expect(choice.get_by_role("option", name="Photo 108×192", exact=True)).to_have_count(1)
        expect(choice.get_by_role("option", name="Photo 120×200", exact=True)).to_have_count(1)
        # The landscape candidate is hard-filtered out for a portrait Frame.
        expect(choice.get_by_role("option", name="Photo 192×108", exact=True)).to_have_count(0)


# Bead 15 — Programs (single window + priority) + optional N-window helper.

PROGRAM_ID = "morning-show"
PROGRAM_PRIORITY = 5
# Two datetime-local field values (operator-local time); the console converts
# them to POSIX-epoch seconds for the stored Program window (starts_at/ends_at).
WINDOW_START = "2027-03-01T09:00"
WINDOW_END = "2027-03-01T11:00"


def _author_live_scene(page, scene_id):
    """Author a LIVE-source Scene through the Scenes region so a real Scene exists
    to bind a Program to (a Program can only reference a stored Scene —
    central/runtime.py:298). Reuses the SceneAuthoring form; the seeded SOURCE and
    VALID_FRAME must already exist.
    """
    scenes = page.get_by_role("region", name="Scenes", exact=True)
    form = scenes.get_by_role("form", name="Author a Scene", exact=True)
    form.get_by_label("Scene ID", exact=True).fill(scene_id)
    form.get_by_label("Source", exact=True).select_option(SOURCE)
    form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
    form.get_by_label("Seconds per cycle", exact=True).fill("30")
    with page.expect_response(
        lambda r: r.url.endswith(f"/v1/operator/scenes/{scene_id}")
        and r.request.method == "PUT"
    ):
        form.get_by_role("button", name="Save Scene", exact=True).click()
    # The Scene must be listed before it can be bound (proves the refresh landed).
    expect(scenes.get_by_label(f"Scene {scene_id}", exact=True)).to_be_visible()


def test_program_schedules_single_window_and_lists(page, registry):
    """Bead 15: bind a Scene to a SINGLE time window with a priority via
    PUT …/programs/{id}; the stored Program then lists with its window + priority
    (design J4).
    """
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)
        _author_live_scene(page, SCENE_ID)

        programs = page.get_by_role("region", name="Programs", exact=True)
        expect(programs).to_be_visible()
        # Not vacuously true: no Program exists yet.
        expect(programs.get_by_label(f"Program {PROGRAM_ID}", exact=True)).to_have_count(0)

        form = programs.get_by_role("form", name="Schedule a Program", exact=True)
        form.get_by_label("Program ID", exact=True).fill(PROGRAM_ID)
        form.get_by_label("Scene", exact=True).select_option(SCENE_ID)
        form.get_by_label("Window start", exact=True).fill(WINDOW_START)
        form.get_by_label("Window end", exact=True).fill(WINDOW_END)
        form.get_by_label("Priority", exact=True).fill(str(PROGRAM_PRIORITY))

        with page.expect_response(
            lambda r: r.url.endswith(f"/v1/operator/programs/{PROGRAM_ID}")
            and r.request.method == "PUT"
        ) as info:
            form.get_by_role("button", name="Schedule Program", exact=True).click()

        response = info.value
        assert response.status == 200
        # A single-window Program: the body is exactly one Scene bound to ONE
        # [starts_at, ends_at) window with a priority — no recurrence field.
        body = response.request.post_data_json
        assert body["program_id"] == PROGRAM_ID
        assert body["scene_id"] == SCENE_ID
        assert body["priority"] == PROGRAM_PRIORITY
        assert body["ends_at"] > body["starts_at"]
        assert "recurrence" not in body
        assert "rrule" not in body

        # After the useMutate() refresh the Program lists with its priority.
        row = programs.get_by_label(f"Program {PROGRAM_ID}", exact=True)
        expect(row).to_be_visible()
        expect(row.get_by_text(f"Priority {PROGRAM_PRIORITY}", exact=True)).to_be_visible()


def test_program_remove_deletes_it(page, registry):
    """Bead 15: Remove issues DELETE …/programs/{id} and the Program leaves the
    list.
    """
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)
        _author_live_scene(page, SCENE_ID)

        programs = page.get_by_role("region", name="Programs", exact=True)
        form = programs.get_by_role("form", name="Schedule a Program", exact=True)
        form.get_by_label("Program ID", exact=True).fill(PROGRAM_ID)
        form.get_by_label("Scene", exact=True).select_option(SCENE_ID)
        form.get_by_label("Window start", exact=True).fill(WINDOW_START)
        form.get_by_label("Window end", exact=True).fill(WINDOW_END)
        form.get_by_label("Priority", exact=True).fill(str(PROGRAM_PRIORITY))
        with page.expect_response(
            lambda r: r.url.endswith(f"/v1/operator/programs/{PROGRAM_ID}")
            and r.request.method == "PUT"
        ):
            form.get_by_role("button", name="Schedule Program", exact=True).click()

        row = programs.get_by_label(f"Program {PROGRAM_ID}", exact=True)
        expect(row).to_be_visible()

        with page.expect_response(
            lambda r: r.url.endswith(f"/v1/operator/programs/{PROGRAM_ID}")
            and r.request.method == "DELETE"
        ) as info:
            programs.get_by_role(
                "button", name=f"Remove program {PROGRAM_ID}", exact=True).click()
        assert info.value.status == 200

        # After the refresh the Program is gone.
        expect(programs.get_by_label(f"Program {PROGRAM_ID}", exact=True)).to_have_count(0)


def test_n_window_helper_creates_separate_programs(page, registry):
    """Bead 15 / Q2: the optional helper creates N SEPARATE windows in one action
    — N independent, individually-stored single-window Programs (each a real
    PUT …/programs/{id}), NOT one recurring rule.
    """
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)
        _author_live_scene(page, SCENE_ID)

        programs = page.get_by_role("region", name="Programs", exact=True)
        form = programs.get_by_role("form", name="Schedule a Program", exact=True)
        form.get_by_label("Program ID", exact=True).fill(PROGRAM_ID)
        form.get_by_label("Scene", exact=True).select_option(SCENE_ID)
        form.get_by_label("Window start", exact=True).fill(WINDOW_START)
        form.get_by_label("Window end", exact=True).fill(WINDOW_END)
        form.get_by_label("Priority", exact=True).fill(str(PROGRAM_PRIORITY))

        multi = programs.get_by_role("group", name="Create separate windows", exact=True)
        multi.get_by_label("Number of windows", exact=True).fill("3")

        # The helper fans out into N discrete PUTs — one stored Program each.
        seen = []
        page.on("request", lambda req: (
            seen.append(req.url)
            if req.method == "PUT" and "/v1/operator/programs/" in req.url
            else None))
        multi.get_by_role("button", name="Add separate windows", exact=True).click()

        # Three separate Programs are now listed, by three distinct ids.
        for index in (1, 2, 3):
            expect(
                programs.get_by_label(f"Program {PROGRAM_ID}-{index}", exact=True)
            ).to_be_visible()


def test_programs_region_implies_no_recurrence_rule(page, registry):
    """Bead 15 honesty (design R2 / Q2): central stores single windows only, so
    NOTHING in the Programs region implies a stored recurrence rule. The N-window
    helper is described only as creating SEPARATE windows / INDIVIDUAL Programs;
    the words "recurring"/"recurrence" appear nowhere in the region.

    This is the mutation-probe target: labelling the helper a "recurring rule"
    turns this test red.
    """
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        _to_showrunner(page)

        programs = page.get_by_role("region", name="Programs", exact=True)
        expect(programs).to_be_visible()

        # The honest N-window copy is present (so the negative assertions below
        # are not vacuously true): separate windows / individual Programs.
        copy = programs.inner_text().lower()
        assert "separate windows" in copy
        assert "individual programs" in copy

        # No recurrence language anywhere in the region.
        assert "recurring" not in copy
        assert "recurrence" not in copy
