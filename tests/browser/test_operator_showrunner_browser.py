"""Behavioral browser checks for the Showrunner shell at /console (Bead 12).

Reuses the existing operator browser harness (real Chromium against the
production create_app on an ephemeral loopback listener, the disposable-schema
`registry` fixture, and the autouse `page_errors` guard). Frames are seeded
through the registry so the console renders real /inventory state.

Every assertion is BEHAVIORAL — role/text/visible state — never SVG coordinates
or DOM structure (design §1c). This is a /console test file; the legacy flat-page
tests were retired at the Bead 17 cutover (this file re-hosts their Showrunner
content on the redesign).

The R4 rule (design §2 R4, J4) is the load-bearing check: the Calibration
facet — the home of every Display CONTROL — is UNREACHABLE in Showrunner mode.
This is the now-fully-enforceable version of Bead 4's placeholder probe: with
Showrunner mode existing, "Calibration is Wall-only" is a real, red-able
assertion.
"""

import os
import re
from datetime import UTC, datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest
from console_tasks import (
    add_source,
    author_scene,
    connect,
    current_hash,
    go,
    scene_continue,
    scene_form,
    schedule_continue,
    schedule_form,
    schedule_program,
    show_advanced,
    show_now,
    source_continue,
    start_scene,
    start_schedule,
    start_source,
    visible_page,
    visit,
)
from media_queue import RecordingMediaQueue
from operator_harness import (
    RequestGate,
    answer_first,
    assert_fits_width,
    operator_server,
    report_readiness,
    tile_health,
)
from playwright.sync_api import expect
from psycopg.types.json import Jsonb
from public_media import public_photo, publish_photo
from test_registry import ADMIN, enroll

from central.catalog import CatalogSnapshot
from central.db import ProcessTransactionClock
from central.media_repository import MediaRepository
from central.media_store import MediaStore
from central.planner import AcquisitionRequest
from central.registry import FrameCreate
from central.runtime import Child, Contribution, Program, Scene
from central.runtime_store import RuntimeStore
from contracts.models import Calibration, FrameProfile
from media.models import RefreshResult, SourceSpec

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


def _runtime(registry):
    """Central's Runtime, commanded directly: setup the console cannot author."""
    return RuntimeStore(registry.db, registry.clock)


def _scene(scene_id, frame=VALID_FRAME, **fields):
    """A live-source Scene on one frame, stored as the console would save it."""
    return Scene(scene_id=scene_id, **{"loop": True, **fields}, contributions=(
        Contribution(target=f"frame:{frame}", source_refs=(SOURCE,)),))


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
    repository = MediaRepository(registry.db, registry.clock, queue=queue, times=ProcessTransactionClock(registry.clock))
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
    return id_a["player_id"], id_b["player_id"]


def test_showrunner_hides_wall_surfaces_and_shows_regions(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")

        # The Wall side: the plan on the Wall page; the boxes on Hardware.
        expect(page.get_by_role("group", name="Wall plan for surface wall")).to_be_visible()
        go(page, "hardware")
        expect(page.get_by_role("region", name="Pis", exact=True)).to_be_visible()

        # Each Show region has its own page, and none of them holds a Wall-only surface
        # (not even hidden: include_hidden and get_by_label count hidden DOM too).
        for section, region in (("now", "Runs"), ("scenes", "Scenes"),
                                ("schedule", "Programs"), ("sources", "Sources")):
            go(page, section)
            expect(page.get_by_role("region", name=region, exact=True)).to_be_visible()
            expect(page.get_by_role("group", name="Wall plan for surface wall", include_hidden=True)).to_have_count(0)
            expect(page.get_by_role("table", name="Players", exact=True, include_hidden=True)).to_have_count(0)
            expect(page.get_by_role("group", name="Unplaced frames", include_hidden=True)).to_have_count(0)
            expect(page.get_by_label("Surface")).to_have_count(0)


def test_showrunner_frame_health_badges_match_the_wall(page, registry):
    for player_id in _seed(registry):
        report_readiness(registry, player_id)
    registry.clock.advance(3)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        # The wall's labels, read first so the show layer can be held to them.
        valid_label = "Player app last reported 3 s ago"
        invalid_label = "Needs calibration"
        expect(tile_health(page, VALID_FRAME)).to_have_accessible_name(valid_label)
        expect(tile_health(page, INVALID_FRAME)).to_have_accessible_name(invalid_label)
        go(page, "now")

        health = page.get_by_role("group", name="Frame health", exact=True)
        expect(health).to_be_visible()

        # Each Frame's health renders as a STATUS badge, located by its accessible
        # identity label, with exactly the label the wall shows — a committed,
        # heard frame reads as heard; a bound-only frame needs calibration (a
        # to-do, never the alarm colour).
        expect(
            health.get_by_label(f"Frame {VALID_FRAME}: {valid_label}", exact=True)
        ).to_be_visible()
        invalid = health.get_by_label(f"Frame {INVALID_FRAME}: {invalid_label}", exact=True)
        expect(invalid).to_be_visible()
        expect(invalid).to_have_class(re.compile(r"\bhealth--todo\b"))


def test_r4_commissioning_unreachable_in_showrunner(page, registry):
    """R4: the Calibration facet — the only home of Display CONTROLS — cannot
    be reached in the show layer. Showrunner never mounts the Inspector, so there
    is no Calibration tab and no committed-calibration control anywhere in the
    show-mode DOM (design §2 R4 / J4).
    """
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")

        # Sanity: on the Wall the Calibration facet IS reachable (proves the
        # assertion below is meaningful, not vacuously true).
        page.get_by_role("button", name=f"Frame {VALID_FRAME}", exact=True).click()
        expect(page.get_by_role("tab", name="Calibration", exact=True)).to_be_visible()

        # On every Show page: no Calibration tab, no committed-calibration control,
        # no editor, not even in hidden DOM — the facet is composed out of the show
        # layer entirely (tests/browser/test_console_shell_browser.py visits every
        # Show route; this visits each Show page from an open Calibration facet).
        for section in ("now", "scenes", "schedule", "sources"):
            go(page, section)
            expect(page.get_by_role("tab", name="Calibration", exact=True,
                                    include_hidden=True)).to_have_count(0)
            expect(page.get_by_role("group", name="Committed calibration",
                                    include_hidden=True)).to_have_count(0)
            expect(page.get_by_role("group", name="Adjust calibration",
                                    include_hidden=True)).to_have_count(0)


def test_sources_render_name_rev_with_refresh(page, registry):
    """A Source renders by its `name:rev` identity with a Refresh button that
    POSTs …/sources/{ref}/refresh; the mutate-driven snapshot refresh keeps the
    Source listed (design J4).
    """
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "sources")

        sources = page.get_by_role("region", name="Sources", exact=True)
        expect(sources).to_be_visible()
        # The Source is identified by its `name:rev` string, not an album title.
        expect(sources.get_by_text("holiday", exact=True)).to_be_visible()

        refresh = sources.get_by_role(
            "button", name="Refresh holiday", exact=True)
        with page.expect_response(
            lambda r: r.url.endswith("/refresh")
            and "/v1/operator/sources/" in r.url
            and r.request.method == "POST"
        ) as info:
            refresh.click()
        assert info.value.status == 202

        # After the useMutate() refresh, the Source is still listed (Plane A —
        # including the media catalog — was re-fetched, not dropped).
        expect(sources.get_by_text("holiday", exact=True)).to_be_visible()


def test_sources_have_no_immich_or_album_language(page, registry):
    """Immich boundary (design D-e): a Source is a saved live query, never a
    downloaded album, and no UI element may imply a Player browses or links to
    Immich. The Sources region carries NO "Immich" / "album" / "open in" copy.
    """
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "sources")

        sources = page.get_by_role("region", name="Sources", exact=True)
        expect(sources).to_be_visible()
        # The Source must be present, so this is not vacuously true.
        expect(sources.get_by_text("holiday", exact=True)).to_be_visible()

        # The intro says what a Source is, in neutral library words (flow design §2 req 4).
        expect(sources.get_by_text(
            "Photo Wall selects media that lives in your photo library. It never uploads, "
            "edits or deletes anything there.", exact=True)).to_be_visible()
        _assert_neutral(sources)

        # Every step of the Source flow, Advanced open, says the same (one connection is
        # known, so the flow opens on Tags).
        form = start_source(page)
        _assert_neutral(sources)
        source_continue(page, "Narrow")
        _assert_neutral(sources)
        source_continue(page, "Name")
        form.get_by_role("button", name="Advanced", exact=True).click()
        form.get_by_label("Source name", exact=True).fill(NEW_SOURCE)
        _assert_neutral(sources)
        source_continue(page, "Review")
        _assert_neutral(sources)


def _assert_neutral(region):
    """No vendor, album or "open in" words, and no noun but Source, in what `region` shows
    (design D-e; console DDD §37)."""
    copy = region.inner_text().lower()
    for retired in ("immich", "album", "open in", "photo source", "match preview"):
        assert retired not in copy, retired


# Bead G2 — SR-source-config: CREATE a Source from the console (content-parity
# GAP 2). A distinct name:rev the seeded SOURCE does not use, so its appearance
# below is caused by THIS create, not the fixture.
NEW_SOURCE = "spring"


def test_source_configuration_creates_source_awaiting_refresh(page, registry):
    """Bead G2: an operator CONFIGURES a new Source from the console — the create
    the legacy page had and Bead 13's list+Refresh lacked (content-parity GAP 2).

    Filling the source-config form and submitting PUTs the stored SourceSpec to
    `/v1/operator/sources/{ref}` (ref = `name:rev`, path-encoded); the server
    answers with a SourceConfigurationReceipt ({source_ref, created}); and after
    the useMutate() refresh the new Source appears in the Sources list by its
    `name:rev` identity, awaiting its first refresh (design D-e / J4).

    This is the invariant the mutation probe attacks: break the create (omit the
    required source_ref from the body) and the Source is never stored, so the
    "appears awaiting refresh" assertion goes red.
    """
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "sources")

        sources = page.get_by_role("region", name="Sources", exact=True)
        expect(sources).to_be_visible()
        # Not vacuously true: the new Source does not exist before the create.
        expect(sources.get_by_text(NEW_SOURCE, exact=True)).to_have_count(0)

        response = add_source(page, NEW_SOURCE, "fixture-library", media_type="image")
        assert response.status == 200
        # The saved query carries its identity, connection and chosen kind.
        body = response.request.post_data_json
        assert body["expected_revision"] is None
        assert body["connection_ref"] == "fixture-library"
        assert body["media_types"] == ["image"]
        # The server reports the Source as CREATED.
        receipt = response.json()
        assert receipt["name"] == NEW_SOURCE and receipt["source_ref"] == NEW_SOURCE + ":1"
        assert receipt["created"] is True

        # After the useMutate() refresh the new Source is listed by name:rev, and
        # — never having been refreshed — shows the honest "Awaiting refresh".
        row = sources.get_by_role("listitem").filter(has_text=NEW_SOURCE)
        expect(row).to_be_visible()
        expect(row.get_by_text(NEW_SOURCE, exact=True)).to_be_visible()
        expect(row.get_by_text("Awaiting refresh", exact=True)).to_be_visible()


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
        connect(page, origin, "scenes")

        scenes = page.get_by_role("region", name="Scenes", exact=True)
        expect(scenes).to_be_visible()
        # No scenes exist yet, so the appearance below is not vacuously true.
        expect(scenes.get_by_label(f"Scene {SCENE_ID}", exact=True)).to_have_count(0)

        # Target one or more EXPLICIT Frames.
        response = author_scene(page, SCENE_ID, SOURCE, (VALID_FRAME, INVALID_FRAME), seconds=45)
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
        connect(page, origin, "scenes")

        scenes = page.get_by_role("region", name="Scenes", exact=True)
        # Not vacuously true: the scene must not already exist.
        expect(scenes.get_by_label(f"Scene {AUTHORED_SCENE_ID}", exact=True)).to_have_count(0)

        # The flow: Kind (hand-picked) → Photos → Frames → Media per frame → Playback →
        # Review, with the same field labels the single form had.
        form = start_scene(page, hand_picked=True)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        form.get_by_label(f"Target frame {INVALID_FRAME}", exact=True).check()
        scene_continue(page, "Media per frame")

        # Each Frame's chooser loads its (profile-filtered) candidates: two
        # portrait candidates each, plus the placeholder option.
        valid_choice = form.get_by_label(f"Media for frame {VALID_FRAME}", exact=True)
        invalid_choice = form.get_by_label(f"Media for frame {INVALID_FRAME}", exact=True)
        expect(valid_choice.get_by_role("option")).to_have_count(3)
        expect(invalid_choice.get_by_role("option")).to_have_count(3)

        # A DISTINCT asset per Frame — the essence of per-frame authoring.
        valid_choice.select_option(portrait_a.asset.asset_id)
        invalid_choice.select_option(portrait_b.asset.asset_id)
        scene_continue(page, "Playback")
        form.get_by_label("Seconds per cycle", exact=True).fill("20")
        scene_continue(page, "Review")
        form.get_by_label("Scene name", exact=True).fill(AUTHORED_SCENE_ID)

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
        connect(page, origin, "scenes")

        form = start_scene(page, hand_picked=True)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        scene_continue(page, "Media per frame")

        choice = form.get_by_label(f"Media for frame {VALID_FRAME}", exact=True)
        # Both portrait candidates are eligible and offered.
        expect(choice.get_by_role("option", name=re.compile(r"^Photo 108×192"))).to_have_count(1)
        expect(choice.get_by_role("option", name=re.compile(r"^Photo 120×200"))).to_have_count(1)
        # The landscape candidate is hard-filtered out for a portrait Frame.
        expect(choice.get_by_role("option", name=re.compile(r"^Photo 192×108"))).to_have_count(0)


