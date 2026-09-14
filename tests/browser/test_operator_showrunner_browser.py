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

from central.media_repository import MediaRepository
from central.registry import FrameCreate
from contracts.models import Calibration, FrameProfile
from media.models import SourceSpec

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


def _seed_source(registry):
    """Configure ONE saved live query through the shared DB so the console's
    /v1/operator/media renders it, wiring a RecordingMediaQueue so the Refresh
    POST is accepted (202). No worker, upstream, or renderer runs.

    Returns the queue to hand to operator_server so the app's own media
    repository (built in create_app) shares it and accepts the refresh.
    """
    queue = RecordingMediaQueue()
    repository = MediaRepository(registry.db, registry.clock, queue=queue)
    repository.configure_source(
        SourceSpec(source_ref=SOURCE, connection_ref="fixture-library"))
    return queue


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
