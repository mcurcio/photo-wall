"""Behavioral browser checks for the Showrunner shell at /console (Bead 12).

Reuses the existing operator browser harness (real Chromium against the
production create_app on an ephemeral loopback listener, the disposable-schema
`registry` fixture, and the autouse `page_errors` guard). Frames are seeded
through the registry so the console renders real /inventory state.

Every assertion is BEHAVIORAL — role/text/visible state — never SVG coordinates
or DOM structure (design §1c). This is a /console test file; the legacy flat-page
tests were retired at the Bead 17 cutover (this file re-hosts their Showrunner
content on the redesign).

The R4 rule (design §2 R4, J4) is the load-bearing check: the Commissioning
facet — the home of every Display CONTROL — is UNREACHABLE in Showrunner mode.
This is the now-fully-enforceable version of Bead 4's placeholder probe: with
Showrunner mode existing, "Commissioning is Wall-only" is a real, red-able
assertion.
"""

import os
import re
from urllib.parse import quote

import pytest
from media_queue import RecordingMediaQueue
from operator_harness import (
    RequestGate,
    operator_server,
    pause_page_clock,
    report_readiness,
    tile_health,
)
from playwright.sync_api import expect
from test_registry import ADMIN, enroll

from central.catalog import CatalogSnapshot
from central.media_repository import MediaRepository
from central.registry import FrameCreate
from central.runtime import Child, Contribution, Program, Scene
from central.runtime_store import RuntimeStore
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
    return id_a["player_id"], id_b["player_id"]


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


def test_showrunner_frame_health_badges_match_the_wall(page, registry):
    for player_id in _seed(registry):
        report_readiness(registry, player_id)
    registry.clock.advance(3)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        # The wall's labels, read first so the show layer can be held to them.
        valid_label = "Last heard 3 s ago"
        invalid_label = "Needs commissioning"
        expect(tile_health(page, VALID_FRAME)).to_have_accessible_name(valid_label)
        expect(tile_health(page, INVALID_FRAME)).to_have_accessible_name(invalid_label)
        _to_showrunner(page)

        health = page.get_by_role("group", name="Frame health", exact=True)
        expect(health).to_be_visible()

        # Each Frame's health renders as a STATUS badge, located by its accessible
        # identity label, with exactly the label the wall shows — a committed,
        # heard frame reads as heard; a bound-only frame needs commissioning (a
        # to-do, never the alarm colour).
        expect(
            health.get_by_label(f"Frame {VALID_FRAME}: {valid_label}", exact=True)
        ).to_be_visible()
        invalid = health.get_by_label(f"Frame {INVALID_FRAME}: {invalid_label}", exact=True)
        expect(invalid).to_be_visible()
        expect(invalid).to_have_class(re.compile(r"\bhealth--todo\b"))


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


# Bead G2 — SR-source-config: CREATE a Source from the console (content-parity
# GAP 2). A distinct name:rev the seeded SOURCE does not use, so its appearance
# below is caused by THIS create, not the fixture.
NEW_SOURCE = "spring:1"


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
        _connect(page, origin)
        _to_showrunner(page)

        sources = page.get_by_role("region", name="Sources", exact=True)
        expect(sources).to_be_visible()
        # Not vacuously true: the new Source does not exist before the create.
        expect(sources.get_by_text(NEW_SOURCE, exact=True)).to_have_count(0)

        form = sources.get_by_role("form", name="Configure a Source", exact=True)
        form.get_by_label("Source name and revision", exact=True).fill(NEW_SOURCE)
        form.get_by_label("Connection name", exact=True).fill("fixture-library")
        form.get_by_label("Media type", exact=True).select_option("image")

        with page.expect_response(
            lambda r: r.url.endswith("/v1/operator/sources/" + quote(NEW_SOURCE, safe=""))
            and r.request.method == "PUT"
        ) as info:
            form.get_by_role("button", name="Save source", exact=True).click()

        response = info.value
        assert response.status == 200
        # The saved query carries its identity, connection and chosen kind.
        body = response.request.post_data_json
        assert body["source_ref"] == NEW_SOURCE
        assert body["connection_ref"] == "fixture-library"
        assert body["media_types"] == ["image"]
        # The server reports the Source as CREATED.
        receipt = response.json()
        assert receipt["source_ref"] == NEW_SOURCE and receipt["created"] is True

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
        _connect(page, origin)
        _to_showrunner(page)

        scenes = page.get_by_role("region", name="Scenes", exact=True)
        expect(scenes).to_be_visible()
        # No scenes exist yet, so the appearance below is not vacuously true.
        expect(scenes.get_by_label(f"Scene {SCENE_ID}", exact=True)).to_have_count(0)

        form = scenes.get_by_role("form", name="Author a Scene", exact=True)
        form.get_by_label("Scene name", exact=True).fill(SCENE_ID)
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

        form.get_by_label("Scene name", exact=True).fill(AUTHORED_SCENE_ID)
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