def test_a_get_through_apiwrite_does_not_drop_a_poll(page, registry):
    """Pass 2 §7: only non-GET calls move the write fence. The candidates read goes through
    apiWrite as a GET while a poll is in flight; the poll still lands (the age resets)."""
    _seed(registry)
    queue = _seed_source(registry, _authored_photos(registry))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes", paused_at=registry.clock.utc())
        form = start_scene(page, hand_picked=True)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")

        reads = RequestGate(page, "**/v1/operator/snapshot")
        reads.holding = True
        page.clock.run_for(5000)
        reads.wait_held()
        reads.holding = False
        expect(page.get_by_text(re.compile(r"updated 5 s ago"))).to_be_visible()

        # The candidates GET, through apiWrite, while the poll is in flight: the flow
        # reads them as soon as a frame is targeted, whichever step shows.
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        scene_continue(page, "Media per frame")
        choice = form.get_by_label(f"Media for frame {VALID_FRAME}", exact=True)
        expect(choice.get_by_role("option", name=re.compile(r"^Photo 108×192"))).to_have_count(1)

        reads.release()
        expect(page.get_by_text(re.compile(r"updated 0 s ago"))).to_be_visible()


# Bead 15 — Programs (single window + priority) + optional N-window helper.

PROGRAM_ID = "morning-show"
PROGRAM_PRIORITY = 5
# Two datetime-local field values (operator-local time); the console converts
# them to POSIX-epoch seconds for the stored Program window (starts_at/ends_at).
WINDOW_START = "2027-03-01T09:00"
WINDOW_END = "2027-03-01T11:00"
WINDOW = (WINDOW_START, WINDOW_END, PROGRAM_PRIORITY)


def test_program_schedules_single_window_and_lists(page, registry):
    """Bead 15: bind a Scene to a SINGLE time window with a priority via
    PUT …/programs/{id}; the stored Program then lists with its window + priority
    (design J4).
    """
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        author_scene(page, SCENE_ID, SOURCE, (VALID_FRAME,))

        go(page, "schedule")
        programs = page.get_by_role("region", name="Programs", exact=True)
        expect(programs).to_be_visible()
        # Not vacuously true: no Program exists yet.
        expect(programs.get_by_label(f"Program {PROGRAM_ID}", exact=True)).to_have_count(0)

        response = schedule_program(page, PROGRAM_ID, SCENE_ID, *WINDOW)
        assert response.url.endswith(f"/v1/operator/programs/{PROGRAM_ID}")
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
        connect(page, origin)
        author_scene(page, SCENE_ID, SOURCE, (VALID_FRAME,))

        programs = page.get_by_role("region", name="Programs", exact=True)
        schedule_program(page, PROGRAM_ID, SCENE_ID, *WINDOW)

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


