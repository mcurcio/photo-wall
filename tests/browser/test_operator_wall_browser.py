"""Behavioral browser checks for the redesigned wall plan at /console (Bead 1).

Reuses the existing operator browser harness (real Chromium against the
production create_app on an ephemeral loopback listener, the disposable-schema
`registry` fixture, and the autouse `page_errors` guard). Frames are seeded
directly through the registry (as the `installation` fixture does) so the console
renders real inventory geometry.

Every assertion is BEHAVIORAL — role/text/visible state — and locates frames by
identity/label, NEVER by SVG coordinates or DOM structure (design §1c).
"""

import os

import pytest
from playwright.sync_api import expect
from test_operator_browser import operator_server
from test_registry import ADMIN

from central.registry import FrameCreate
from contracts.models import FrameProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

PORTRAIT = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)
LANDSCAPE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)

PLACED = "placed-frame"
ORIGIN = "legacy-origin"
OTHER_SURFACE = "window-frame"


def _seed(registry):
    # A placed frame with distinct geometry on surface "wall".
    registry.create_frame(FrameCreate(
        id=PLACED, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    # A legacy origin-stacked frame (no distinct position) -> Unplaced tray.
    registry.create_frame(FrameCreate(
        id=ORIGIN, surface_id="wall", x_mm=0, y_mm=0,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    # A placed frame on a second surface (sorts after "wall", so "wall" stays the
    # default) to prove the Surface filter re-renders the plan.
    registry.create_frame(FrameCreate(
        id=OTHER_SURFACE, surface_id="window", x_mm=50, y_mm=50,
        width_mm=400, height_mm=300, profile=LANDSCAPE))


def _connect(page, origin):
    page.goto(origin + "/console")
    page.get_by_label("Operator token").fill(ADMIN)
    page.get_by_role("button", name="Connect", exact=True).click()


def test_wall_plan_places_frames_and_unplaced_tray_holds_origin_frame(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)

        # The placed frame renders ON THE PLAN (default Surface "wall"), located
        # by its accessible identity label, not by coordinates.
        expect(page.get_by_role("button", name=f"Frame {PLACED}", exact=True)).to_be_visible()

        # The origin-stacked legacy frame is PRESENT IN THE UNPLACED TRAY by
        # identity -- not drawn at the origin on the plan.
        tray = page.get_by_role("group", name="Unplaced frames")
        expect(tray.get_by_role("button", name=ORIGIN, exact=True)).to_be_visible()
        # It is not drawn on the plan.
        expect(page.get_by_role("button", name=f"Frame {ORIGIN}", exact=True)).to_have_count(0)


def test_surface_filter_switches_the_plan(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)

        # Default Surface "wall" shows its placed frame.
        expect(page.get_by_role("button", name=f"Frame {PLACED}", exact=True)).to_be_visible()

        # Switching the Surface filter re-renders the plan with the other Surface.
        page.get_by_label("Surface", exact=True).select_option("window")
        expect(page.get_by_role("button", name=f"Frame {OTHER_SURFACE}", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name=f"Frame {PLACED}", exact=True)).to_have_count(0)