def test_a_get_through_apiwrite_does_not_drop_a_poll(page, registry):
    """Pass 2 §7: only non-GET calls move the write fence. The candidates read goes through
    apiWrite as a GET while a poll is in flight; the poll still lands (the age resets)."""
    _seed(registry)
    queue = _seed_source(registry, _authored_photos(registry))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        pause_page_clock(page, registry.clock.utc())
        _connect(page, origin)
        _to_showrunner(page)
        form = page.get_by_role("region", name="Scenes", exact=True).get_by_role(
            "form", name="Author a Scene", exact=True)
        form.get_by_label("Authored per-frame", exact=True).check()
        form.get_by_label("Source", exact=True).select_option(SOURCE)

        reads = RequestGate(page, "**/v1/operator/inventory")
        reads.holding = True
        page.clock.run_for(5000)
        reads.wait_held()
        reads.holding = False
        expect(page.get_by_text(re.compile(r"updated 5 s ago"))).to_be_visible()

        # The candidates GET, through apiWrite, while the poll is in flight.
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        choice = form.get_by_label(f"Media for frame {VALID_FRAME}", exact=True)
        expect(choice.get_by_role("option", name="Photo 108×192", exact=True)).to_have_count(1)

        reads.release()
        expect(page.get_by_text(re.compile(r"updated 0 s ago"))).to_be_visible()


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
    form.get_by_label("Scene name", exact=True).fill(scene_id)
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


def _schedule_program(page, program, scene_id, start=WINDOW_START, end=WINDOW_END,
                      priority=PROGRAM_PRIORITY, *, submit=True):
    """Fill the Programs region's Schedule form (the one place its field names live) and,
    with `submit`, schedule it and return the PUT response."""
    programs = page.get_by_role("region", name="Programs", exact=True)
    form = programs.get_by_role("form", name="Schedule a Program", exact=True)
    form.get_by_label("Program ID", exact=True).fill(program)
    form.get_by_label("Scene", exact=True).select_option(scene_id)
    form.get_by_label("Window start", exact=True).fill(start)
    form.get_by_label("Window end", exact=True).fill(end)
    form.get_by_label("Priority", exact=True).fill(str(priority))
    if not submit:
        return None
    with page.expect_response(
        lambda r: "/v1/operator/programs/" in r.url and r.request.method == "PUT"
    ) as info:
        form.get_by_role("button", name="Schedule Program", exact=True).click()
    return info.value


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

        response = _schedule_program(page, PROGRAM_ID, SCENE_ID)
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
        _connect(page, origin)
        _to_showrunner(page)
        _author_live_scene(page, SCENE_ID)

        programs = page.get_by_role("region", name="Programs", exact=True)
        _schedule_program(page, PROGRAM_ID, SCENE_ID)

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
        _schedule_program(page, PROGRAM_ID, SCENE_ID, submit=False)

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