def test_program_remove_refusal_and_unknown_outcome_can_be_retried(page, registry):
    """A refusal explains that nothing was removed; a server error stays unknown,
    refreshes the list, and allows an explicit retry. Mutation probe: drop outcome
    feedback or treat a 5xx as success."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        author_scene(page, SCENE_ID, SOURCE, (VALID_FRAME,))
        programs = page.get_by_role("region", name="Programs", exact=True)
        schedule_program(page, PROGRAM_ID, SCENE_ID, *WINDOW)
        row = programs.get_by_label(f"Program {PROGRAM_ID}", exact=True)
        remove_url = f"**/v1/operator/programs/{PROGRAM_ID}"
        responses = iter((409, 500, None))

        def fail_then_delete(route):
            if route.request.method != "DELETE":
                route.continue_()
                return
            status = next(responses)
            if status is None:
                route.continue_()
            elif status == 409:
                route.fulfill(status=status, content_type="application/json",
                              body='{"error":"program_started"}')
            else:
                route.fulfill(status=status, content_type="application/json",
                              body='{"error":"temporary_failure"}')

        page.route(remove_url, fail_then_delete)
        row.get_by_role("button", name=f"Remove program {PROGRAM_ID}", exact=True).click()
        expect(row.get_by_role("status")).to_have_text(
            f"Program {PROGRAM_ID} was not removed: program started. Check the current Program state before retrying.")
        expect(row.get_by_role("button", name=f"Remove program {PROGRAM_ID}", exact=True)).to_be_enabled()

        row.get_by_role("button", name=f"Remove program {PROGRAM_ID}", exact=True).click()
        expect(row.get_by_role("status")).to_have_text(
            f"Removal outcome for Program {PROGRAM_ID} is unknown. Check the current Program state before retrying.")
        expect(row.get_by_role("button", name=f"Remove program {PROGRAM_ID}", exact=True)).to_be_enabled()

        with page.expect_response(
            lambda response: response.url.endswith(f"/v1/operator/programs/{PROGRAM_ID}")
            and response.request.method == "DELETE"
        ) as info:
            row.get_by_role("button", name=f"Remove program {PROGRAM_ID}", exact=True).click()
        assert info.value.status == 200
        expect(programs.get_by_label(f"Program {PROGRAM_ID}", exact=True)).to_have_count(0)

        # A later Program may reuse the plain id; the old accepted receipt must
        # not disable its Remove control or appear on the new card.
        schedule_program(page, PROGRAM_ID, SCENE_ID, *WINDOW)
        row = programs.get_by_label(f"Program {PROGRAM_ID}", exact=True)
        expect(row.get_by_role("button", name=f"Remove program {PROGRAM_ID}", exact=True)).to_be_enabled()
        expect(row.get_by_role("status")).to_have_count(0)


def test_scene_delete_removes_unused_scene_with_revision_guard(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        author_scene(page, "delete-unused", SOURCE, (VALID_FRAME,))
        scenes = page.get_by_role("region", name="Scenes", exact=True)
        with page.expect_response(lambda r: r.request.method == "DELETE" and "/v1/operator/scenes/delete-unused?" in r.url) as info:
            scenes.get_by_role("button", name="Delete Scene delete-unused", exact=True).click()
            dialog = page.get_by_role("dialog")
            expect(dialog).to_contain_text("removes Scene delete-unused from future choices")
            expect(dialog).to_contain_text("never stops a Run")
            dialog.get_by_role("button", name="Confirm delete", exact=True).click()
        assert info.value.status == 200
        assert info.value.request.url.endswith("expected_revision=1")
        expect(scenes.get_by_role("heading", name="Scene delete-unused", exact=True)).to_have_count(0)


def test_scene_delete_refusal_names_dependent_program(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        author_scene(page, SCENE_ID, SOURCE, (VALID_FRAME,))
        go(page, "schedule")
        schedule_program(page, PROGRAM_ID, SCENE_ID, *WINDOW)
        go(page, "scenes")
        scenes = page.get_by_role("region", name="Scenes", exact=True)
        with page.expect_response(lambda r: r.request.method == "DELETE" and f"/v1/operator/scenes/{SCENE_ID}?" in r.url) as info:
            scenes.get_by_role("button", name=f"Delete Scene {SCENE_ID}", exact=True).click()
            dialog = page.get_by_role("dialog")
            expect(dialog).to_contain_text(f"Program {PROGRAM_ID}")
            dialog.get_by_role("button", name="Confirm delete", exact=True).click()
        assert info.value.status == 409
        expect(page.get_by_role("alert")).to_contain_text(f"Program {PROGRAM_ID}")


def test_n_window_helper_creates_separate_programs(page, registry):
    """Bead 15 / Q2: the optional helper creates N SEPARATE windows in one action
    — N independent, individually-stored single-window Programs (each a real
    PUT …/programs/{id}), NOT one recurring rule.
    """
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        author_scene(page, SCENE_ID, SOURCE, (VALID_FRAME,))

        programs = page.get_by_role("region", name="Programs", exact=True)
        # Bead 4: "Number of windows" sits under the When step's Advanced, and the write
        # is Review's "Add separate windows".
        form = schedule_program(page, PROGRAM_ID, SCENE_ID, *WINDOW, windows=3, submit=False)

        # The helper fans out into N discrete PUTs — one stored Program each.
        seen = []
        page.on("request", lambda req: (
            seen.append(req.url)
            if req.method == "PUT" and "/v1/operator/programs/" in req.url
            else None))
        form.get_by_role("button", name="Add separate windows", exact=True).click()

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
        connect(page, origin, "schedule")

        programs = page.get_by_role("region", name="Programs", exact=True)
        expect(programs).to_be_visible()

        # Bead 4: the helper lives under the When step's Advanced; the Schedule page
        # names it, and so does the helper itself. Both are held to the same honesty.
        visit(page, "#/schedule/new/when")
        schedule_form(page).get_by_role("button", name="Advanced", exact=True).click()
        expect(programs.get_by_role("group", name="Create separate windows", exact=True)
               ).to_be_visible()
        helper_copy = programs.inner_text().lower()
        go(page, "schedule")
        page_copy = programs.inner_text().lower()

        for copy in (page_copy, helper_copy):
            # The honest N-window copy is present (so the negative assertions below
            # are not vacuously true): separate windows / individual Programs.
            assert "separate windows" in copy
            assert "individual programs" in copy

            # No recurrence language anywhere in the region.
            assert "recurring" not in copy
            assert "recurrence" not in copy


# Bead 16 — Run control + activation outcome + "why" panel (SR-runs).

WHY_HIGH = "why-high"
WHY_LOW = "why-low"


def test_activation_shows_synchronous_outcome_truthfully(page, registry):
    """Bead 16: POST …/activations returns a SYNCHRONOUS Admission {status,
    reason}; the console shows that outcome at the moment, exactly as returned —
    admitted first, then IGNORED for a second activation of the same running Scene
    (repeat=ignore). The non-admitted outcome is surfaced truthfully, never
    softened (design J4).
    """
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        author_scene(page, SCENE_ID, SOURCE, (VALID_FRAME,))

        go(page, "now")
        runs = page.get_by_role("region", name="Runs", exact=True)
        outcome = runs.get_by_label("Activation outcome", exact=True)
        # No outcome is shown before an activation — it is synchronous only.
        expect(outcome).to_have_count(0)

        response = show_now(page, SCENE_ID, 0)
        assert response.status == 200
        # The server admitted it; the console says exactly that.
        expect(runs.get_by_label("Activation outcome", exact=True)).to_have_text(
            f"Started: Central admitted a Run of {SCENE_ID}.")
        expect(runs.get_by_text("Revision", exact=True)).to_have_count(0)

        # Activating the SAME running Scene again (new activation id,
        # repeat=ignore) is IGNORED — shown truthfully, not as a success.
        show_now(page, SCENE_ID, 0)
        expect(runs.get_by_label("Activation outcome", exact=True)).to_have_text(
            f"Not started: {SCENE_ID} is already running, left as is.")


def test_runs_region_shows_only_synchronous_outcomes_no_missed_window(page, registry):
    """Slice 3 §9 (rewrites Bead 16's no-missed-row check): an activation outcome
    still appears only at the moment of activation, and a Program reads "Missed"
    only for a missed_window outcome Central served. A window Central slept
    through after a tick is a warm restart: it caught up logically, so the row
    reads "Ran" (never "Missed"), with the limit stated in its hint.

    Mutation-probe target: treating any past window with no live Run as missed
    turns the "slept" assertions red.
    """
    _seed(registry)
    queue = _seed_source(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _scene(SCENE_ID, loop=False))
    now = registry.clock.utc()
    runtime.command("advance", now)  # Central has ticked: a warm Runtime
    runtime.command("set_program", Program(  # saved after its window ended
        program_id="late", scene_id=SCENE_ID, starts_at=now - 600, ends_at=now - 300))
    runtime.command("set_program", Program(
        program_id="slept", scene_id=SCENE_ID, starts_at=now + 60, ends_at=now + 120))
    registry.clock.advance(300)  # Central is down across "slept"
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")

        runs = page.get_by_role("region", name="Runs", exact=True)
        expect(runs.get_by_label("Activation outcome", exact=True)).to_have_count(0)

        go(page, "schedule")
        programs = page.get_by_role("region", name="Programs", exact=True)
        # Past Programs are collapsed until asked for.
        expect(programs.get_by_label("Program late", exact=True)).not_to_be_visible()
        programs.get_by_text("Past (2)", exact=True).click()
        missed = "Missed: its window had ended before Central first scheduled it."
        expect(programs.get_by_label("Program late", exact=True)).to_contain_text(missed)
        slept = programs.get_by_label("Program slept", exact=True)
        expect(slept).to_contain_text(re.compile(r"Ran \d\d:\d\d.*\(one cycle, then ended\)"))
        expect(slept).to_contain_text("if Central was down during the window")
        expect(slept).not_to_contain_text("Missed")

        # The outcome appears ONLY at the synchronous moment of activation.
        response = show_now(page, SCENE_ID, 0)
        assert response.status == 200
        expect(runs.get_by_label("Activation outcome", exact=True)).to_have_text(
            f"Started: Central admitted a Run of {SCENE_ID}.")


def test_cancel_removes_live_run(page, registry):
    """Bead 16: an activated Scene lists as a live Run with Finish and Cancel;
    Cancel issues POST …/runs/{id}/cancel and, after the mutate refresh, the Run
    leaves the list (design J4).
    """
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        author_scene(page, SCENE_ID, SOURCE, (VALID_FRAME,))

        go(page, "now")
        runs = page.get_by_role("region", name="Runs", exact=True)
        # Not vacuous: no live Runs before activation.
        expect(runs.get_by_text("No Run is running.", exact=True)).to_be_visible()

        show_now(page, SCENE_ID, 0)

        run_row = runs.get_by_role("listitem").first
        expect(run_row).to_be_visible()
        expect(run_row.get_by_text(f"Scene {SCENE_ID}", exact=True)).to_be_visible()

        run_row.get_by_role("button", name=re.compile(r"^Cancel run ")).click()
        dialog = page.get_by_role("dialog", name=f"Cancel the Run of {SCENE_ID}?")
        expect(dialog).to_contain_text("skipping its outro; its child Scenes stop too")
        with page.expect_response(
            lambda r: "/v1/operator/runs/" in r.url
            and r.url.endswith("/cancel")
            and r.request.method == "POST"
        ) as info:
            dialog.get_by_role("button", name="Confirm cancel", exact=True).click()
        assert info.value.status == 200

        # After the useMutate() refresh the cancelled Run is gone.
        expect(runs.get_by_text("No Run is running.", exact=True)).to_be_visible()


def test_finish_live_run_posts(page, registry):
    """Bead 16: Finish issues POST …/runs/{id}/finish for a live Run (design J4).
    """
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        author_scene(page, SCENE_ID, SOURCE, (VALID_FRAME,))

        runs = page.get_by_role("region", name="Runs", exact=True)
        show_now(page, SCENE_ID, 0)

        run_row = runs.get_by_role("listitem").first
        expect(run_row).to_be_visible()

        with page.expect_response(
            lambda r: "/v1/operator/runs/" in r.url
            and r.url.endswith("/finish")
            and r.request.method == "POST"
        ) as info:
            run_row.get_by_role("button", name=re.compile(r"^Finish run ")).click()
        assert info.value.status == 200


def test_why_panel_ranks_contributions_by_precedence(page, registry):
    """Bead 16: the "why" panel ranks a Frame's contributions by the total
    precedence order (priority, root_order, admission_order) — deterministically,
    winner first — reusing the shared precedence read (primitive #4, join.js).

    Two Scenes both target VALID_FRAME; the higher-priority activation ranks ABOVE
    the lower one regardless of activation order (design J4/§6a).
    """
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        # Two Scenes, both targeting VALID_FRAME.
        author_scene(page, WHY_LOW, SOURCE, (VALID_FRAME,))
        author_scene(page, WHY_HIGH, SOURCE, (VALID_FRAME,))

        # Activate the LOW priority first, the HIGH priority second — so ordering
        # cannot be an accident of activation order.
        show_now(page, WHY_LOW, 1)
        show_now(page, WHY_HIGH, 5)

        runs = page.get_by_role("region", name="Runs", exact=True)
        why = runs.get_by_role("group", name="Why", exact=True)
        why.get_by_role("button", name=f"Why? {VALID_FRAME}", exact=True).click()

        rows = why.get_by_role("list", name="Contribution precedence").get_by_role("listitem")
        expect(rows).to_have_count(2)
        # Deterministic precedence: higher priority (why-high, 5) ranks first.
        expect(rows.nth(0)).to_contain_text(WHY_HIGH)
        expect(rows.nth(0)).to_contain_text("priority 5")
        expect(rows.nth(1)).to_contain_text(WHY_LOW)
        expect(rows.nth(1)).to_contain_text("priority 1")


# Pass 2 slice 3A (docs/operator-console-ux-pass2-showrunner.md).


def _scenes_form(page):
    return scene_form(page)


def _epoch(page, local):
    """The POSIX seconds of a `datetime-local` value in the browser's time zone."""
    return page.evaluate("(value) => new Date(value).getTime() / 1000", local)




def test_tracer_a_named_scene_keeps_playing_through_its_program(page, registry):
    """§15 tracer: the operator names a Scene, sees the id it saves under, keeps
    "Keep playing" on and saves; the flow's draft is discarded and the list shows the id.
    A Program 18:00–20:00 with Central 5 min past 18:00 still holds its Run live — a
    Scene no longer plays one cycle and stops (the P1).

    Bead 2: the Scene form is a flow. "Keep playing" is on by default under Playback's
    Advanced, the name is asked on Review, and "the form clears" is now "the draft is
    discarded": the flow returns to the cards, which offer a fresh "New Scene" and no
    "Resume draft"."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        scenes = page.get_by_role("region", name="Scenes", exact=True)
        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        scene_continue(page, "Playback")
        form.get_by_role("button", name="Advanced", exact=True).click()
        expect(form.get_by_label("Keep playing until the Program ends", exact=True)).to_be_checked()
        scene_continue(page, "Review")
        name = form.get_by_label("Scene name", exact=True)
        name.fill("Family Evening")
        expect(name).to_have_accessible_description(re.compile("Saved as family-evening"))
        with page.expect_response(
            lambda r: r.url.endswith("/v1/operator/scenes/family-evening")
            and r.request.method == "PUT"
        ) as info:
            form.get_by_role("button", name="Save Scene", exact=True).click()
        body = info.value.request.post_data_json
        assert info.value.status == 200
        assert body["scene_id"] == "family-evening" and body["loop"] is True

        # The draft is discarded, so the saved Scene never reads as a collision.
        expect(scenes.get_by_label("Scene family-evening", exact=True)).to_be_visible()
        expect(scenes.get_by_role("button", name="New Scene", exact=True)).to_be_visible()
        expect(scenes.get_by_role("button", name=re.compile("^Resume draft"))).to_have_count(0)
        expect(scenes.get_by_text(re.compile("already exists"))).to_have_count(0)

        start = _epoch(page, "2027-03-01T18:00")
        registry.clock.advance(start + 300 - registry.clock.utc())
        # A wall-clock jump of years ends the 30-day session (pass A §8): sign in again.
        connect(page, origin)
        schedule_program(page, "evening-show", "family-evening",
                         "2027-03-01T18:00", "2027-03-01T20:00", 0)
        go(page, "now")
        runs = page.get_by_role("region", name="Runs", exact=True)
        expect(runs.get_by_text("Scene family-evening", exact=True)).to_be_visible()


def test_a_name_without_a_latin_letter_asks_for_an_id(page, registry):
    """Bead 2: the Id sits under Review's Advanced; a name with no usable id opens it at
    once with the reason (it used to appear beside the name in the single form)."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = author_scene(page, "夕方", SOURCE, (VALID_FRAME,), submit=False)
        identifier = form.get_by_label("Id", exact=True)
        expect(identifier).to_be_visible()
        # Held open while the id must be typed: the toggle says so and cannot close it.
        advanced = form.get_by_role("button", name="Advanced", exact=True)
        expect(advanced).to_have_attribute("aria-expanded", "true")
        expect(advanced).to_be_disabled()
        expect(identifier).to_have_accessible_description(
            "This name needs a Latin letter or digit for its id; type an id.")
        identifier.fill("yugata")
        with page.expect_response(
            lambda r: r.url.endswith("/v1/operator/scenes/yugata") and r.request.method == "PUT"
        ) as info:
            form.get_by_role("button", name="Save Scene", exact=True).click()
        assert info.value.status == 200


def test_the_id_is_derived_from_the_name_under_advanced_and_can_be_changed(page, registry):
    """Bead 2, slice 3 §5: Review's Advanced says the id the name saves under; "Change"
    in the name's hint opens it at the Id field, and a typed id is what is saved."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = author_scene(page, "Family Evening", SOURCE, (VALID_FRAME,), submit=False)
        advanced = form.get_by_role("button", name="Advanced", exact=True)
        expect(advanced).to_have_attribute("aria-expanded", "false")
        expect(advanced).to_have_accessible_description("Id: family-evening")
        form.get_by_role("button", name="Change", exact=True).click()
        identifier = form.get_by_label("Id", exact=True)
        expect(identifier).to_be_focused()
        expect(identifier).to_have_value("family-evening")
        identifier.fill("evening-2")
        expect(advanced).to_have_accessible_description("Id: evening-2")
        with page.expect_response(
            lambda r: r.url.endswith("/v1/operator/scenes/evening-2") and r.request.method == "PUT"
        ) as info:
            form.get_by_role("button", name="Save Scene", exact=True).click()
        assert info.value.status == 200


def test_a_colliding_name_is_refused_before_any_request(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        author_scene(page, "family-evening", SOURCE, (VALID_FRAME,))
        puts = []
        page.on("request", lambda request: puts.append(request.url)
                if request.method == "PUT" else None)
        form = author_scene(page, "Family Evening", SOURCE, (VALID_FRAME,), submit=False)
        name = form.get_by_label("Scene name", exact=True)
        collision = "A Scene called family-evening already exists; choose another name."
        expect(name).to_have_accessible_description(re.compile(re.escape(collision)))
        form.get_by_role("button", name="Save Scene", exact=True).click()
        expect(form.get_by_role("alert")).to_contain_text(collision)
        expect(name).to_be_focused()
        assert puts == []


def test_the_problem_summary_is_frozen_at_submit(page, registry):
    """§6: submitting with problems sends nothing and freezes a summary; a poll
    that changes the live problems (another operator saves "evening") updates
    the field's reason but never rewrites the summary under the reader.

    Bead 2: Review is reached by a typed URL with the Source and frames unanswered, so
    Save lists problems that live on earlier steps; focus goes to the summary (its first
    entry opens the Photos step), not to a Source field that Review does not show."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes", paused_at=registry.clock.utc())
        visit(page, "#/scenes/new/review")
        form = _scenes_form(page)
        name = form.get_by_label("Scene name", exact=True)
        name.fill("Evening")
        form.get_by_role("button", name="Save Scene", exact=True).click()
        summary = form.get_by_role("alert")
        expect(summary).to_contain_text("Choose a Source.")
        expect(summary).to_contain_text("Choose at least one frame.")
        expect(summary).to_be_focused()

        _runtime(registry).command("set_scene", Scene(
            scene_id="evening",
            contributions=(Contribution(target=f"frame:{VALID_FRAME}", source_refs=(SOURCE,)),)))
        page.clock.run_for(5000)
        collision = "A Scene called evening already exists; choose another name."
        expect(name).to_have_accessible_description(re.compile(re.escape(collision)))
        expect(summary).not_to_contain_text("already exists")
        expect(summary).to_contain_text("Choose a Source.")


# §12 layout.

LONG_ID = "reception" + "northwallleftofthemainentrance" * 3  # no break opportunity

def _seed_long_ids(registry):
    """A Source, Scene, Program and live Run whose ids are long unbroken strings."""
    MediaRepository(registry.db, registry.clock, queue=RecordingMediaQueue(), times=ProcessTransactionClock(registry.clock)).configure_source(
        SourceSpec(source_ref=LONG_ID + ":1", connection_ref="fixture-library"))
    runtime = _runtime(registry)
    runtime.command("set_scene", Scene(
        scene_id=LONG_ID, loop=True,
        contributions=(Contribution(target=f"frame:{VALID_FRAME}", source_refs=(SOURCE,)),)))
    now = registry.clock.utc()
    runtime.command("set_program", Program(
        program_id=LONG_ID, scene_id=LONG_ID, starts_at=now + 3600, ends_at=now + 7200))
    runtime.command("activate", LONG_ID, "long-act", now)


def _box(page, region):
    return page.get_by_role("region", name=region, exact=True).bounding_box()


def test_each_show_region_is_a_page_beside_the_sidebar_wide_and_full_width_narrow(
        page, registry):
    # Bead 1b replaced the two-column Showrunner with one page per section: wide, the
    # sidebar sits beside the page and every Show region starts at the same place;
    # narrow, the sidebar becomes a drawer and each region spans the page.
    _seed(registry)
    queue = _seed_source(registry)
    page.set_viewport_size({"width": 1440, "height": 900})
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        nav = page.get_by_role("navigation", name="Sections", exact=True).bounding_box()
        boxes = {}
        for section, region in (("now", "Runs"), ("scenes", "Scenes"),
                                ("schedule", "Programs"), ("sources", "Sources")):
            go(page, section)
            boxes[region] = _box(page, region)
            assert nav["x"] + nav["width"] <= boxes[region]["x"], region
        assert len({round(box["x"]) for box in boxes.values()}) == 1, boxes

        page.set_viewport_size({"width": 390, "height": 844})
        for section, region in (("now", "Runs"), ("scenes", "Scenes")):
            go(page, section)
            box = _box(page, region)
            assert box["x"] <= 16.5 and box["x"] + box["width"] >= 390 - 16.5, (region, box)


def test_long_ids_never_scroll_the_showrunner_sideways_at_phone_width(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    _seed_long_ids(registry)
    page.set_viewport_size({"width": 390, "height": 844})
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        # Every Show page, each showing a long id: a Run, a Scene, a Program, a Source.
        for section, text in (
                              ("schedule", f"Program {LONG_ID}"), ("sources", LONG_ID)):
            go(page, section)
            expect(visible_page(page).get_by_text(text, exact=True).first).to_be_visible()
            assert_fits_width(page, section)


# §9 rows and §10 precedence.

LOBBY_FRAME = "lobby-left"


def _add_lobby_frame(registry):
    registry.create_frame(FrameCreate(
        id=LOBBY_FRAME, surface_id="lobby", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))


def _refused_by_guard(registry):
    """A protecting Run of "guard" on VALID_FRAME, and a Program for SCENE_ID on the
    same frame whose window starts while guard runs: Central refuses it."""
    runtime = _runtime(registry)
    runtime.command("set_scene", _scene("guard", protect_frames=True))
    runtime.command("set_scene", _scene(SCENE_ID))
    now = registry.clock.utc()
    runtime.command("activate", "guard", "guard-act", now, priority=5)
    runtime.command("set_program", Program(
        program_id="blocked", scene_id=SCENE_ID, starts_at=now + 60, ends_at=now + 200000))
    return runtime, now


def test_a_refused_program_names_its_protector(page, registry):
    """Central names the refusing Run with the refusal; the console never
    re-derives it, so a Scene edited afterwards cannot change who is named
    (mutation-probe target: the Scene now reaches only INVALID_FRAME)."""
    _seed(registry)
    queue = _seed_source(registry)
    runtime, _now = _refused_by_guard(registry)
    registry.clock.advance(120)
    runtime.command("advance", registry.clock.utc())  # Central refuses "blocked" at +60
    # An edit (revision 2): the Scene now reaches only INVALID_FRAME.
    runtime.command("set_scene", _scene(SCENE_ID, frame=INVALID_FRAME, revision=2))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "schedule")
        row = page.get_by_role("region", name="Programs", exact=True).get_by_label(
            "Program blocked", exact=True)
        expect(row).to_contain_text(
            f"Did not start: {VALID_FRAME} was protected by the Run of guard.")
        go(page, "now")
        guard = page.get_by_role("region", name="Runs", exact=True).get_by_role(
            "listitem").filter(has_text="Scene guard")
        expect(guard).to_contain_text(f"protects {VALID_FRAME}")
        expect(guard).to_contain_text("priority 5")


def test_a_refused_program_whose_protector_is_no_longer_served_names_none(page, registry):
    """The protector ended over a day ago, so it is not served: the row never names
    a Run it cannot show (mutation-probe target: a name without a served Run)."""
    _seed(registry)
    queue = _seed_source(registry)
    runtime, now = _refused_by_guard(registry)
    guard = next(run.run_id for run in runtime.read().project(now).runs if run.scene_id == "guard")
    registry.clock.advance(100)
    runtime.command("cancel", guard, registry.clock.utc())  # refuses "blocked" at +60 first
    registry.clock.advance(86400 + 60)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "schedule")
        row = page.get_by_role("region", name="Programs", exact=True).get_by_label(
            "Program blocked", exact=True)
        expect(row).to_contain_text(
            "Did not start: its frames were protected by another Run, no longer listed.")
        expect(row).not_to_contain_text("guard")


