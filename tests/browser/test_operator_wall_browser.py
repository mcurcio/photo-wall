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
import time

import pytest
from console_tasks import connect
from operator_harness import (
    operator_server,
    pause_page_clock,
    report_readiness,
    sign_in,
    tile_health,
)
from playwright.sync_api import expect
from test_registry import enroll

from central.registry import FrameCreate
from central.runtime import Child, Contribution, Program, Scene
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


def test_wall_plan_places_frames_and_unplaced_tray_holds_origin_frame(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")

        # The placed frame renders ON THE PLAN (default Surface "wall"), located
        # by its accessible identity label, not by coordinates.
        expect(page.get_by_role("button", name=f"Frame {PLACED}", exact=True)).to_be_visible()

        # The origin-stacked legacy frame is PRESENT IN THE UNPLACED TRAY by
        # identity -- not drawn at the origin on the plan.
        tray = page.get_by_role("group", name="Unplaced frames")
        expect(tray.get_by_role("button", name=ORIGIN, exact=True)).to_be_visible()
        # It is not drawn on the plan.
        expect(page.get_by_role("button", name=f"Frame {ORIGIN}", exact=True)).to_have_count(0)


# Bead 2 -- now-showing join + frame health + honesty. A frame that a live Run
# targets vs a frame whose bound output reported no display at Player start.
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
    # Compound-key bindings: same player, different outputs. frameHealth() must
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
    # Returned for callers (Bead 3 Inspector) that assert the bound Player id;
    # the Bead 2 tile tests ignore the return.
    return player_id


def test_tile_shows_scheduled_intent_frame_health_and_never_claims_live(page, registry):
    player_id = _seed_now_showing(registry)
    # The Player is heard, so each tile's health reaches its display/commissioning rows.
    report_readiness(registry, player_id)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")

        # The frame a live Run targets shows the intended now-showing chip -- the
        # scene_id joined by the STRING "frame:<id>" -- plus its phase, located by
        # frame identity (its status group), not by coordinates.
        showing = page.get_by_role("group", name=f"Frame {SHOWING} status", exact=True)
        expect(showing).to_contain_text(f"Scheduled: {SCENE}")
        expect(showing).to_contain_text("Phase: body")
        # Its bound output had a display at Player start; it was never commissioned.
        expect(tile_health(page, SHOWING)).to_have_accessible_name("Needs commissioning")

        # The second frame's bound output reported no display: its health says so,
        # scoped to that frame's identity (compound-key join -- the two frames
        # share a player but resolve to different ports).
        offline = page.get_by_role("group", name=f"Frame {OFFLINE} status", exact=True)
        expect(tile_health(page, OFFLINE)).to_have_accessible_name(
            "No display detected when the Player started")
        expect(offline).to_contain_text("No display detected")
        expect(showing).not_to_contain_text("No display detected")

        # Honesty (design §6a): the surface asserts intent, never confirmed
        # playback -- the literal "LIVE" appears nowhere on the console.
        expect(page.get_by_text(re.compile("LIVE"))).to_have_count(0)


def test_selecting_frame_opens_inspector_with_binding_and_nowshowing(page, registry):
    # Reuse the Bead 2 seeding: a placed frame bound to a connected output that a
    # live Run targets. `player_id` is the Player the frame is bound to.
    player_id = _seed_now_showing(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")

        # Selecting the frame on the plan (by identity) opens its read-only
        # Inspector, scoped to that frame's identity.
        page.get_by_role("button", name=f"Frame {SHOWING}", exact=True).click()
        inspector = page.get_by_role("region", name=f"Frame {SHOWING} inspector", exact=True)
        expect(inspector).to_be_visible()

        # The three facet tabs exist; Commissioning is present as a stub (its real
        # body lands in Bead 4 -- it is NOT a hardware control here).
        expect(inspector.get_by_role("tab", name="Commissioning", exact=True)).to_be_visible()
        expect(inspector.get_by_role("tab", name="Binding", exact=True)).to_be_visible()
        expect(inspector.get_by_role("tab", name="Now-showing", exact=True)).to_be_visible()

        # Binding facet: the bound Player and Output, read through the frame's
        # FrameInventory row.
        inspector.get_by_role("tab", name="Binding", exact=True).click()
        expect(inspector).to_contain_text(player_id)
        expect(inspector).to_contain_text("HDMI-A-1")

        # Now-showing facet: the intended scene_id (joined by the STRING
        # "frame:<id>") plus the precedence-ranked "why" -- the frame's
        # contributions ranked by (priority, root_order, admission_order).
        inspector.get_by_role("tab", name="Now-showing", exact=True).click()
        expect(inspector).to_contain_text(f"Intended scene: {SCENE}")
        why = inspector.get_by_role("list", name="Why")
        expect(why).to_contain_text(SCENE)
        expect(why).to_contain_text("priority")

        # Honesty (design §6a): the Inspector asserts intent, never confirmed
        # playback -- "LIVE" appears nowhere.
        expect(page.get_by_text(re.compile("LIVE"))).to_have_count(0)


def test_frame_inspector_guides_content_authoring_from_the_selected_frame(page, registry):
    registry.create_frame(FrameCreate(
        id="new-frame", surface_id="wall", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        page.get_by_role("button", name="Frame new-frame", exact=True).click()
        inspector = page.get_by_role(
            "region", name="Frame new-frame inspector", exact=True)
        inspector.get_by_role("tab", name="Now-showing", exact=True).click()

        expect(inspector).to_contain_text("Nothing scheduled.")
        expect(inspector).to_contain_text("Frame new-frame starts selected on its Frames step")
        expect(inspector).to_contain_text("Show now or Schedule it")
        inspector.get_by_role("link", name="Make a Scene", exact=True).click()

        expect(page.get_by_role(
            "heading", level=1, name="Scenes", exact=True)).to_be_visible()
        expect(page.get_by_role(
            "heading", name="What kind of Scene?", exact=True)).to_be_visible()
        assert page.evaluate("window.location.hash") == "#/scenes/new/kind?target=new-frame"


def test_surface_filter_switches_the_plan(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")

        # Default Surface "wall" shows its placed frame.
        expect(page.get_by_role("button", name=f"Frame {PLACED}", exact=True)).to_be_visible()

        # Switching the Surface filter re-renders the plan with the other Surface.
        page.get_by_label("Surface", exact=True).select_option("window")
        expect(page.get_by_role("button", name=f"Frame {OTHER_SURFACE}", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name=f"Frame {PLACED}", exact=True)).to_have_count(0)


# Bead 10 -- S-place: drag-to-create (POST) + drag-to-move (PATCH). Assertions are
# behavioral OUTCOMES: the STORED placement read back from registry.inventory()
# (mm values), tray membership, and on-plan identity -- NEVER rendered SVG pixel
# coordinates (design §1c). The px->mm scale MUST match projection.js
# dragToPlacement (WALL_SPAN_MM / viewport.width = 4000 / 960).
SCALE = 4000 / 960
TOL = 40  # mm; absorbs sub-pixel drag rounding + Math.round in dragToPlacement.


def _plan_svg(page):
    return page.get_by_role("group", name="Wall plan for surface wall", exact=True).locator("svg")


def _plan_box(page):
    """Grow the viewport tall enough that the whole plan SVG is on-screen (its
    height exceeds the default 720px viewport, and a mouse drag off the bottom edge
    does not register), then return its box. No scrolling — box coords and the
    handler's getBoundingClientRect stay in one stable, fully-visible frame."""
    svg = _plan_svg(page)
    svg.wait_for(state="visible")
    page.set_viewport_size({"width": 1280, "height": 1600})
    # A settle read: the resize reflows layout; wait for the box to stop changing.
    box = svg.bounding_box()
    for _ in range(20):
        time.sleep(0.05)
        nxt = svg.bounding_box()
        if nxt == box:
            return box
        box = nxt
    return box


def _drag(page, box, fx0, fy0, fx1, fy1):
    """Drag on the plan SVG from one fractional point of its box to another.

    Playwright mouse coords and the element box are both viewport-relative, and
    the handler maps client px -> viewBox px linearly (the SVG preserves its
    viewBox aspect: `width:100%; height:auto`), so viewBox_x == fraction * 960.
    """
    page.mouse.move(box["x"] + fx0 * box["width"], box["y"] + fy0 * box["height"])
    page.mouse.down()
    page.mouse.move(box["x"] + fx1 * box["width"], box["y"] + fy1 * box["height"], steps=8)
    page.mouse.up()


def _wait_for(predicate, timeout=5.0):
    """Poll `predicate` until it returns a truthy value or the timeout elapses."""
    deadline = time.time() + timeout
    value = predicate()
    while not value and time.time() < deadline:
        time.sleep(0.1)
        value = predicate()
    assert value, "condition not met within timeout"
    return value


def _seed_empty_wall(registry):
    """A "wall" Surface whose only frame is origin-stacked (routed to the tray),
    so the plan canvas is EMPTY yet the Surface exists to place onto."""
    registry.create_frame(FrameCreate(
        id="origin-seed", surface_id="wall", x_mm=0, y_mm=0,
        width_mm=300, height_mm=500, profile=PORTRAIT))


def _fill_landscape_profile(page):
    page.get_by_label("Display width (px)").fill("1920")
    page.get_by_label("Display height (px)").fill("1080")
    page.get_by_label("Diagonal (inches)").fill("24")


def test_first_frame_can_be_drawn_when_installation_has_no_frames(page, registry):
    assert not registry.inventory().frames
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        expect(page.get_by_label("Surface", exact=True)).to_have_value("wall")
        expect(page.get_by_text("Drag on this plan to place a Frame")).to_be_visible()
        _drag(page, _plan_box(page), 0.30, 0.30, 0.60, 0.50)
        form = page.get_by_role("form", name="New frame")
        expect(form).to_be_visible()
        form.get_by_label("Frame id", exact=True).fill("first-frame")
        _fill_landscape_profile(page)
        form.get_by_role("button", name="Create frame", exact=True).click()
        expect(page.get_by_role("button", name="Frame first-frame", exact=True)
               ).to_be_visible()
        assert {frame.id for frame in registry.inventory().frames} == {"first-frame"}


def test_first_run_guidance_opens_measured_frame_form_with_focus(page, registry):
    assert not registry.inventory().frames
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        guidance = page.get_by_role("note", name="Getting started")
        expect(guidance).to_be_visible()
        guidance.get_by_role("button", name="Add first frame", exact=True).click()

        form = page.get_by_role("form", name="New frame")
        expect(form).to_be_visible()
        frame_id = form.get_by_label("Frame id", exact=True)
        expect(frame_id).to_be_focused()
        # Opening the action leaves the dismissible guidance and its draft state
        # intact while the existing measured create flow owns the form.
        expect(guidance).to_be_visible()
        expect(page.get_by_role("button", name="Add frame with measurements", exact=True)
               ).to_be_disabled()


def test_drag_create_posts_frame_with_scaled_placement(page, registry):
    _seed_empty_wall(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        box = _plan_box(page)
        before = {frame.id for frame in registry.inventory().frames}

        # Drag a landscape rectangle across the middle of the empty canvas, then
        # capture a coherent (landscape) display profile in the new-frame form.
        _drag(page, box, 0.30, 0.30, 0.60, 0.50)
        page.get_by_label("Frame id", exact=True).fill("lobby-left")
        _fill_landscape_profile(page)
        page.get_by_role("button", name="Create frame", exact=True).click()
        # The form closes on a successful POST -> the row exists server-side.
        expect(page.get_by_role("button", name="Create frame", exact=True)).to_have_count(0)

        created = [frame for frame in registry.inventory().frames if frame.id not in before]
        assert len(created) == 1
        frame = created[0]
        # The operator's readable id is the stored id (slice 2 §8).
        assert frame.id == "lobby-left"
        # STORED placement matches the dragged region under the px->mm scale:
        # start viewBox (0.30*960, 0.30*600), end (0.60*960, 0.50*600).
        assert abs(frame.x_mm - 0.30 * 960 * SCALE) <= TOL       # ~1200
        assert abs(frame.y_mm - 0.30 * 600 * SCALE) <= TOL       # ~750
        assert abs(frame.width_mm - 0.30 * 960 * SCALE) <= TOL   # ~1200
        assert abs(frame.height_mm - 0.20 * 600 * SCALE) <= TOL  # ~500

        # And it renders on the plan by identity (not in the tray).
        expect(page.get_by_role("button", name=f"Frame {frame.id}", exact=True)).to_be_visible()


def test_a_frame_id_with_a_colon_is_refused_before_any_request(page, registry):
    _seed_empty_wall(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        posts = []
        page.on("request", lambda request: posts.append(request.url)
                if request.method == "POST" and request.url.endswith("/v1/operator/frames")
                else None)
        _drag(page, _plan_box(page), 0.30, 0.30, 0.60, 0.50)
        field = page.get_by_label("Frame id", exact=True)
        field.fill("lobby:left")
        # Checked as you type: the rule is stated against the field.
        expect(field).to_have_attribute("aria-invalid", "true")
        expect(field).to_have_accessible_description(re.compile(r"^Not usable\."))
        _fill_landscape_profile(page)
        page.get_by_role("button", name="Create frame", exact=True).click()
        expect(page.get_by_role("form", name="New frame").get_by_role("alert")
               ).to_contain_text("up to 96")
        page.wait_for_timeout(300)
        assert posts == []
        assert {frame.id for frame in registry.inventory().frames} == {"origin-seed"}


def test_a_taken_frame_id_says_so(page, registry):
    _seed_empty_wall(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        _drag(page, _plan_box(page), 0.30, 0.30, 0.60, 0.50)
        page.get_by_label("Frame id", exact=True).fill("origin-seed")
        _fill_landscape_profile(page)
        page.get_by_role("button", name="Create frame", exact=True).click()
        expect(page.get_by_role("form", name="New frame").get_by_role("alert")
               ).to_have_text("A frame named origin-seed already exists.")


def test_drag_created_frame_never_lands_in_unplaced_tray(page, registry):
    _seed_empty_wall(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        box = _plan_box(page)
        before = {frame.id for frame in registry.inventory().frames}

        # Drag DOWN-TO the plan ORIGIN corner so the rect's top-left is viewBox
        # (0,0) -> raw mm (0,0). (Pressing starts at a safe interior point and
        # releases at the corner; pointer capture keeps the events on the SVG.) The
        # non-origin nudge (errata: isUnplaced heuristic) must push it off (0,0) so
        # it is DRAWN, not routed into the Unplaced tray.
        _drag(page, box, 0.40, 0.40, 0.0, 0.0)
        page.get_by_label("Frame id", exact=True).fill("corner.1")
        _fill_landscape_profile(page)
        page.get_by_role("button", name="Create frame", exact=True).click()
        expect(page.get_by_role("button", name="Create frame", exact=True)).to_have_count(0)

        created = [frame for frame in registry.inventory().frames if frame.id not in before]
        assert len(created) == 1
        frame = created[0]
        assert frame.id == "corner.1"
        # Never the exact origin -> never mis-routed to the tray.
        assert (frame.x_mm, frame.y_mm) != (0, 0)
        assert frame.x_mm >= 1

        # It renders on the plan and is absent from the Unplaced tray.
        expect(page.get_by_role("button", name=f"Frame {frame.id}", exact=True)).to_be_visible()
        tray = page.get_by_role("group", name="Unplaced frames")
        expect(tray.get_by_role("button", name=frame.id, exact=True)).to_have_count(0)


def test_measured_create_and_numeric_reposition(page, registry):
    _seed_empty_wall(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        page.get_by_role("button", name="Add frame with measurements", exact=True).click()
        form = page.get_by_role("form", name="New frame")
        form.get_by_label("Frame id", exact=True).fill("measured-frame")
        form.get_by_label("X position (mm)").fill("125")
        form.get_by_label("Y position (mm)").fill("75")
        form.get_by_label("Frame width (mm)").fill("400")
        form.get_by_label("Frame height (mm)").fill("225")
        form.get_by_role("button", name="Create frame", exact=True).click()
        expect(page.get_by_role("button", name="Frame measured-frame", exact=True)).to_be_visible()
        created = next(frame for frame in registry.inventory().frames if frame.id == "measured-frame")
        assert (created.x_mm, created.y_mm, created.width_mm, created.height_mm) == (125, 75, 400, 225)

        page.get_by_role("button", name="Frame measured-frame", exact=True).click()
        page.get_by_role("button", name="Edit placement for measured-frame", exact=True).click()
        form = page.get_by_role("form", name="Place frame measured-frame")
        form.get_by_label("X position (mm)").fill("850")
        form.get_by_label("Y position (mm)").fill("260")
        form.get_by_label("Frame width (mm)").fill("420")
        form.get_by_label("Frame height (mm)").fill("236.25")
        form.get_by_role("button", name="Save placement", exact=True).click()
        def _repositioned():
            frame = next(f for f in registry.inventory().frames if f.id == "measured-frame")
            return frame if (frame.x_mm, frame.y_mm, frame.width_mm, frame.height_mm) == (
                850, 260, 420, 236.25) else None

        _wait_for(_repositioned)


def test_drag_move_existing_frame_patches_placement(page, registry):
    registry.create_frame(FrameCreate(
        id=PLACED, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        expect(page.get_by_role("button", name=f"Frame {PLACED}", exact=True)).to_be_visible()
        box = _plan_box(page)

        # The single placed frame fits the viewport at rect x:0..360, y:0..600.
        # Press inside it (viewBox ~180,300) and drag right to viewBox ~540,300;
        # the frame is REPOSITIONED (PATCH, LWW) and corrects on the next snapshot.
        _drag(page, box, 180 / 960, 300 / 600, 540 / 960, 300 / 600)

        def _moved():
            frame = next(f for f in registry.inventory().frames if f.id == PLACED)
            return frame if frame.x_mm != 100 else None

        frame = _wait_for(_moved)
        # Repositioned to the right (delta 360 viewBox px -> ~1500 mm).
        assert abs(frame.x_mm - 360 * SCALE) <= TOL  # ~1500
        # A move must NOT resize: the stored physical dimensions are preserved.
        assert frame.width_mm == 300
        assert frame.height_mm == 500
        # It still renders on the plan by identity.
        expect(page.get_by_role("button", name=f"Frame {PLACED}", exact=True)).to_be_visible()


def test_a_drag_across_a_poll_ends_in_the_dragged_placement(page, registry):
    """Pass 2 §7: the 5 s poll replaces Plane A mid-drag, and the drag (a ref plus local
    state in Plan) survives it -- the release lands where the operator dragged to."""
    registry.create_frame(FrameCreate(
        id=PLACED, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        connect(page, origin, "wall")
        expect(page.get_by_role("button", name=f"Frame {PLACED}", exact=True)).to_be_visible()
        box = _plan_box(page)

        page.mouse.move(box["x"] + 180 / 960 * box["width"], box["y"] + 300 / 600 * box["height"])
        page.mouse.down()
        page.mouse.move(box["x"] + 360 / 960 * box["width"], box["y"] + 300 / 600 * box["height"],
                        steps=4)
        # A poll lands mid-drag.
        with page.expect_response("**/v1/operator/inventory"):
            page.clock.run_for(5000)
        page.wait_for_timeout(200)
        page.mouse.move(box["x"] + 540 / 960 * box["width"], box["y"] + 300 / 600 * box["height"],
                        steps=4)
        page.mouse.up()

        def _moved():
            frame = next(f for f in registry.inventory().frames if f.id == PLACED)
            return frame if frame.x_mm != 100 else None

        frame = _wait_for(_moved)
        assert abs(frame.x_mm - 360 * SCALE) <= TOL


# Bead 11 -- S-remove: DELETE (guarded 409 -> distinctive operator guidance) + drag
# a tray (origin-stacked) frame onto the plan (PATCH -> distinct geometry -> leaves
# the tray). Assertions are behavioral OUTCOMES: server-side identity via
# registry.inventory(), on-plan/in-tray identity by role/label, and the DISTINCTIVE
# guard wording (design §9a) -- never SVG pixel coordinates or a generic substring.
CLEAR = "clear-frame"
BOUND = "bound-frame"


def _plan(page):
    return page.get_by_role("group", name="Wall plan for surface wall", exact=True)


def _confirm_delete(page):
    """The delete confirmation (slice 2 §7): a plain Confirm naming the frame."""
    page.get_by_role("dialog").get_by_role("button", name="Confirm delete", exact=True).click()


def _seed_bound(registry):
    """A placed frame bound to a connected output, with NO live Run -- so DELETE
    passes the runtime guard and is refused by the binding guard (frame_bound)."""
    identity, _key, _request = enroll(registry, count=1)
    player_id = identity["player_id"]
    registry.create_frame(FrameCreate(
        id=BOUND, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    registry.bind(BOUND, player_id, "HDMI-A-1", expected_generation=0)
    return player_id


def test_delete_clear_frame_removes_it_from_the_plan(page, registry):
    registry.create_frame(FrameCreate(
        id=CLEAR, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        # Select the clear frame on the plan, then delete it via its control.
        page.get_by_role("button", name=f"Frame {CLEAR}", exact=True).click()
        page.get_by_role("button", name=f"Delete frame {CLEAR}", exact=True).click()
        expect(page.get_by_role("dialog")).to_contain_text(
            "No saved Scenes reference this Frame in this snapshot")
        _confirm_delete(page)

        # Gone by identity from the plan AND from server inventory (real removal).
        expect(page.get_by_role("button", name=f"Frame {CLEAR}", exact=True)).to_have_count(0)
        _wait_for(lambda: all(f.id != CLEAR for f in registry.inventory().frames))
        # The dialog closed, the plan region holds focus, and a status line says so. (The
        # wall's last frame is gone, so the region now names no surface.)
        plan = page.get_by_role("group", name=re.compile(r"^Wall plan for surface"))
        expect(page.get_by_role("dialog")).to_have_count(0)
        expect(plan).to_be_focused()
        expect(plan.get_by_role("status")).to_have_text(f"Frame {CLEAR} deleted.")


def test_delete_frame_lists_stored_scene_roots_and_upcoming_programs(page, registry):
    registry.create_frame(FrameCreate(
        id=CLEAR, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    store = RuntimeStore(registry.db, registry.clock)
    store.command("set_scene", Scene(scene_id="nested-gallery", children=(Child(scene=Scene(
        scene_id="inline", contributions=(Contribution(
            target="frame:" + CLEAR, kind="black"),))),)))
    now = registry.clock.utc()
    store.command("set_program", Program(
        program_id="morning-show", scene_id="nested-gallery",
        starts_at=now + 60, ends_at=now + 120))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        page.get_by_role("button", name=f"Frame {CLEAR}", exact=True).click()
        page.get_by_role("button", name=f"Delete frame {CLEAR}", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_contain_text("Saved Scenes targeting this Frame must be edited or removed")
        expect(dialog.get_by_text("nested-gallery", exact=True)).to_be_visible()
        expect(dialog).to_contain_text("Upcoming Programs using those Scenes")
        expect(dialog.get_by_text("morning-show", exact=True)).to_be_visible()
        expect(dialog).to_contain_text("Central checks references again when you confirm")
        _confirm_delete(page)
        alert = _plan(page).get_by_role("alert")
        expect(alert).to_contain_text("saved Scenes: nested-gallery")
        expect(alert).to_contain_text("Programs: morning-show")
        assert any(frame.id == CLEAR for frame in registry.inventory().frames)


def test_delete_frame_referenced_refusal_gives_refresh_and_edit_guidance(page, registry):
    registry.create_frame(FrameCreate(
        id=CLEAR, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        page.route(f"**/v1/operator/frames/{CLEAR}", lambda route: route.fulfill(
            status=409, content_type="application/json",
            body='{"error":"frame_referenced","scene_ids":["new-scene"],'
                 '"program_ids":["new-program"],'
                 '"queued_activation_ids":["queued-show"]}'))
        page.get_by_role("button", name=f"Frame {CLEAR}", exact=True).click()
        page.get_by_role("button", name=f"Delete frame {CLEAR}", exact=True).click()
        _confirm_delete(page)
        alert = _plan(page).get_by_role("alert")
        expect(alert).to_contain_text("saved Scenes: new-scene")
        expect(alert).to_contain_text("Programs: new-program")
        expect(alert).to_contain_text("queued activations: queued-show")
        expect(alert).to_contain_text("Refresh Equipment. Edit or remove the saved Scenes")
        expect(alert).to_contain_text("Edit or remove the Programs")
        expect(alert).to_contain_text("retry after Central resolves them")
        assert any(frame.id == CLEAR for frame in registry.inventory().frames)


def test_delete_frame_queue_only_refusal_does_not_suggest_editing_scenes(page, registry):
    registry.create_frame(FrameCreate(
        id=CLEAR, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        page.route(f"**/v1/operator/frames/{CLEAR}", lambda route: route.fulfill(
            status=409, content_type="application/json",
            body='{"error":"frame_referenced","scene_ids":[],"program_ids":[],'
                 '"queued_activation_ids":["queued-only"]}'))
        page.get_by_role("button", name=f"Frame {CLEAR}", exact=True).click()
        page.get_by_role("button", name=f"Delete frame {CLEAR}", exact=True).click()
        _confirm_delete(page)
        alert = _plan(page).get_by_role("alert")
        expect(alert).to_contain_text("queued activations: queued-only")
        expect(alert).to_contain_text("retry after Central resolves them")
        expect(alert).not_to_contain_text("Edit or remove")
        assert any(frame.id == CLEAR for frame in registry.inventory().frames)


def test_delete_bound_frame_shows_unbind_guidance(page, registry):
    _seed_bound(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        page.get_by_role("button", name=f"Frame {BOUND}", exact=True).click()
        page.get_by_role("button", name=f"Delete frame {BOUND}", exact=True).click()
        _confirm_delete(page)

        # 409 frame_bound -> the DISTINCTIVE unbind guidance (design §9a). Asserting
        # the specific remedy wording, not a generic substring, so a mapping that
        # collapses the code to a generic error cannot false-green.
        expect(_plan(page).get_by_role("alert")).to_contain_text("unbind it before deleting")
        # The Frame is NOT deleted -- it still renders and still exists server-side.
        expect(page.get_by_role("button", name=f"Frame {BOUND}", exact=True)).to_be_visible()
        assert any(f.id == BOUND for f in registry.inventory().frames)


def test_delete_frame_with_live_run_shows_finish_guidance(page, registry):
    # SHOWING is bound AND a live Run targets it; the route checks the runtime guard
    # first, so DELETE is refused with frame_in_use (not frame_bound).
    _seed_now_showing(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        page.get_by_role("button", name=f"Frame {SHOWING}", exact=True).click()
        page.get_by_role("button", name=f"Delete frame {SHOWING}", exact=True).click()
        # The dialog lists the live Run it captured when it opened.
        expect(page.get_by_role("dialog")).to_contain_text("Live Runs on it")
        _confirm_delete(page)

        # 409 frame_in_use -> the DISTINCTIVE finish/cancel-the-Run guidance.
        expect(_plan(page).get_by_role("alert")).to_contain_text("finish or cancel")
        expect(page.get_by_role("button", name=f"Frame {SHOWING}", exact=True)).to_be_visible()


def test_drop_tray_frame_onto_plan_gives_distinct_geometry(page, registry):
    # The only frame is origin-stacked -> it starts in the Unplaced tray.
    registry.create_frame(FrameCreate(
        id=ORIGIN, surface_id="wall", x_mm=0, y_mm=0,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        tray = page.get_by_role("group", name="Unplaced frames")
        item = tray.get_by_role("button", name=ORIGIN, exact=True)
        expect(item).to_be_visible()
        # It is not on the plan yet.
        expect(page.get_by_role("button", name=f"Frame {ORIGIN}", exact=True)).to_have_count(0)

        # Tall viewport so BOTH the plan (top) and the tray (below it) are fully
        # on-screen without scrolling -- mouse coords and getBoundingClientRect then
        # share one stable frame.
        page.set_viewport_size({"width": 1400, "height": 2000})
        svg = _plan_svg(page)
        svg.wait_for(state="visible")
        plan_box = svg.bounding_box()
        for _ in range(20):
            time.sleep(0.05)
            nxt = svg.bounding_box()
            if nxt == plan_box:
                break
            plan_box = nxt
        item_box = item.bounding_box()

        # Press on the tray entry and RELEASE over the plan interior: the frame is
        # dropped onto the plan (PATCH), gaining a distinct non-origin position.
        page.mouse.move(item_box["x"] + item_box["width"] / 2,
                        item_box["y"] + item_box["height"] / 2)
        page.mouse.down()
        page.mouse.move(plan_box["x"] + 0.5 * plan_box["width"],
                        plan_box["y"] + 0.4 * plan_box["height"], steps=8)
        page.mouse.up()

        def _placed():
            frame = next(f for f in registry.inventory().frames if f.id == ORIGIN)
            return frame if (frame.x_mm, frame.y_mm) != (0, 0) else None

        moved = _wait_for(_placed)
        # PATCH preserved the stored physical dimensions (partial placement).
        assert moved.width_mm == 300
        assert moved.height_mm == 500
        # It now renders on the plan by identity and has LEFT the Unplaced tray.
        expect(page.get_by_role("button", name=f"Frame {ORIGIN}", exact=True)).to_be_visible()
        expect(tray.get_by_role("button", name=ORIGIN, exact=True)).to_have_count(0)


# Bead 18 -- H-refresh: the global snapshot-age clock ("updated N s ago" + Refresh),
# the /healthz pill, and the non-blocking, dismissible first-run guidance banner
# whose dismissed flag lives in Plane B (survives a Plane A refresh). Behavioral:
# role/text/visible state only.


def test_snapshot_clock_advances_and_refresh_resets_the_age(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)

        # The global bar shows the snapshot age in the "updated N s ago" form.
        expect(page.get_by_text(re.compile(r"updated \d+ s ago"))).to_be_visible()

        # The clock advances on its own (no refresh) -- wait until it reads >= 2s.
        expect(
            page.get_by_text(re.compile(r"updated [2-9]\d* s ago"))
        ).to_be_visible(timeout=8000)

        # Explicit Refresh replaces Plane A with a fresh read, so the age resets.
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(
            page.get_by_text(re.compile(r"updated [01] s ago"))
        ).to_be_visible(timeout=8000)


def test_health_pill_renders_a_health_state(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)

        # The /healthz pill reports central reachable (DB up in the test harness),
        # located by its accessible name, not by coordinates.
        expect(
            page.get_by_role("status", name="Central health: ok")
        ).to_be_visible(timeout=12000)


def test_guidance_banner_is_dismissible_and_dismissal_survives_refresh(page, registry):
    # No frames seeded -> first-run empty wall -> the guidance banner shows.
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)

        guidance = page.get_by_role("note", name="Getting started")
        expect(guidance).to_be_visible()

        # Dismiss it (Plane B, component-local).
        guidance.get_by_role("button", name="Dismiss guidance", exact=True).click()
        expect(page.get_by_role("note", name="Getting started")).to_have_count(0)

        # Let the clock advance, then explicitly Refresh (replaces Plane A). Wait
        # for the age to reset to prove the refresh actually landed and re-rendered
        # -- only then assert the banner is STILL gone: a dismissal that lives in
        # Plane B is not resurrected by a Plane A refresh (design §4a).
        expect(
            page.get_by_text(re.compile(r"updated [1-9]\d* s ago"))
        ).to_be_visible(timeout=8000)
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(
            page.get_by_text(re.compile(r"updated [01] s ago"))
        ).to_be_visible(timeout=8000)
        # Let post-refresh effects settle: a mutation that resurrects the dismissed
        # flag on a Plane A change re-renders the banner within a frame or two, so a
        # bare to_have_count(0) can false-green in the paint window before it
        # reappears. Wait past that window, THEN assert the banner stays gone.
        page.wait_for_timeout(800)
        expect(page.get_by_role("note", name="Getting started")).to_have_count(0)
