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
import re

import pytest
from playwright.sync_api import expect
from test_operator_browser import operator_server
from test_registry import ADMIN, enroll

from central.registry import FrameCreate
from central.runtime import Contribution, Scene
from central.runtime_store import RuntimeStore
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


# Bead 2 -- now-showing join + connectivity + honesty. A frame that a live Run
# targets vs a frame whose bound output reports disconnected.
SHOWING = "showing-frame"
OFFLINE = "offline-frame"
SCENE = "lobby-scene"


def _seed_now_showing(registry):
    """Seed two placed frames on "wall": one on a connected output that a live Run
    targets, one on a disconnected output. Returns nothing; drives real registry +
    RuntimeStore state so the console joins against genuine /inventory + /runtime.
    """
    # A player reporting two connected outputs, then re-enrolling (same serial)
    # reporting only HDMI-A-1 -- HDMI-A-2 is left connected=false by the registry
    # (registry.py:135), giving a real disconnected output to bind to.
    identity, key, request = enroll(registry, count=2)
    enroll(registry, key=key, device_id=request.device_id, count=1)
    player_id = identity["player_id"]

    registry.create_frame(FrameCreate(
        id=SHOWING, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    registry.create_frame(FrameCreate(
        id=OFFLINE, surface_id="wall", x_mm=600, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    # Compound-key bindings: same player, different outputs. connectivity() must
    # resolve each frame to its OWN (player_id, output_id) port.
    registry.bind(SHOWING, player_id, "HDMI-A-1", expected_generation=0)
    registry.bind(OFFLINE, player_id, "HDMI-A-2", expected_generation=0)

    # A looping Scene targeting frame:SHOWING, activated at the current (frozen)
    # clock so the projected RunView is in body phase and visible[] carries the
    # string target "frame:<id>" the console joins on.
    store = RuntimeStore(registry.db, registry.clock)
    store.command("set_scene", Scene(
        scene_id=SCENE, loop=True, cycle_seconds=30,
        contributions=(Contribution(target="frame:" + SHOWING,
                                     source_refs=("lobby-photos:1",)),)))
    store.command("activate", SCENE, "lobby-activation", registry.clock.utc())


def test_tile_shows_scheduled_intent_connectivity_and_never_claims_live(page, registry):
    _seed_now_showing(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)

        # The frame a live Run targets shows the intended now-showing chip -- the
        # scene_id joined by the STRING "frame:<id>" -- plus its phase, located by
        # frame identity (its status group), not by coordinates.
        showing = page.get_by_role("group", name=f"Frame {SHOWING} status", exact=True)
        expect(showing).to_contain_text(f"Scheduled: {SCENE}")
        expect(showing).to_contain_text("Phase: body")
        # Its bound output is connected.
        expect(showing.get_by_role("img", name="Player connected")).to_be_visible()

        # The second frame's bound output reports disconnected: its connectivity
        # dot says so, scoped to that frame's identity (compound-key join -- the
        # two frames share a player but resolve to different ports).
        offline = page.get_by_role("group", name=f"Frame {OFFLINE} status", exact=True)
        expect(offline.get_by_role("img", name="Player disconnected")).to_be_visible()

        # Honesty (design §6a): the surface asserts intent, never confirmed
        # playback -- the literal "LIVE" appears nowhere on the console.
        expect(page.get_by_text(re.compile("LIVE"))).to_have_count(0)


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