def test_a_one_cycle_scene_says_so_on_the_scene_and_its_run(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = author_scene(page, SCENE_ID, SOURCE, (VALID_FRAME,), loop=False, submit=False)
        # Review lists the advanced value too.
        expect(form.get_by_text("No, it plays one cycle", exact=True)).to_be_visible()
        with page.expect_response(
            lambda r: r.url.endswith(f"/v1/operator/scenes/{SCENE_ID}") and r.request.method == "PUT"
        ) as info:
            form.get_by_role("button", name="Save Scene", exact=True).click()
        assert info.value.request.post_data_json["loop"] is False
        once = "plays one 30 s cycle, then ends"
        expect(page.get_by_role("region", name="Scenes", exact=True).get_by_label(
            f"Scene {SCENE_ID}", exact=True)).to_contain_text(once)
        show_now(page, SCENE_ID, 0)
        run_row = page.get_by_role("region", name="Runs", exact=True).get_by_role("listitem").first
        expect(run_row).to_contain_text(once)
        expect(run_row).to_contain_text("started directly (Show now or the API)")


def test_why_states_admission_order_and_the_limit_line(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", Scene(
        scene_id="evening", loop=True,
        contributions=(Contribution(target=f"frame:{VALID_FRAME}", source_refs=(SOURCE,)),),
        children=(Child(scene=_scene("intro")),)))
    runtime.command("activate", "evening", "evening-act", registry.clock.utc())
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        why = page.get_by_role("region", name="Runs", exact=True).get_by_role(
            "group", name="Why", exact=True)
        why.get_by_role("button", name=f"Why? {VALID_FRAME}", exact=True).click()
        expect(why).to_contain_text(
            f"Central's Runs on {VALID_FRAME}: intro (priority 0, part of evening's Run, started "
            "directly, by Show now or the API) on top.")  # a child names its root's Run (§35)
        rows = why.get_by_role("list", name="Contribution precedence").get_by_role("listitem")
        expect(rows).to_have_count(2)
        expect(rows.nth(1)).to_have_text(
            "evening (priority 0) is underneath: same Run of evening; "
            "the later child Scene is on top.")
        expect(why).to_contain_text(
            "If intro has no usable media for this frame (none eligible, still preparing, or "
            "no compatible variant), Central plans the next layer down instead.")
        expect(why).to_contain_text("An unbound frame gets no layers at all.")


def test_why_names_the_winning_program_from_its_root_run(page, registry):
    """The winner's Program is read from its root Run (`program_id`), never from
    the Intent (mutation-probe target). Removing its running Program is confirmed."""
    _seed(registry)
    queue = _seed_source(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _scene("evening"))
    runtime.command("set_scene", _scene("morning"))
    now = registry.clock.utc()
    runtime.command("set_program", Program(
        program_id="weekday-evenings", scene_id="evening", starts_at=now + 10,
        ends_at=now + 7200, priority=5))
    runtime.command("activate", "morning", "morning-act", now, priority=1)
    registry.clock.advance(60)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        runs = page.get_by_role("region", name="Runs", exact=True)
        why = runs.get_by_role("group", name="Why", exact=True)
        why.get_by_role("button", name=f"Why? {VALID_FRAME}", exact=True).click()
        expect(why).to_contain_text(
            f"Central's Runs on {VALID_FRAME}: evening (priority 5, Program weekday-evenings) on top.")
        expect(why.get_by_role("list", name="Contribution precedence").get_by_role("listitem").nth(1)).to_have_text(
            "morning (priority 1) is underneath: evening has priority 5.")
        expect(runs.get_by_role("listitem").filter(has_text="Scene evening")).to_contain_text(
            "Program weekday-evenings")
        # The Now page names the zone its clock times use.
        expect(runs).to_contain_text(re.compile(r"Times in .+ \(this browser's time zone\)"))

        # The Program's Run is the planned fact on the read-only Wall tile and on Frame ›
        # Status (console DDD §35): Central's Runs, never what the Panel shows.
        planned = ("On top: evening · Program weekday-evenings (Central's Runs; media not "
                   "checked; the Panel is not observed)")
        go(page, "wall")
        tile = page.get_by_role("group", name=f"Frame {VALID_FRAME} status", exact=True)
        expect(tile).to_contain_text(planned)
        expect(tile.get_by_role("img", name=planned, exact=True)).to_have_count(1)
        expect(tile).not_to_contain_text("Ending (outro)")
        page.get_by_role("button", name=f"Frame {VALID_FRAME}", exact=True).click()
        inspector = page.get_by_role("region", name=f"Frame {VALID_FRAME} inspector", exact=True)
        expect(inspector).to_contain_text(planned)
        expect(inspector).to_contain_text(
            f"Central's Runs on {VALID_FRAME}: evening (priority 5, Program weekday-evenings) on top.")

        go(page, "schedule")
        programs = page.get_by_role("region", name="Programs", exact=True)
        row = programs.get_by_label("Program weekday-evenings", exact=True)
        expect(row).to_contain_text(re.compile(r"Running since \d\d:\d\d"))
        row.get_by_role("button", name="Remove program weekday-evenings", exact=True).click()
        dialog = page.get_by_role("dialog", name="Remove program weekday-evenings?")
        with page.expect_response(
            lambda r: r.url.endswith("/v1/operator/programs/weekday-evenings")
            and r.request.method == "DELETE"
        ):
            dialog.get_by_role("button", name="Confirm remove", exact=True).click()
        expect(programs.get_by_role("status")).to_have_text("Program weekday-evenings removed.")