# Bead 16 — Run control + activation outcome + "why" panel (SR-runs).

WHY_HIGH = "why-high"
WHY_LOW = "why-low"


def _activate(page, scene_id, activation_id, priority):
    """Activate a Scene NOW through the Runs region's Activate form and return the
    synchronous POST /v1/operator/activations response. The server answers with an
    Admission ({status, reason}) that the console shows AT THE MOMENT.
    """
    runs = page.get_by_role("region", name="Runs", exact=True)
    form = runs.get_by_role("form", name="Activate a Scene", exact=True)
    form.get_by_label("Scene to activate", exact=True).select_option(scene_id)
    form.get_by_label("Activation ID", exact=True).fill(activation_id)
    form.get_by_label("Activation priority", exact=True).fill(str(priority))
    with page.expect_response(
        lambda r: r.url.endswith("/v1/operator/activations")
        and r.request.method == "POST"
    ) as info:
        form.get_by_role("button", name="Activate now", exact=True).click()
    return info.value


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
        _connect(page, origin)
        _to_showrunner(page)
        _author_live_scene(page, SCENE_ID)

        runs = page.get_by_role("region", name="Runs", exact=True)
        outcome = runs.get_by_label("Activation outcome", exact=True)
        # No outcome is shown before an activation — it is synchronous only.
        expect(outcome).to_have_count(0)

        response = _activate(page, SCENE_ID, "act-first", 0)
        assert response.status == 200
        # The server admitted it; the console says exactly that.
        expect(runs.get_by_text("Activation admitted", exact=True)).to_be_visible()

        # Activating the SAME running Scene again (new activation id,
        # repeat=ignore) is IGNORED — shown truthfully, not as a success.
        _activate(page, SCENE_ID, "act-second", 0)
        expect(runs.get_by_text("Activation ignored", exact=True)).to_be_visible()


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
        _connect(page, origin)
        _to_showrunner(page)

        runs = page.get_by_role("region", name="Runs", exact=True)
        expect(runs.get_by_label("Activation outcome", exact=True)).to_have_count(0)

        programs = page.get_by_role("region", name="Programs", exact=True)
        programs.get_by_text("Past (2)", exact=True).click()
        missed = "Missed: its window had ended before Central first scheduled it."
        expect(programs.get_by_label("Program late", exact=True)).to_contain_text(missed)
        slept = programs.get_by_label("Program slept", exact=True)
        expect(slept).to_contain_text(re.compile(r"Ran \d\d:\d\d.*\(one cycle, then ended\)"))
        expect(slept).to_contain_text("if Central was down during the window")
        expect(slept).not_to_contain_text("Missed")

        # The outcome appears ONLY at the synchronous moment of activation.
        response = _activate(page, SCENE_ID, "act-sync", 0)
        assert response.status == 200
        expect(runs.get_by_label("Activation outcome", exact=True)).to_be_visible()
        expect(runs.get_by_text("Activation admitted", exact=True)).to_be_visible()


def test_cancel_removes_live_run(page, registry):
    """Bead 16: an activated Scene lists as a live Run with Finish and Cancel;
    Cancel issues POST …/runs/{id}/cancel and, after the mutate refresh, the Run
    leaves the list (design J4).
    """
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)
        _author_live_scene(page, SCENE_ID)

        runs = page.get_by_role("region", name="Runs", exact=True)
        # Not vacuous: no live Runs before activation.
        expect(runs.get_by_text("No live Runs.", exact=True)).to_be_visible()

        _activate(page, SCENE_ID, "cancel-act", 0)

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
        expect(runs.get_by_text("No live Runs.", exact=True)).to_be_visible()