def test_the_target_picker_groups_by_surface_with_health_in_the_description(page, registry):
    _seed(registry)
    _add_lobby_frame(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        lobby = form.get_by_role("group", name="Frames on lobby", exact=True)
        wall = form.get_by_role("group", name="Frames on wall", exact=True)
        picked = lobby.get_by_role("checkbox", name=f"Target frame {LOBBY_FRAME}", exact=True)
        expect(picked).to_have_accessible_name(f"Target frame {LOBBY_FRAME}")
        expect(picked).to_have_accessible_description("Needs a Player")
        valid = wall.get_by_role("checkbox", name=f"Target frame {VALID_FRAME}", exact=True)
        expect(valid).to_have_accessible_name(f"Target frame {VALID_FRAME}")
        expect(valid).to_have_accessible_description("No report yet")
        expect(wall.get_by_role("checkbox")).to_have_count(2)


def test_a_target_deleted_mid_draft_is_dropped_and_announced(page, registry):
    _seed(registry)
    _add_lobby_frame(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes", paused_at=registry.clock.utc())
        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        form.get_by_label(f"Target frame {LOBBY_FRAME}", exact=True).check()

        response = page.request.delete(
            f"{origin}/v1/operator/frames/{LOBBY_FRAME}",
            headers={"Authorization": f"Bearer {ADMIN}"},
        )
        assert response.status == 200
        page.clock.run_for(5000)
        expect(form.get_by_role("status")).to_have_text(
            f"{LOBBY_FRAME} was deleted and removed from this Scene.")
        expect(form.get_by_label(f"Target frame {LOBBY_FRAME}", exact=True)).to_have_count(0)
        scene_continue(page, "Playback")
        scene_continue(page, "Review")
        form.get_by_label("Scene name", exact=True).fill(SCENE_ID)
        with page.expect_response(
            lambda r: r.url.endswith(f"/v1/operator/scenes/{SCENE_ID}") and r.request.method == "PUT"
        ) as info:
            form.get_by_role("button", name="Save Scene", exact=True).click()
        targets = [c["target"] for c in info.value.request.post_data_json["contributions"]]
        assert targets == [f"frame:{VALID_FRAME}"]


# §11 activation.


def _runs_of(page, origin, scene_id):
    """Every served Run of a Scene (live or ended), read through the public contract."""
    response = page.request.get(origin + "/v1/operator/runtime", headers={
        "Authorization": "Bearer " + ADMIN})
    return [run for run in response.json()["current"]["runs"] if run["scene_id"] == scene_id]


def _retry_after_unknown(page, registry, answer):
    """Restart SCENE_ID; the first POST reaches Central (and commits) but the
    console gets `answer` instead. It says "Outcome unknown"; the retry must send
    the SAME activation id, so Central returns the stored Admission and there is
    one Run. A fresh id would restart again: two Runs (one cancelled)."""
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _scene(SCENE_ID))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        sent = []
        page.on("request", lambda request: sent.append(request.post_data_json["activation_id"])
                if request.url.endswith("/v1/operator/activations")
                and request.method == "POST" else None)

        def first_attempt(route):
            route.fetch()  # the write reaches Central and commits
            answer(route)
        answer_first(page, "**/v1/operator/activations", first_attempt)

        runs = page.get_by_role("region", name="Runs", exact=True)
        # Bead 5: the activation is the Show-now flow; "Restart it" sits under Review's
        # Advanced.
        form = show_now(page, SCENE_ID, repeat="Restart it", submit=False)
        form.get_by_role("button", name="Activate now", exact=True).click()
        outcome = runs.get_by_label("Activation outcome", exact=True)
        expect(outcome).to_have_text(
            "Outcome unknown. Try again; it will not start twice. "
            "Changing the form makes this a new activation.")

        with page.expect_response(lambda r: r.url.endswith("/v1/operator/activations")):
            form.get_by_role("button", name="Activate now", exact=True).click()
        expect(outcome).to_have_text(f"Started: Central admitted a Run of {SCENE_ID}.")
        assert len(set(sent)) == 1, sent
        assert len(_runs_of(page, origin, SCENE_ID)) == 1


def test_an_activation_retried_after_an_abort_reuses_its_key(page, registry):
    _retry_after_unknown(page, registry, lambda route: route.abort())


def test_an_activation_retried_after_a_500_reuses_its_key(page, registry):
    _retry_after_unknown(page, registry, lambda route: route.fulfill(
        status=500, content_type="application/json", body='{"error": "internal"}'))


def test_restart_states_that_the_new_run_has_no_program_end(page, registry):
    _seed(registry)
    _runtime(registry).command("set_scene", _scene(SCENE_ID))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "now")
        # Bead 5: the choice is on the Show-now flow's Review, under Advanced.
        form = show_now(page, SCENE_ID, submit=False)
        show_advanced(form)
        expect(form.get_by_label("Leave it running", exact=True)).to_be_checked()
        restart = form.get_by_label("Restart it", exact=True)
        restart.check()
        expect(restart).to_have_accessible_description(
            "Ends the current Run and starts a new one now. A restarted Run has no Program "
            "end; it plays until finished.")


def test_a_one_cycle_restart_says_it_plays_one_cycle(page, registry):
    _seed(registry)
    _runtime(registry).command("set_scene", _scene("once", loop=False, cycle_seconds=20))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "now")
        form = show_now(page, "once", submit=False)
        show_advanced(form)
        restart = form.get_by_label("Restart it", exact=True)
        restart.check()
        expect(restart).to_have_accessible_description(
            "Ends the current Run and starts a new one now. A restarted Run has no Program "
            "end; it plays one 20 s cycle, then ends.")


def test_an_ended_protecting_run_says_protected_in_the_past(page, registry):
    _seed(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _scene("guard", protect_frames=True))
    now = registry.clock.utc()
    guard = runtime.command("activate", "guard", "guard-act", now).run_id
    registry.clock.advance(60)
    runtime.command("cancel", guard, registry.clock.utc())
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "now")
        runs = page.get_by_role("region", name="Runs", exact=True)
        runs.get_by_text("Recently ended (1)", exact=True).click()
        row = runs.get_by_label("Cancelled Runs", exact=True).get_by_role("listitem")
        expect(row).to_contain_text(f"protected {VALID_FRAME}")
        expect(row).not_to_contain_text(f"protects {VALID_FRAME}")


def test_a_protected_refusal_names_the_protecting_run(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _scene("guard", protect_frames=True))
    runtime.command("set_scene", _scene(SCENE_ID))
    runtime.command("activate", "guard", "guard-act", registry.clock.utc(), priority=5)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        response = show_now(page, SCENE_ID, 0)
        assert response.json()["reason"] == "protected_frames"
        expect(page.get_by_role("region", name="Runs", exact=True).get_by_label(
            "Activation outcome", exact=True)).to_have_text(
            f"Not started: {VALID_FRAME} is protected by the Run of guard.")


# §7 windows helper and Source form.


def _program_puts(page):
    """Collect every Program PUT body the console sends, in order."""
    bodies = []
    page.on("request", lambda request: bodies.append(request.post_data_json)
            if request.method == "PUT" and "/v1/operator/programs/" in request.url else None)
    return bodies


def _windows(page):
    return page.get_by_role("region", name="Programs", exact=True).get_by_role(
        "group", name="Create separate windows", exact=True)


def test_the_windows_helper_follows_the_weekday_mask(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        author_scene(page, SCENE_ID, SOURCE, (VALID_FRAME,))
        # 1 March 2027 is a Monday.
        form = schedule_program(page, "Weekday Show", SCENE_ID, "2027-03-01T18:00",
                                "2027-03-01T20:00", 0, windows=6, submit=False)
        # Review's Change opens When's Advanced at the mask (bead 4).
        form.get_by_role("button", name="Change Repeat on", exact=True).click()
        repeat = _windows(page).get_by_role("group", name="Repeat on", exact=True)
        expect(repeat.get_by_label("Monday", exact=True)).to_be_focused()
        for day in ("Saturday", "Sunday"):
            repeat.get_by_label(day, exact=True).uncheck()
        schedule_continue(page, "Review")
        bodies = _program_puts(page)
        form.get_by_role("button", name="Add separate windows", exact=True).click()
        programs = page.get_by_role("region", name="Programs", exact=True)
        expect(programs.get_by_role("status")).to_have_text("Created 6 separate Programs.")
        days = ("01", "02", "03", "04", "05", "08")
        expected = {f"weekday-show-{n}": _epoch(page, f"2027-03-{day}T18:00")
                    for n, day in enumerate(days, start=1)}
        assert {body["program_id"]: body["starts_at"] for body in bodies} == expected


@pytest.mark.browser_context_args(timezone_id="Europe/London")
def test_the_windows_helper_keeps_local_time_across_a_dst_change(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        author_scene(page, SCENE_ID, SOURCE, (VALID_FRAME,))
        go(page, "schedule")
        programs = page.get_by_role("region", name="Programs", exact=True)
        expect(programs).to_contain_text("Times in Europe/London (this browser's time zone)")
        # British Summer Time starts at 01:00 UTC on Sunday 28 March 2027.
        form = schedule_program(page, "Evening", SCENE_ID, "2027-03-27T18:00",
                                "2027-03-27T20:00", 0, windows=2, submit=False)
        # Review names the zone too, beside the times it lists (bead 4).
        expect(form).to_contain_text("Times in Europe/London")
        bodies = _program_puts(page)
        form.get_by_role("button", name="Add separate windows", exact=True).click()
        expect(programs.get_by_label("Program evening-2", exact=True)).to_be_visible()
        # Every Program card's clock times carry their zone (console DDD §35): GMT before the
        # change, BST (or its offset, in a locale with no abbreviation for it) after.
        expect(programs.get_by_label("Program evening-1", exact=True)).to_contain_text(
            re.compile(r"18:00–20:00 GMT"))
        expect(programs.get_by_label("Program evening-2", exact=True)).to_contain_text(
            re.compile(r"18:00–20:00 (BST|UTC\+01:00)"))
        first, second = sorted(bodies, key=lambda body: body["program_id"])
        # 18:00 GMT then 18:00 BST: 23 hours apart, not 24.
        assert second["starts_at"] - first["starts_at"] == 23 * 3600
        assert second["ends_at"] - first["ends_at"] == 23 * 3600


def test_the_windows_helper_refuses_overlap_and_overlong_ids(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        author_scene(page, SCENE_ID, SOURCE, (VALID_FRAME,))
        bodies = _program_puts(page)
        # Bead 4: the overlap is the When step's (it asks the window and the helper), so
        # its Continue refuses it before the name is asked.
        form = start_schedule(page)
        form.get_by_label("Scene", exact=True).select_option(SCENE_ID)
        schedule_continue(page, "When")
        # A 25 h window repeated daily: each would overlap the next.
        form.get_by_label("Window start", exact=True).fill("2027-03-01T18:00")
        form.get_by_label("Window end", exact=True).fill("2027-03-02T19:00")
        form.get_by_role("button", name="Advanced", exact=True).click()
        _windows(page).get_by_label("Number of windows", exact=True).fill("3")
        form.get_by_role("button", name="Continue", exact=True).click()
        expect(form.get_by_role("alert")).to_contain_text(
            "Each window must end before the next starts.")

        # An id whose window ids pass 128 characters.
        form.get_by_label("Window end", exact=True).fill("2027-03-01T20:00")
        schedule_continue(page, "Review")
        form.get_by_label("Program name", exact=True).fill("Marathon")
        form.get_by_role("button", name="Change", exact=True).click()
        form.get_by_label("Id", exact=True).fill("x" * 127)
        form.get_by_role("button", name="Add separate windows", exact=True).click()
        expect(form.get_by_role("alert")).to_contain_text(
            f"Name too long: {'x' * 127}-3 must be at most 128 characters.")
        assert bodies == []


def test_the_windows_helper_retries_only_the_unconfirmed_windows(page, registry):
    """One window's PUT answers 500 without reaching Central: it is "not
    confirmed", never "not created", and adding again sends only that window —
    the confirmed ones are neither resent nor read as collisions."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        author_scene(page, SCENE_ID, SOURCE, (VALID_FRAME,))
        programs = page.get_by_role("region", name="Programs", exact=True)
        form = schedule_program(page, PROGRAM_ID, SCENE_ID, *WINDOW, windows=3, submit=False)
        answer_first(page, f"**/v1/operator/programs/{PROGRAM_ID}-2", lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"error": "internal"}'))
        bodies = _program_puts(page)
        send = form.get_by_role("button", name="Add separate windows", exact=True)
        send.click()
        expect(programs.get_by_role("status")).to_have_text(
            f"Created 2 of 3 separate Programs. Not confirmed: {PROGRAM_ID}-2; Central did "
            "not answer. Add separate windows again to send only these.")
        # Bead 4: the flow stays on Review with the draft, so it can be sent again.
        expect(send).to_be_enabled()
        assert page.evaluate("window.location.hash") == "#/schedule/new/review"
        # The cards, shown beside the kept draft, list the stored windows only.
        go(page, "schedule")
        expect(programs.get_by_label(f"Program {PROGRAM_ID}-1", exact=True)).to_be_visible()
        expect(programs.get_by_label(f"Program {PROGRAM_ID}-3", exact=True)).to_be_visible()
        expect(programs.get_by_label(f"Program {PROGRAM_ID}-2", exact=True)).to_have_count(0)
        programs.get_by_role("button", name="Resume draft (Draft)", exact=True).click()
        assert page.evaluate("window.location.hash") == "#/schedule/new/review"

        bodies.clear()
        send.click()
        expect(programs.get_by_role("status")).to_have_text("Created 3 separate Programs.")
        assert [body["program_id"] for body in bodies] == [f"{PROGRAM_ID}-2"]
        for index in (1, 2, 3):
            expect(programs.get_by_label(f"Program {PROGRAM_ID}-{index}", exact=True)).to_be_visible()


def test_an_invalid_window_count_gives_a_reason_and_is_never_reset(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        author_scene(page, SCENE_ID, SOURCE, (VALID_FRAME,))
        bodies = _program_puts(page)
        form = schedule_program(page, PROGRAM_ID, SCENE_ID, *WINDOW, submit=False)
        # Bead 4: the count is When's (under Advanced); its Continue refuses it.
        form.get_by_role("button", name="Change Number of windows", exact=True).click()
        count = _windows(page).get_by_label("Number of windows", exact=True)
        expect(count).to_be_focused()
        count.fill("0")
        expect(count).to_have_accessible_description("Between 1 and 60 windows.")
        form.get_by_role("button", name="Continue", exact=True).click()
        expect(count).to_have_value("0")
        expect(count).to_be_focused()
        count.fill("61")
        expect(count).to_have_accessible_description("Between 1 and 60 windows.")
        assert bodies == []


def test_the_source_form_sends_favourites_and_a_capture_window(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        # Pass 5: the connection is its own step (no Source yet, nothing reported), and the
        # filters are "Narrow it down".
        form = start_source(page)
        form.get_by_label("Connection name", exact=True).fill("fixture-library")
        source_continue(page, "Tags")
        source_continue(page, "Narrow")
        form.get_by_label("Favourites", exact=True).select_option("only")
        until = form.get_by_label("Dated until", exact=True)
        form.get_by_label("Dated from", exact=True).fill("2024-01-01")
        until.fill("2023-06-01")
        expect(until).to_have_accessible_description(
            re.compile("'Dated until' must be after 'Dated from'."))
        until.fill("2025-01-01")
        source_continue(page, "Name")
        form.get_by_label("Source name", exact=True).fill(NEW_SOURCE)
        source_continue(page, "Review")
        with page.expect_response(
            lambda r: r.url.endswith("/v1/operator/source-names/" + quote(NEW_SOURCE, safe=""))
            and r.request.method == "PUT"
        ) as info:
            form.get_by_role("button", name="Save Source", exact=True).click()
        assert info.value.status == 200
        body = info.value.request.post_data_json
        assert body["favorites"] is True
        assert body["captured_from"] == _epoch(page, "2024-01-01T00:00")
        assert body["captured_until"] == _epoch(page, "2025-01-01T00:00")


def test_a_dismissed_confirm_whose_opener_is_gone_moves_focus_to_the_successor(page, registry):
    """useConfirm (ConfirmAction.jsx): a dialog closed with no result goes back to its
    opener — or, when a poll removed the opener, to the owner's declared successor
    (the Runs region), never to the page body."""
    _seed(registry)
    queue = _seed_source(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _scene(SCENE_ID))
    runtime.command("activate", SCENE_ID, "gone-act", registry.clock.utc())
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now", paused_at=registry.clock.utc())
        runs = page.get_by_role("region", name="Runs", exact=True)
        opener = runs.get_by_role("button", name=re.compile(r"^Cancel run "))
        opener.click()
        dialog = page.get_by_role("dialog", name=f"Cancel the Run of {SCENE_ID}?")
        expect(dialog).to_be_visible()

        # Another operator cancels it; the next poll drops the live row and its opener.
        run_id = next(run.run_id for run in runtime.read().project(registry.clock.utc()).runs
                      if run.scene_id == SCENE_ID)
        runtime.command("cancel", run_id, registry.clock.utc())
        page.clock.run_for(5000)
        expect(runs.get_by_text("No Run is running.", exact=True)).to_be_visible()
        expect(opener).to_have_count(0)

        dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(runs).to_be_focused()


# Pass 2 slice 3B (docs/operator-console-ux-pass2-showrunner.md §13): Scene view and lossless edit.

# A stored id the name rule would rewrite ("lobby-loop-v2"): Edit must keep it.
STORED_ID = "Lobby_Loop.v2"


def _console_scene(scene_id, frame=VALID_FRAME, **fields):
    """A live Scene in exactly the shape the console saves: only the fields it sends
    are set, so every other field is stored at its model default."""
    return Scene(scene_id=scene_id, **{"loop": True, **fields}, contributions=(Contribution(
        target=f"frame:{frame}", role=frame, source_refs=(SOURCE,), retain_on_expiry=True),))


def _scene_row(page, scene_id):
    """A Scene's card (bead 2: a summary card, no longer a disclosure to open)."""
    return page.get_by_role("region", name="Scenes", exact=True).get_by_label(
        f"Scene {scene_id}", exact=True)



def test_editing_a_scene_replaces_it_under_its_stored_id_at_the_next_revision(page, registry):
    """§13: a Scene saved with its other fields at their defaults stays editable
    (mutation probe: compare without filling defaults); Edit shows the stored id and
    never re-derives it (mutation probe: the name rule would send lobby-loop-v2); Replace
    is confirmed and sends revision + 1.

    Bead 2: Edit opens the flow at Review, which lists the stored values (they used to be
    the filled form's fields); "Change" opens the value's step and focuses it, and
    Continue returns to Review. After Replace the flow returns to the cards (#/scenes)
    instead of an emptied form."""
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _console_scene(STORED_ID))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        row = _scene_row(page, STORED_ID)
        expect(row).to_contain_text("live from holiday")
        expect(row.get_by_text("Revision", exact=True)).to_have_count(0)
        expect(row).to_contain_text("no Program")
        row.get_by_role("button", name=f"Edit Scene {STORED_ID}", exact=True).click()

        form = _scenes_form(page)
        expect(form).to_contain_text(f"Editing {STORED_ID}. Its name stays the same.")
        assert current_hash(page) == f"#/scenes/{STORED_ID}/edit/review"
        expect(form.get_by_label("Scene name", exact=True)).to_have_count(0)
        # The stored values, exactly, where each is asked: every Change link opens its
        # step, and Continue returns to Review.
        form.get_by_role("button", name="Change Photos", exact=True).click()
        expect(form.get_by_label("Source", exact=True)).to_have_value(SOURCE)
        scene_continue(page, "Review")
        form.get_by_role("button", name="Change Frames", exact=True).click()
        expect(form.get_by_label(f"Target frame {VALID_FRAME}", exact=True)).to_be_checked()
        expect(form.get_by_label(f"Target frame {INVALID_FRAME}", exact=True)).not_to_be_checked()
        scene_continue(page, "Review")
        form.get_by_role("button", name="Change Keep playing until the Program ends",
                         exact=True).click()
        expect(form.get_by_label("Keep playing until the Program ends", exact=True)
               ).to_be_checked()
        scene_continue(page, "Review")
        form.get_by_role("button", name="Change Seconds per cycle", exact=True).click()
        seconds = form.get_by_label("Seconds per cycle", exact=True)
        expect(seconds).to_be_focused()
        seconds.fill("45")
        scene_continue(page, "Review")
        form.get_by_role("button", name="Replace Scene", exact=True).click()

        dialog = page.get_by_role("dialog", name=f"Replace Scene {STORED_ID}?")
        expect(dialog).to_contain_text(
            "Runs already going keep what they started with; Programs that start later use "
            "the saved changes.")
        with page.expect_response(
            lambda r: "/v1/operator/scenes/" in r.url and r.request.method == "PUT"
        ) as info:
            dialog.get_by_role("button", name="Confirm replace", exact=True).click()
        assert info.value.url.endswith("/v1/operator/scenes/" + quote(STORED_ID, safe=""))
        assert info.value.status == 200
        body = info.value.request.post_data_json
        assert (body["scene_id"], body["revision"], body["cycle_seconds"]) == (STORED_ID, 2, 45)
        expect(page.get_by_role("region", name="Scenes", exact=True).get_by_role(
            "status")).to_have_text(f"Scene {STORED_ID} saved.")
        expect(_scene_row(page, STORED_ID).get_by_text("Revision", exact=True)).to_have_count(0)
        assert current_hash(page) == "#/scenes"


def test_a_scene_the_console_cannot_author_withholds_edit_with_the_reason(page, registry):
    _seed(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _console_scene("plain"))
    runtime.command("set_scene", Scene(
        scene_id="evening", loop=True,
        contributions=_console_scene("x").contributions,
        children=(Child(scene=_console_scene("intro")),)))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "scenes")
        # Not vacuous: a Scene the console can author offers Edit.
        expect(_scene_row(page, "plain").get_by_role("button", name="Edit Scene plain")).to_be_visible()
        evening = _scene_row(page, "evening")
        expect(evening).to_contain_text(
            "Edit unavailable: Uses features the console can't edit yet (child Scenes, see-through "
            "photos, or different settings per Frame).")
        expect(evening.get_by_role("button", name="Edit Scene evening")).to_have_count(0)


def test_scene_cards_summarize_inline_and_outro_frames_and_media(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _console_scene("plain"))
    runtime.command("set_scene", Scene(
        scene_id="layered", loop=True, outro_seconds=5,
        children=(Child(scene=Scene(scene_id="inline", contributions=(
            Contribution(target=f"frame:{INVALID_FRAME}", source_refs=(SOURCE,)),
        ))),),
        outro_contributions=(Contribution(
            target=f"frame:{VALID_FRAME}", asset_refs=("chosen-still",)),),
    ))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        layered = _scene_row(page, "layered")
        expect(layered).to_contain_text("live from holiday")
        expect(layered).to_contain_text("authored: 1 chosen item")
        expect(layered.get_by_text(re.compile(r"^valid-frame:"))).to_have_count(1)
        expect(layered.get_by_text(re.compile(r"^invalid-frame:"))).to_have_count(1)
        expect(layered).not_to_contain_text("no media")
        expect(layered).not_to_contain_text("Frames none")
        expect(layered.get_by_role("button", name="Edit Scene layered")).to_have_count(0)
        plain = _scene_row(page, "plain")
        expect(plain).to_contain_text("live from holiday")
        expect(plain).not_to_contain_text("authored:")


def _put_authored(page, origin, scene_id, choices):
    """Store an authored Scene through the public route, as the console saves one."""
    scene = {"scene_id": scene_id, "revision": 1, "cycle_seconds": 30, "loop": True,
             "contributions": [{"target": f"frame:{frame}", "role": frame, "kind": "media",
                                "asset_refs": [asset], "retain_on_expiry": True}
                               for frame, asset in choices.items()]}
    response = page.request.put(
        origin + f"/v1/operator/scenes/{scene_id}/authored",
        headers={"Authorization": "Bearer " + ADMIN},
        data={"scene": scene, "source_ref": SOURCE, "asset_ids": sorted(set(choices.values()))})
    assert response.status == 200, response.text()


def _drop_member(registry, asset_id):
    """The upstream library no longer holds this item (a later refresh dropped it)."""
    with registry.db.transaction() as conn:
        conn.execute("DELETE FROM source_members WHERE source_ref=%s AND asset_id=%s",
                     (SOURCE, asset_id))


def test_editing_an_authored_scene_preselects_its_items_that_are_still_candidates(page, registry):
    _seed(registry)
    portrait_a, portrait_b, landscape = _authored_photos(registry)
    queue = _seed_source(registry, (portrait_a, portrait_b, landscape))
    a, b = portrait_a.asset.asset_id, portrait_b.asset.asset_id
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _put_authored(page, origin, AUTHORED_SCENE_ID, {VALID_FRAME: a, INVALID_FRAME: b})
        _drop_member(registry, b)
        connect(page, origin, "scenes")
        row = _scene_row(page, AUTHORED_SCENE_ID)
        expect(row).to_contain_text("authored: 2 chosen items")
        row.get_by_role("button", name=f"Edit Scene {AUTHORED_SCENE_ID}", exact=True).click()

        # Bead 2: the Kind is on Review ("Hand-picked per frame", the old "Authored
        # per-frame"), and the choosers are on the Media per frame step.
        form = _scenes_form(page)
        answers = form.get_by_label("Your answers", exact=True)
        expect(answers).to_contain_text("Hand-picked per frame")
        # An authored Scene does not store its Source: the operator picks it.
        expect(answers).to_contain_text("Not chosen")
        page.get_by_role("navigation", name="Steps", exact=True).get_by_role(
            "button", name="Photos", exact=True).click()
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        scene_continue(page, "Media per frame")
        expect(form.get_by_label(f"Media for frame {VALID_FRAME}", exact=True)).to_have_value(a)
        # b left the Source, so it is no longer a candidate and is not kept.
        invalid_choice = form.get_by_label(f"Media for frame {INVALID_FRAME}", exact=True)
        expect(invalid_choice.get_by_role("option")).to_have_count(2)
        expect(invalid_choice).to_have_value("")
        invalid_choice.select_option(a)
        scene_continue(page, "Playback")
        scene_continue(page, "Review")
        form.get_by_role("button", name="Replace Scene", exact=True).click()
        with page.expect_response(
            lambda r: r.url.endswith(f"/scenes/{AUTHORED_SCENE_ID}/authored")
            and r.request.method == "PUT"
        ) as info:
            page.get_by_role("dialog").get_by_role("button", name="Confirm replace").click()
        assert info.value.status == 200
        body = info.value.request.post_data_json
        assert (body["scene"]["revision"], body["asset_ids"]) == (2, [a])


def test_authored_save_refusals_are_said_in_plain_words(page, registry):
    """§13: the authored PUT's two 409s, each as its sentence."""
    _seed(registry)
    portrait_a, portrait_b, landscape = _authored_photos(registry)
    queue = _seed_source(registry, (portrait_a, portrait_b, landscape))
    a, b = portrait_a.asset.asset_id, portrait_b.asset.asset_id
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        scenes = page.get_by_role("region", name="Scenes", exact=True)

        with registry.db.transaction() as conn:  # the last refresh failed
            conn.execute("UPDATE media_sources SET status='unavailable' WHERE source_ref=%s",
                         (SOURCE,))
        form = author_scene(page, AUTHORED_SCENE_ID, SOURCE, {VALID_FRAME: a}, submit=False)
        with page.expect_response(lambda r: r.url.endswith("/authored")) as info:
            form.get_by_role("button", name="Save Scene", exact=True).click()
        assert (info.value.status, info.value.json()["error"]) == (409, "source_not_fresh")
        expect(scenes).to_contain_text(
            "Could not save Scene. The Source's last refresh failed; authored choices can be "
            "saved once it succeeds.")

        with registry.db.transaction() as conn:
            conn.execute("UPDATE media_sources SET status='ok' WHERE source_ref=%s", (SOURCE,))
        # Bead 2: the chooser is on the Media per frame step; Continue returns to Review.
        form.get_by_role("button", name="Change Media per frame", exact=True).click()
        form.get_by_label(f"Media for frame {VALID_FRAME}", exact=True).select_option(b)
        scene_continue(page, "Review")
        _drop_member(registry, b)
        with page.expect_response(lambda r: r.url.endswith("/authored")) as info:
            form.get_by_role("button", name="Save Scene", exact=True).click()
        assert (info.value.status, info.value.json()["error"]) == (409, "authored_asset_not_member")
        expect(scenes).to_contain_text(
            "Could not save Scene. That item is no longer in the Source; choose again.")
        # The choosers read their candidates again: b is no longer offered.
        form.get_by_role("button", name="Change Media per frame", exact=True).click()
        choice = form.get_by_label(f"Media for frame {VALID_FRAME}", exact=True)
        expect(choice.get_by_role("option")).to_have_count(2)
        expect(choice).to_have_value("")


def test_two_editors_replacing_one_scene_the_second_ends_changed(page, registry):
    """§13, Question 4 flipped: both editors open revision 1; the first Replace lands
    as revision 2, so Central refuses the second (409 scene_revision_conflict) and the
    dialog ends "changed" with the first editor's Scene kept. Mutation probe: drop the
    guard in Runtime.set_scene (the second silently replaces the first)."""
    _seed(registry)
    queue = _seed_source(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _console_scene(SCENE_ID))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes", paused_at=registry.clock.utc())
        _scene_row(page, SCENE_ID).get_by_role("button", name=f"Edit Scene {SCENE_ID}").click()
        form = _scenes_form(page)
        form.get_by_role("button", name="Change Seconds per cycle", exact=True).click()
        form.get_by_label("Seconds per cycle", exact=True).fill("45")
        scene_continue(page, "Review")

        # The other editor replaces it meanwhile, as the console saves; no poll has
        # shown it here yet.
        first = _console_scene(SCENE_ID, revision=2, cycle_seconds=20).model_dump(mode="json")
        response = page.request.put(origin + f"/v1/operator/scenes/{SCENE_ID}",
                                    headers={"Authorization": "Bearer " + ADMIN}, data=first)
        assert response.status == 200, response.text()
        form.get_by_role("button", name="Replace Scene", exact=True).click()
        dialog = page.get_by_role("dialog", name=f"Replace Scene {SCENE_ID}?")
        with page.expect_response(
            lambda r: r.url.endswith(f"/v1/operator/scenes/{SCENE_ID}") and r.request.method == "PUT"
        ) as info:
            dialog.get_by_role("button", name="Confirm replace", exact=True).click()
        assert (info.value.status, info.value.json()) == (409, {"error": "scene_revision_conflict"})
        expect(dialog.get_by_role("status")).to_have_text(
            "This Scene was changed since you opened it; nothing was replaced. "
            "Review now offers Reload.")
        expect(dialog.get_by_role("button", name="Confirm replace")).to_have_count(0)
        stored = _runtime(registry).read().export_state()["scenes"][SCENE_ID]
        assert (stored["revision"], stored["cycle_seconds"]) == (2, 20)
        # The refresh after the write shows revision 2: Replace is withheld, so closing
        # the dialog moves focus to Reload rather than to the disabled Replace.
        expect(form.get_by_role("button", name="Replace Scene", exact=True)).to_be_disabled()
        dialog.get_by_role("button", name="Close", exact=True).click()
        expect(form.get_by_role("button", name="Reload", exact=True)).to_be_focused()


# Pass 2 slice 3B (§14): the media pipeline and "why nothing new?".


def _set_source(registry, ref, **columns):
    """A configured Source whose served refresh columns are set directly: the facts a
    worker's refreshes would have left (no worker runs in these checks)."""
    spec = columns.pop("spec", {})
    MediaRepository(registry.db, registry.clock, queue=RecordingMediaQueue(), times=ProcessTransactionClock(registry.clock)).configure_source(
        SourceSpec(source_ref=ref, connection_ref="fixture-library", **spec))
    if columns:
        assignments = ",".join(f"{name}=%s" for name in columns)
        values = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in columns.values()]
        with registry.db.transaction() as conn:
            conn.execute(f"UPDATE media_sources SET {assignments} WHERE source_ref=%s",
                         (*values, ref))