def test_finish_live_run_posts(page, registry):
    """Bead 16: Finish issues POST …/runs/{id}/finish for a live Run (design J4).
    """
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)
        _author_live_scene(page, SCENE_ID)

        runs = page.get_by_role("region", name="Runs", exact=True)
        _activate(page, SCENE_ID, "finish-act", 0)

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
        _connect(page, origin)
        _to_showrunner(page)
        # Two Scenes, both targeting VALID_FRAME.
        _author_live_scene(page, WHY_LOW)
        _author_live_scene(page, WHY_HIGH)

        # Activate the LOW priority first, the HIGH priority second — so ordering
        # cannot be an accident of activation order.
        _activate(page, WHY_LOW, "why-low-act", 1)
        _activate(page, WHY_HIGH, "why-high-act", 5)

        runs = page.get_by_role("region", name="Runs", exact=True)
        why = runs.get_by_role("group", name="Why", exact=True)
        why.get_by_label("Frame for why", exact=True).select_option(VALID_FRAME)

        rows = why.get_by_role("listitem")
        expect(rows).to_have_count(2)
        # Deterministic precedence: higher priority (why-high, 5) ranks first.
        expect(rows.nth(0)).to_contain_text(WHY_HIGH)
        expect(rows.nth(0)).to_contain_text("priority 5")
        expect(rows.nth(1)).to_contain_text(WHY_LOW)
        expect(rows.nth(1)).to_contain_text("priority 1")


# Pass 2 slice 3A (docs/operator-console-ux-pass2-showrunner.md).


def _scenes_form(page):
    return page.get_by_role("region", name="Scenes", exact=True).get_by_role(
        "form", name="Author a Scene", exact=True)


def _epoch(page, local):
    """The POSIX seconds of a `datetime-local` value in the browser's time zone."""
    return page.evaluate("(value) => new Date(value).getTime() / 1000", local)




def test_tracer_a_named_scene_keeps_playing_through_its_program(page, registry):
    """§15 tracer: the operator names a Scene, sees the id it saves under, keeps
    "Keep playing" on and saves; the form clears and the list shows the id. A
    Program 18:00–20:00 with Central 5 min past 18:00 still holds its Run live —
    a Scene no longer plays one cycle and stops (the P1)."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)
        scenes = page.get_by_role("region", name="Scenes", exact=True)
        form = _scenes_form(page)

        name = form.get_by_label("Scene name", exact=True)
        name.fill("Family Evening")
        expect(name).to_have_accessible_description(re.compile("Saved as family-evening"))
        expect(form.get_by_label("Keep playing until the Program ends", exact=True)).to_be_checked()
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        with page.expect_response(
            lambda r: r.url.endswith("/v1/operator/scenes/family-evening")
            and r.request.method == "PUT"
        ) as info:
            form.get_by_role("button", name="Save Scene", exact=True).click()
        body = info.value.request.post_data_json
        assert info.value.status == 200
        assert body["scene_id"] == "family-evening" and body["loop"] is True

        # The form clears, so the saved Scene never reads as a collision.
        expect(scenes.get_by_label("Scene family-evening", exact=True)).to_be_visible()
        expect(name).to_have_value("")
        expect(form.get_by_text(re.compile("already exists"))).to_have_count(0)

        start = _epoch(page, "2027-03-01T18:00")
        registry.clock.advance(start + 300 - registry.clock.utc())
        _schedule_program(page, "evening-show", "family-evening",
                          "2027-03-01T18:00", "2027-03-01T20:00", 0)
        runs = page.get_by_role("region", name="Runs", exact=True)
        expect(runs.get_by_text("Scene family-evening", exact=True)).to_be_visible()


def test_a_name_without_a_latin_letter_asks_for_an_id(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)
        form = _scenes_form(page)
        form.get_by_label("Scene name", exact=True).fill("夕方")
        identifier = form.get_by_label("Id", exact=True)
        expect(identifier).to_be_visible()
        expect(identifier).to_have_accessible_description(
            "This name needs a Latin letter or digit for its id; type an id.")
        identifier.fill("yugata")
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        with page.expect_response(
            lambda r: r.url.endswith("/v1/operator/scenes/yugata") and r.request.method == "PUT"
        ) as info:
            form.get_by_role("button", name="Save Scene", exact=True).click()
        assert info.value.status == 200


def test_a_colliding_name_is_refused_before_any_request(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)
        _author_live_scene(page, "family-evening")
        form = _scenes_form(page)
        puts = []
        page.on("request", lambda request: puts.append(request.url)
                if request.method == "PUT" else None)
        name = form.get_by_label("Scene name", exact=True)
        name.fill("Family Evening")
        collision = "A Scene called family-evening already exists; choose another name."
        expect(name).to_have_accessible_description(re.compile(re.escape(collision)))
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        form.get_by_role("button", name="Save Scene", exact=True).click()
        expect(form.get_by_role("alert")).to_contain_text(collision)
        expect(name).to_be_focused()
        assert puts == []


def test_the_problem_summary_is_frozen_at_submit(page, registry):
    """§6: submitting with problems sends nothing and freezes a summary; a poll
    that changes the live problems (another operator saves "evening") updates
    the field's reason but never rewrites the summary under the reader."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        pause_page_clock(page, registry.clock.utc())
        _connect(page, origin)
        _to_showrunner(page)
        form = _scenes_form(page)
        name = form.get_by_label("Scene name", exact=True)
        name.fill("Evening")
        form.get_by_role("button", name="Save Scene", exact=True).click()
        summary = form.get_by_role("alert")
        expect(summary).to_contain_text("Choose a Source.")
        expect(summary).to_contain_text("Choose at least one frame.")
        expect(form.get_by_label("Source", exact=True)).to_be_focused()

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