def _utc(year):
    return datetime(year, 1, 1, tzinfo=UTC).timestamp()


def _pipeline(page):
    return page.get_by_role("region", name="Media pipeline", exact=True)


@pytest.mark.browser_context_args(timezone_id="UTC")
def test_the_media_pipeline_states_each_source(page, registry):
    _seed(registry)
    now = registry.clock.utc()
    good = {"valid": 790, "discovered": 800, "pending": 4, "rejected": 6}
    _set_source(registry, "awaiting:1")
    _set_source(registry, "fresh:1", next_refresh=now + 30, last_success=now - 60, status="ok",
                counts=good, spec={"favorites": True, "media_types": ("image",),
                                   "captured_from": _utc(2024), "captured_until": _utc(2025)})
    _set_source(registry, "failing:1", next_refresh=now + 30, last_success=now - 7200,
                status="unavailable", diagnostics=[{"code": "upstream_unavailable"}])
    _set_source(registry, "empty:1", next_refresh=now + 30, last_success=now, status="ok",
                counts={"valid": 0})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "now", paused_at=now)
        pipeline = _pipeline(page)

        def state(ref):
            return pipeline.get_by_label(f"Refresh of {ref.rsplit(':', 1)[0]}", exact=True)
        expect(state("awaiting:1")).to_contain_text("Awaiting refresh")
        expect(state("fresh:1")).to_contain_text(
            "Your photo library last reported 1 min ago · the media worker accepted 790 in that refresh"
            " · 10 items pending or rejected · favourites only · photos only · dated 2024")
        expect(state("fresh:1")).to_contain_text("found 800 · valid 790 · pending 4 · rejected 6")
        expect(state("failing:1")).to_contain_text("Your photo library is unreachable · last good refresh 2 h ago")
        expect(state("failing:1")).to_contain_text("upstream unavailable")
        expect(state("empty:1")).to_contain_text("nothing valid in the last refresh")

        registry.clock.advance(30 + 125 + 240)  # every refresh is 6 min past due
        page.clock.run_for(5000)
        expect(state("fresh:1")).to_contain_text("Refresh overdue by 6 min")
        expect(state("awaiting:1")).to_contain_text("Awaiting refresh")
        expect(state("failing:1")).to_contain_text("Your photo library is unreachable")
        copy = pipeline.inner_text()
        for claim in ("LIVE", "online", "connected", "Immich"):
            assert claim not in copy