_OFFENDERS = """() => [...document.querySelectorAll("body *")]
    .filter((el) => el.getBoundingClientRect().right > window.innerWidth + 0.5)
    .map((el) => el.tagName + "." + [...el.classList].join("."))
    .slice(0, 12)"""


def _seed_long_ids(registry):
    """A Source, Scene, Program and live Run whose ids are long unbroken strings."""
    MediaRepository(registry.db, registry.clock, queue=RecordingMediaQueue()).configure_source(
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


def test_the_showrunner_is_two_columns_wide_and_runs_first_narrow(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    page.set_viewport_size({"width": 1440, "height": 900})
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)
        expect(page.get_by_role("region", name="Runs", exact=True)).to_be_visible()
        runs, scenes = _box(page, "Runs"), _box(page, "Scenes")
        programs, sources = _box(page, "Programs"), _box(page, "Sources")
        # Now (Runs) beside the Library (Scenes, Programs, Sources), at the same top.
        assert runs["x"] + runs["width"] <= scenes["x"]
        assert abs(runs["y"] - scenes["y"]) < 1
        assert scenes["x"] == programs["x"] == sources["x"]
        assert scenes["y"] < programs["y"] < sources["y"]

        page.set_viewport_size({"width": 390, "height": 844})
        runs, scenes = _box(page, "Runs"), _box(page, "Scenes")
        assert abs(runs["x"] - scenes["x"]) < 1 and runs["y"] < scenes["y"]


def test_long_ids_never_scroll_the_showrunner_sideways_at_phone_width(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    _seed_long_ids(registry)
    page.set_viewport_size({"width": 390, "height": 844})
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)
        expect(page.get_by_role("region", name="Runs", exact=True).get_by_text(
            f"Scene {LONG_ID}", exact=True)).to_be_visible()
        expect(page.get_by_role("region", name="Sources", exact=True).get_by_text(
            LONG_ID + ":1", exact=True)).to_be_visible()
        fits = page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
        assert fits, f"overflows at 390 px: {page.evaluate(_OFFENDERS)}"


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
    _seed(registry)
    queue = _seed_source(registry)
    _refused_by_guard(registry)
    registry.clock.advance(120)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)
        row = page.get_by_role("region", name="Programs", exact=True).get_by_label(
            "Program blocked", exact=True)
        expect(row).to_contain_text(
            f"Did not start: {VALID_FRAME} was protected by the Run of guard.")
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
        _connect(page, origin)
        _to_showrunner(page)
        row = page.get_by_role("region", name="Programs", exact=True).get_by_label(
            "Program blocked", exact=True)
        expect(row).to_contain_text(
            f"Did not start: {VALID_FRAME} was protected by another Run, no longer listed.")
        expect(row).not_to_contain_text("guard")