def test_the_media_pipeline_states_each_worker_state(page, registry):
    _seed(registry)
    repository = MediaRepository(registry.db, registry.clock, queue=RecordingMediaQueue(), times=ProcessTransactionClock(registry.clock))
    repository.health()  # the settings row, as Central's first media read makes it
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "now", paused_at=registry.clock.utc())
        worker = _pipeline(page).get_by_label("Media worker", exact=True)
        expect(worker).to_have_text("never checked in")
        expect(worker).to_have_class(re.compile(r"\bhealth--alarm\b"))

        repository.worker_status("storage_pressure")
        page.clock.run_for(5000)
        expect(worker).to_have_text("reported: storage is full")
        expect(_pipeline(page)).to_contain_text("preparing 0 · waiting 0 · failed 0 · cache 0 of")

        repository.worker_status(None)
        registry.clock.advance(20)
        page.clock.run_for(5000)
        expect(worker).to_have_text(re.compile(
            r"^Media worker last reported 20 s ago · preparing 0 · waiting 0 · failed 0 · cache 0 of 4\.3 GB$"))
        expect(worker).to_have_class(re.compile(r"\bhealth--ok\b"))

        registry.clock.advance(2 * 300 + 60 - 20 + 1)
        page.clock.run_for(5000)
        expect(worker).to_have_text("quiet for 11 min")


def test_media_ages_are_taken_against_the_media_read_time_not_the_inventory_read_time(page, registry):
    """G11: media times are the database's, so the worker fact ages against `media.read_at`.
    The stub moves `inventory.read_at` (Central's process clock) an hour away from it."""
    _seed(registry)
    repository = MediaRepository(registry.db, registry.clock, queue=RecordingMediaQueue(),
                                 times=ProcessTransactionClock(registry.clock))
    repository.worker_status(None)
    seen = registry.clock.utc()

    def skewed(route):
        response = route.fetch()
        body = response.json()
        body["media"]["read_at"] = seen + 30
        body["inventory"]["read_at"] = seen + 3600
        route.fulfill(response=response, json=body)

    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "now", paused_at=registry.clock.utc())
        page.route("**/v1/operator/snapshot", skewed)
        page.clock.run_for(5000)
        worker = _pipeline(page).get_by_label("Media worker", exact=True)
        expect(worker).to_have_text(re.compile(r"^Media worker last reported 30 s ago · "))
        expect(worker).to_have_class(re.compile(r"\bhealth--ok\b"))


def _why_chain(page, frame_id=VALID_FRAME):
    """Open "Why nothing new?" on `frame_id`'s row of the Why group (bead 5: a disclosure
    per frame, where a "Frame for why" chooser used to show both explanations at once)."""
    why = page.get_by_role("region", name="Runs", exact=True).get_by_role(
        "group", name="Why", exact=True)
    why.get_by_role("button", name=f"Why nothing new? {frame_id}", exact=True).click()
    return why.get_by_role("group", name=f"Why nothing new on {frame_id}?", exact=True)


def _run_now(registry, scene):
    """Store a Scene and start a Run of it now."""
    runtime = _runtime(registry)
    runtime.command("set_scene", scene)
    runtime.command("activate", scene.scene_id, "live-act", registry.clock.utc())


def _stop(chain):
    return chain.get_by_role("listitem").filter(has_text="Stops here.")


def test_why_nothing_new_stops_at_a_one_cycle_run_that_ended_and_its_still(page, registry):
    """§14 step 2: nothing is intended any more because the one-cycle Run ended; the
    frame keeps its last still if it was a photo (retain_on_expiry). Mutation probes:
    drop the retained-still words; render the chain inside the ranked list."""
    _seed(registry)
    queue = _seed_source(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _console_scene(SCENE_ID, loop=False))
    runtime.command("activate", SCENE_ID, "once-act", registry.clock.utc())
    registry.clock.advance(45)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        chain = _why_chain(page)
        expect(_stop(chain)).to_have_count(1)
        expect(_stop(chain)).to_contain_text(re.compile(
            rf"Run ended\? {SCENE_ID}'s Run ended at \d\d:\d\d(:\d\d)? \S+ after one cycle; if its "
            r"last item was a photo, the frame keeps that still \(a video is not kept\)\."))
        expect(chain.get_by_role("listitem").first).to_contain_text(
            "On top: nothing · no Run puts a layer on this Frame now")
        # The chain is its own group: the ranked list is not in it.
        expect(chain.get_by_role("list", name="Contribution precedence")).to_have_count(0)


def test_why_nothing_new_stops_at_an_authored_scene(page, registry):
    _seed(registry)
    portrait_a, portrait_b, landscape = _authored_photos(registry)
    queue = _seed_source(registry, (portrait_a, portrait_b, landscape))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _put_authored(page, origin, AUTHORED_SCENE_ID, {VALID_FRAME: portrait_a.asset.asset_id})
        _runtime(registry).command("activate", AUTHORED_SCENE_ID, "fixed-act", registry.clock.utc())
        connect(page, origin, "now")
        chain = _why_chain(page)
        expect(_stop(chain)).to_contain_text(
            "Authored? Fixed, hand-picked media; new photos never appear by design.")
        expect(chain.get_by_role("button", name="Check this frame")).to_have_count(0)
        # Ranked beside it, unchanged: one layer.
        why = page.get_by_role("region", name="Runs", exact=True).get_by_role("group", name="Why")
        why.get_by_role("button", name=f"Why? {VALID_FRAME}", exact=True).click()
        expect(why.get_by_role("list", name="Contribution precedence").get_by_role(
            "listitem")).to_have_count(1)


def test_check_this_frame_counts_as_the_planner_does(page, registry, tmp_path):
    """§14 step 5: the frame's candidates tallied by the standing Central serves —
    usable, still preparing, failed to prepare. The candidates route's count includes a
    failed preparation (mutation probe: serve "usable" for every candidate)."""
    _seed(registry)
    portrait_a, portrait_b, landscape = _authored_photos(registry)
    queue = _seed_source(registry, (portrait_a, portrait_b, landscape))
    now = registry.clock.utc()
    with registry.db.transaction() as conn:  # the refresh found them usable
        conn.execute("UPDATE media_sources SET counts=%s WHERE source_ref=%s",
                     (Jsonb({"valid": 3, "discovered": 3}), SOURCE))
    _run_now(registry, _console_scene(SCENE_ID))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now", paused_at=now)
        chain = _why_chain(page)
        expect(chain).to_contain_text(
            "The Source holiday: Your photo library last reported 0 s ago · the media worker accepted 3 "
            "in that refresh.")
        chain.get_by_role("button", name="Check this frame", exact=True).click()
        expect(_stop(chain)).to_contain_text(
            "Check this frame Nothing usable yet: 2 still preparing.")

        repository = MediaRepository(registry.db, registry.clock, queue=queue, times=ProcessTransactionClock(registry.clock))
        repository.set_recipe("a" * 64)  # the worker checks in with its recipe
        repository.request_acquisitions((AcquisitionRequest(
            asset_id=portrait_a.asset.asset_id, assignment_ids=("a",), earliest_start=now),))
        publish_photo(MediaStore(repository, tmp_path / "media"), portrait_a)
        repository.request_acquisitions((AcquisitionRequest(
            asset_id=portrait_b.asset.asset_id, assignment_ids=("b",), earliest_start=now),))
        with registry.db.transaction() as conn:
            conn.execute("UPDATE media_jobs SET state='failed',failure_code='asset_missing' "
                         "WHERE asset_id=%s", (portrait_b.asset.asset_id,))
        response = page.request.get(
            origin + f"/v1/operator/sources/{quote(SOURCE, safe='')}/candidates"
            f"?frame_id={VALID_FRAME}", headers={"Authorization": "Bearer " + ADMIN})
        assert response.json()["count"] == 2  # the route counts the failed one too
        assert sorted(c["standing"] for c in response.json()["candidates"]) == [
            "failed_to_prepare", "usable"]
        page.clock.run_for(5000)  # the next poll serves the worker's check-in
        chain.get_by_role("button", name="Check again", exact=True).click()
        check = chain.get_by_role("listitem").filter(has_text="Check this frame")
        expect(check).to_contain_text("1 usable · 1 failed to prepare.")
        expect(check).not_to_contain_text("Stops here.")
        expect(_stop(chain)).to_contain_text("Frame health")


def test_check_this_frame_skips_a_failing_source_and_counts_a_shared_item_once(page, registry):
    """§14 step 5 across a Scene's Sources, as planning pools them (planner.py `_pool`):
    a Source whose last refresh failed contributes nothing, and an item two Sources
    share counts once. Mutation probes: keep the failing Source; count per Source."""
    _seed(registry)
    portrait_a, portrait_b, _landscape = _authored_photos(registry)
    queue = _seed_source(registry, (portrait_a, portrait_b))
    a, b = portrait_a.asset.asset_id, portrait_b.asset.asset_id
    now = registry.clock.utc()
    _drop_member(registry, b)  # SOURCE holds a
    fresh = dict(next_refresh=now + 30, last_success=now, counts={"valid": 1})
    _set_source(registry, "shared:1", status="ok", **fresh)
    _set_source(registry, "down:1", status="unavailable", **fresh)
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO source_members VALUES(%s,%s),(%s,%s)",
                     ("shared:1", a, "down:1", b))
    _run_now(registry, Scene(scene_id=SCENE_ID, loop=True, contributions=(Contribution(
        target=f"frame:{VALID_FRAME}", source_refs=(SOURCE, "shared:1", "down:1")),)))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now", paused_at=now)
        chain = _why_chain(page)
        chain.get_by_role("button", name="Check this frame", exact=True).click()
        expect(_stop(chain)).to_contain_text(
            "Check this frame Nothing usable yet: 1 still preparing.")


@pytest.mark.browser_context_args(timezone_id="Europe/London")
def test_a_capture_window_across_a_dst_change_names_its_last_whole_day(page, registry):
    """§7 "dated until" is exclusive: a window ending at local midnight on 1 April
    names 31 March, though that day was 23 h long (mutation probe: until - 86400)."""
    _seed(registry)
    london = ZoneInfo("Europe/London")
    start = datetime(2024, 3, 1, tzinfo=london).timestamp()
    until = datetime(2024, 4, 1, tzinfo=london).timestamp()
    _set_source(registry, "spring:1", spec={"captured_from": start, "captured_until": until})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "now")
        expect(_pipeline(page).get_by_label("Refresh of spring", exact=True)).to_contain_text(
            re.compile(r"dated (1 Mar 2024 to 31 Mar 2024|Mar 1, 2024 to Mar 31, 2024)\b"))


def test_the_chooser_says_dated_and_readiness_and_waits_while_loading(page, registry):
    """§14 labels and §6 "Loading compatible media…" (a state, never "No compatible
    media" while the read is in flight); "(2)" only for a remaining duplicate."""
    _seed(registry)
    now = registry.clock.utc()
    photos = (*_authored_photos(registry), public_photo(number=4, width=108, height=192,
                                                        captured_at=now))
    queue = _seed_source(registry, photos)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        # Bead 2: the chooser is on the Media per frame step, whose Continue (the old
        # Save) reports the loading state.
        form = start_scene(page, hand_picked=True)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        reads = RequestGate(page, "**/candidates*")
        reads.holding = True
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        reads.wait_held()
        scene_continue(page, "Media per frame")
        choice = form.get_by_label(f"Media for frame {VALID_FRAME}", exact=True)
        expect(choice.get_by_role("option")).to_have_text(["Loading compatible media…"])
        form.get_by_role("button", name="Continue", exact=True).click()
        expect(form.get_by_role("alert")).to_contain_text("Loading compatible media…")

        reads.holding = False
        reads.release()
        expect(choice.get_by_role("option").first).to_have_text("Choose compatible media")
        labels = sorted(choice.get_by_role("option").all_inner_texts()[1:])
        taken = r" · dated .+ \d\d:\d\d(:\d\d)? \S+ · preparing"
        assert len(labels) == 3, labels
        assert re.fullmatch(r"Photo 108×192" + taken, labels[0]), labels
        assert re.fullmatch(r"Photo 108×192" + taken + r" \(2\)", labels[1]), labels
        assert re.fullmatch(r"Photo 120×200" + taken, labels[2]), labels