def test_a_one_cycle_scene_says_so_on_the_scene_and_its_run(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        _connect(page, origin)
        _to_showrunner(page)
        form = _scenes_form(page)
        form.get_by_label("Scene name", exact=True).fill(SCENE_ID)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        form.get_by_label("Keep playing until the Program ends", exact=True).uncheck()
        with page.expect_response(
            lambda r: r.url.endswith(f"/v1/operator/scenes/{SCENE_ID}") and r.request.method == "PUT"
        ) as info:
            form.get_by_role("button", name="Save Scene", exact=True).click()
        assert info.value.request.post_data_json["loop"] is False
        once = "plays one 30 s cycle, then ends"
        expect(page.get_by_role("region", name="Scenes", exact=True).get_by_label(
            f"Scene {SCENE_ID}", exact=True)).to_contain_text(once)
        _activate(page, SCENE_ID, "once-act", 0)
        run_row = page.get_by_role("region", name="Runs", exact=True).get_by_role("listitem").first
        expect(run_row).to_contain_text(once)
        expect(run_row).to_contain_text("activated directly")


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
        _connect(page, origin)
        _to_showrunner(page)
        why = page.get_by_role("region", name="Runs", exact=True).get_by_role(
            "group", name="Why", exact=True)
        why.get_by_label("Frame for why", exact=True).select_option(VALID_FRAME)
        expect(why).to_contain_text(
            f"Central's plan for {VALID_FRAME}: intro (priority 0, activated directly) on top.")
        rows = why.get_by_role("listitem")
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
        _connect(page, origin)
        _to_showrunner(page)
        runs = page.get_by_role("region", name="Runs", exact=True)
        why = runs.get_by_role("group", name="Why", exact=True)
        why.get_by_label("Frame for why", exact=True).select_option(VALID_FRAME)
        expect(why).to_contain_text(
            f"Central's plan for {VALID_FRAME}: evening (priority 5, Program weekday-evenings) on top.")
        expect(why.get_by_role("listitem").nth(1)).to_have_text(
            "morning (priority 1) is underneath: evening has priority 5.")
        expect(runs.get_by_role("listitem").filter(has_text="Scene evening")).to_contain_text(
            "Program weekday-evenings")

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
        _connect(page, origin)
        _to_showrunner(page)
        form = _scenes_form(page)
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
        pause_page_clock(page, registry.clock.utc())
        _connect(page, origin)
        _to_showrunner(page)
        form = _scenes_form(page)
        form.get_by_label("Scene name", exact=True).fill(SCENE_ID)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        form.get_by_label(f"Target frame {LOBBY_FRAME}", exact=True).check()

        registry.delete_frame(LOBBY_FRAME)
        page.clock.run_for(5000)
        expect(form.get_by_role("status")).to_have_text(
            f"{LOBBY_FRAME} was deleted and removed from this Scene.")
        expect(form.get_by_label(f"Target frame {LOBBY_FRAME}", exact=True)).to_have_count(0)
        with page.expect_response(
            lambda r: r.url.endswith(f"/v1/operator/scenes/{SCENE_ID}") and r.request.method == "PUT"
        ) as info:
            form.get_by_role("button", name="Save Scene", exact=True).click()
        targets = [c["target"] for c in info.value.request.post_data_json["contributions"]]
        assert targets == [f"frame:{VALID_FRAME}"]
