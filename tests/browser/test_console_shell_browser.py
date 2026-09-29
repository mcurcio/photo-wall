"""Behavioral checks for the console shell: serving, sections and routes, the drawer, and the
session overlay (flow design §3, §6; bead 1b).

Reuses the existing operator browser harness (real Chromium against the production create_app
on an ephemeral loopback listener, the disposable-schema `registry` fixture, and the autouse
`page_errors` guard). Both `/` and `/console` serve the same built shell with the same CSP and
no-store headers. Assertions are behavioral (role/text/headers/URL), never geometry, except
where the drawer's layout is the point.

Sections are pages with hash routes. The Show pages stay mounted and `hidden` when not current
(rule 2), so a draft survives any navigation; the Wall and neutral pages mount only while
current, so no Show or neutral page holds Commissioning DOM (R4). The route tables' sample paths
come from routeSamples.json, the file the tables themselves read (tests/test_console_routes_r4.py
checks that every table takes its samples from its own group), so these visits follow the
tables without parsing JSX.
"""

import json
import os
import re
from pathlib import Path

import pytest
from console_tasks import (
    LABELS,
    add_source,
    author_scene,
    connect,
    current_hash,
    go,
    scene_continue,
    scene_form,
    start_scene,
    visible_page,
    visit,
)
from operator_harness import (
    INVENTORY,
    RequestGate,
    drive_poll,
    operator_server,
    report_readiness,
    sign_in,
    submit_sign_in,
)
from playwright.sync_api import expect
from test_operator_showrunner_browser import (
    INVALID_FRAME,
    SOURCE,
    VALID_FRAME,
    _authored_photos,
    _seed,
    _seed_source,
)
from test_registry import enroll

from central.registry import FrameCreate
from contracts.models import Calibration, FrameProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';"
       " frame-ancestors 'none'")

SAMPLES = json.loads(
    (Path(__file__).parents[2] / "central/console/src/routeSamples.json").read_text())
SAMPLE_FRAME = "sample-frame"  # the frame id in the Wall's sample paths
PORTRAIT = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)
NARROW = {"width": 390, "height": 844}



def _heading(page, section):
    return page.get_by_role("heading", level=1, name=LABELS[section], exact=True)


def _sidebar_link(page, section):
    return page.get_by_role("navigation", name="Sections", exact=True).get_by_role(
        "link", name=LABELS[section], exact=True)


def _expect_on(page, section):
    expect(_heading(page, section)).to_be_visible()
    expect(_sidebar_link(page, section)).to_have_attribute("aria-current", "page")


def _frame(registry, frame_id, *, x_mm=100):
    registry.create_frame(FrameCreate(
        id=frame_id, surface_id="wall", x_mm=x_mm, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))


def _bound_commissioned(registry, frame_id, *, x_mm=100, commissioned=True):
    """A placed frame bound to a fresh, heard Player's HDMI-A-1."""
    identity, _key, _request = enroll(registry, count=1)
    _frame(registry, frame_id, x_mm=x_mm)
    registry.bind(frame_id, identity["player_id"], "HDMI-A-1", expected_generation=0)
    if commissioned:
        registry.calibrate(frame_id, "commit", expected_revision=1,
                           calibration=Calibration(), expected_generation=1)
    report_readiness(registry, identity["player_id"])


def _scene_form(page):
    return scene_form(page)


# --- Serving and first load.


def test_console_shell_serves_with_csp_and_renders(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        # The shell document carries the SAME hardening as `/` (index()):
        # no-store and the strict same-origin CSP that admits the bundle.
        served = page.request.get(origin + "/console")
        assert served.status == 200
        assert served.headers["cache-control"] == "no-store"
        assert served.headers["content-security-policy"] == CSP

        # The built bundle mounts: with no session, a first load shows the whole-page
        # sign-in screen, and the shell is mounted behind it, hidden. If the shell fails
        # to mount (mutation probe) this fails.
        page.goto(origin + "/console")
        expect(page.get_by_role("heading", name="Sign in to Photo Wall")).to_be_visible()
        expect(page.get_by_label("Operator token")).to_be_visible()
        expect(page.locator("main")).to_be_attached()
        expect(page.locator("main")).to_be_hidden()


def test_root_serves_the_console_shell_after_cutover(page, registry):
    # Bead 17 cutover: `/` (index()) now serves the redesigned console shell — the
    # SAME built bundle as the /console alias, with the SAME hardening (no-store +
    # strict same-origin CSP). This is the redesign becoming the operator's `/`;
    # the retired flat page is gone.
    with operator_server(registry.db, registry.clock) as origin:
        served = page.request.get(origin + "/")
        assert served.status == 200
        assert served.headers["cache-control"] == "no-store"
        assert served.headers["content-security-policy"] == CSP
        # The shell loads the redesign as a same-origin ES module entry — the
        # CSP-clean bundle, not the retired flat page's classic script.
        body = served.text()
        assert 'type="module"' in body
        assert "/console/assets/" in body

        # The React app mounts and renders at `/`, not just /console.
        page.goto(origin + "/")
        expect(page.get_by_role("heading", name="Sign in to Photo Wall")).to_be_visible()
        expect(page.locator("main")).to_be_attached()


# --- Landing, unknown routes and deep links (§6 hash routing details).


def test_landing_is_the_wall_until_a_frame_exists_then_now_showing(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        _expect_on(page, "wall")
        assert current_hash(page) == "#/wall"
        expect(page.get_by_role("note", name="Getting started")).to_be_visible()
    _frame(registry, "first")
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        _expect_on(page, "now")
        assert current_hash(page) == "#/now"


def test_an_unknown_route_is_replaced_by_the_landing_route(page, registry):
    _frame(registry, "first")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "scenes")
        before = page.evaluate("history.length")
        visit(page, "#/no-such-page")
        _expect_on(page, "now")
        assert current_hash(page) == "#/now"
        # Replaced, not pushed: the unknown route's own entry now reads #/now, and Back
        # returns to the page before it, never to the unknown route.
        assert page.evaluate("history.length") == before + 1
        page.go_back()
        _expect_on(page, "scenes")


def test_a_deep_link_survives_sign_in_and_waits_for_the_snapshot(page, registry):
    _frame(registry, "first")
    with operator_server(registry.db, registry.clock) as origin:
        page.context.clear_cookies()
        page.goto(origin + "/console#/sources")
        expect(page.get_by_role("heading", name="Sign in to Photo Wall")).to_be_visible()
        # The first read after sign-in is held: the route is already parsed, and the page
        # says "Loading…" until the snapshot arrives.
        gate = RequestGate(page, INVENTORY)
        gate.holding = True
        submit_sign_in(page)
        gate.wait_held()
        _expect_on(page, "sources")
        expect(visible_page(page).get_by_text("Loading…", exact=True)).to_be_visible()
        expect(page.get_by_role("region", name="Sources", exact=True)).to_have_count(0)
        gate.holding = False
        gate.release()
        expect(page.get_by_role("region", name="Sources", exact=True)).to_be_visible()
        assert current_hash(page) == "#/sources"


def test_a_stale_frame_route_says_it_no_longer_exists(page, registry):
    _frame(registry, "first")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        visit(page, "#/wall/frames/ghost/binding")
        _expect_on(page, "wall")
        expect(page.get_by_text("Frame ghost: This no longer exists.")).to_be_visible()
        expect(page.get_by_role("tab")).to_have_count(0)
        # A typed id that is no id at all is never echoed (routes.js `routeIdName`).
        visit(page, "#/wall/frames/Call%20555%20now!/binding")
        expect(page.get_by_text("An unknown Frame: This no longer exists.")).to_be_visible()
        expect(page.get_by_text(re.compile("Call 555"))).to_have_count(0)


# --- Sections, history and the poll.


def test_back_and_forward_move_between_sections(page, registry):
    _frame(registry, "first")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        for section in ("scenes", "schedule", "wall"):
            go(page, section)
        # A plain tile selection moves the route to the frame but replaces the entry.
        before = page.evaluate("history.length")
        page.get_by_role("button", name="Frame first", exact=True).click()
        expect(page.get_by_role("region", name="Frame first inspector", exact=True)
               ).to_be_visible()
        assert current_hash(page) == "#/wall/frames/first/commissioning"
        assert page.evaluate("history.length") == before
        page.go_back()
        _expect_on(page, "schedule")
        page.go_back()
        _expect_on(page, "scenes")
        page.go_forward()
        _expect_on(page, "schedule")
        page.go_forward()
        _expect_on(page, "wall")
        expect(page.get_by_role("region", name="Frame first inspector", exact=True)
               ).to_be_visible()


def test_the_poll_keeps_running_across_sections(page, registry):
    identity, _key, _request = enroll(registry, count=1)
    _frame(registry, "first")
    registry.bind("first", identity["player_id"], "HDMI-A-1", expected_generation=0)
    reads = []
    page.on("request", lambda request: reads.append(request.url)
            if request.url.endswith("/v1/operator/inventory") else None)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, paused_at=registry.clock.utc())
        badge = page.get_by_role("group", name="Frame health", exact=True).get_by_label(
            re.compile(r"^Frame first: "))
        expect(badge).to_have_accessible_name("Frame first: Enrolled 0 s ago, no report yet")
        for section in ("wall", "attention", "equipment", "schedule"):
            go(page, section)
            count = len(reads)
            drive_poll(page)
            assert len(reads) == count + 1, section
        # A poll applied while Now showing is hidden is there when it is shown again.
        report_readiness(registry, identity["player_id"])
        drive_poll(page)
        go(page, "now")
        expect(badge).to_have_accessible_name("Frame first: Needs commissioning")


def test_a_scene_draft_survives_a_wall_visit_a_section_change_and_a_refresh(page, registry):
    """Rule 2. Bead 2: the draft is the Scene flow's; it also survives step changes, and
    the Scenes page resumes it at the step it was left on (here Review, with the name)."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = author_scene(page, "evening-draft", SOURCE, (VALID_FRAME,), submit=False)

        go(page, "wall")
        expect(page.get_by_role("button", name=f"Frame {VALID_FRAME}", exact=True)).to_be_visible()
        go(page, "schedule")
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(page.get_by_text(re.compile(r"updated [01] s ago"))).to_be_visible()
        go(page, "scenes")
        page.get_by_role("button", name="Resume draft (Draft)", exact=True).click()

        expect(form.get_by_label("Scene name", exact=True)).to_have_value("evening-draft")
        form.get_by_role("button", name="Change Photos", exact=True).click()
        expect(form.get_by_label("Source", exact=True)).to_have_value(SOURCE)
        scene_continue(page, "Review")
        form.get_by_role("button", name="Change Frames", exact=True).click()
        expect(form.get_by_label(f"Target frame {VALID_FRAME}", exact=True)).to_be_checked()
        expect(form.get_by_label(f"Target frame {INVALID_FRAME}", exact=True)).not_to_be_checked()


def test_hidden_show_pages_announce_no_status(page, registry):
    _frame(registry, "first")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        add_source(page, "spring", "fixture-library")
        saved = "Saved Source spring."
        expect(page.get_by_role("status").filter(has_text=saved)).to_be_visible()

        go(page, "wall")
        # The status stays in the DOM (the page is mounted) but its page is `hidden`, so it
        # is out of the accessibility tree: nothing on another page announces it.
        expect(page.locator("main > section[hidden]").get_by_text(saved)).to_have_count(1)
        expect(page.get_by_role("status", include_hidden=True).filter(has_text=saved)
               ).to_have_count(1)
        expect(page.get_by_role("status").filter(has_text=saved)).to_have_count(0)
        assert saved not in page.locator("body").aria_snapshot()
        # Every Show page but none of the others is in the DOM while the Wall is shown.
        assert page.locator("main > section").count() == 5
        expect(page.locator("main > section:not([hidden])")).to_have_count(1)


# --- R4 by routes (§6): every Show and neutral sample path, visited.


def test_no_show_or_neutral_route_holds_display_controls(page, registry):
    _bound_commissioned(registry, SAMPLE_FRAME)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        commissioning = [
            page.get_by_role("tab", name="Commissioning", exact=True, include_hidden=True),
            page.get_by_role("group", name="Committed calibration", include_hidden=True),
            page.get_by_role("group", name="Adjust calibration", include_hidden=True),
            page.get_by_role("region", name=re.compile(r"inspector$"), include_hidden=True),
        ]
        # Positive control: every Wall sample is a Wall page, and the Commissioning sample
        # shows the controls, so the checks below are not vacuous.
        for section, paths in SAMPLES["wall"].items():
            for path in paths:
                visit(page, path)
                _expect_on(page, section)
                assert current_hash(page) == path
        visit(page, f"#/wall/frames/{SAMPLE_FRAME}/commissioning")
        for landmark in commissioning:
            expect(landmark).to_have_count(1)

        visited = 0
        for table in ("show", "neutral"):
            for section, paths in SAMPLES[table].items():
                for path in paths:
                    visit(page, path)
                    _expect_on(page, section)
                    assert current_hash(page) == path, path  # a real route of its section
                    for landmark in commissioning:
                        expect(landmark).to_have_count(0)
                    visited += 1
        assert visited == sum(len(paths) for table in ("show", "neutral")
                              for paths in SAMPLES[table].values())


# --- The Needs attention page.


def test_needs_attention_links_each_frame_to_the_facet_showing_its_cause(page, registry):
    _frame(registry, "no-player", x_mm=100)
    _bound_commissioned(registry, "to-commission", x_mm=500, commissioned=False)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "scenes")
        # "Show all" in the strip opens the page; on a Show page the strip's entries are text.
        strip = page.get_by_role("region", name="Wall attention", exact=True)
        strip.get_by_role("button", name="Show frames", exact=True).click()
        expect(strip.get_by_role("list").get_by_role("button")).to_have_count(0)
        strip.get_by_role("link", name="Show all", exact=True).click()
        _expect_on(page, "attention")

        entries = visible_page(page).get_by_role("list", name="Frames needing attention")
        expect(entries.get_by_role("link")).to_have_text([
            "no-player — Needs a Player", "to-commission — Needs commissioning"])
        expect(entries.get_by_role("link", name="no-player — Needs a Player")).to_have_attribute(
            "href", "#/wall/frames/no-player/binding")
        entries.get_by_role("link", name="to-commission — Needs commissioning").click()

        _expect_on(page, "wall")
        inspector = page.get_by_role("region", name="Frame to-commission inspector", exact=True)
        expect(inspector.get_by_role("tab", name="Commissioning", exact=True)).to_have_attribute(
            "aria-selected", "true")
        expect(inspector.get_by_role("heading", name="Frame to-commission", exact=True)
               ).to_be_focused()
        assert current_hash(page) == "#/wall/frames/to-commission/commissioning"


# --- The drawer under 850 px.


def test_the_drawer_traps_focus_closes_on_escape_and_a_link_focuses_the_page_heading(
        page, registry):
    _frame(registry, "first")
    page.set_viewport_size(NARROW)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        menu = page.get_by_role("button", name="Menu", exact=True)
        expect(menu).to_be_visible()
        expect(page.get_by_role("navigation", name="Sections", exact=True)).to_be_hidden()

        menu.click()
        drawer = page.get_by_role("dialog", name="Menu", exact=True)
        expect(drawer).to_be_visible()
        expect(menu).to_have_attribute("aria-expanded", "true")
        # Modal: the page behind is inert, so Tab and Shift+Tab reach only the drawer's
        # controls (between its last and first, focus may pass to the browser itself,
        # which leaves no element of the page focused).
        focused = """() => document.activeElement.closest('dialog[open]') !== null
            ? document.activeElement.textContent || document.activeElement.ariaLabel
            : document.activeElement === document.body ? '(browser)' : '(page)'"""
        reached = set()
        for key in ["Tab"] * 12 + ["Shift+Tab"] * 12:
            page.keyboard.press(key)
            reached.add(page.evaluate(focused))
        assert "(page)" not in reached, reached
        assert {"Close menu", "Now showing", "Needs attention"} <= reached, reached

        page.keyboard.press("Escape")
        expect(drawer).to_be_hidden()
        expect(menu).to_be_focused()
        expect(menu).to_have_attribute("aria-expanded", "false")

        menu.click()
        drawer.get_by_role("link", name="Scenes", exact=True).click()
        expect(drawer).to_be_hidden()
        expect(_heading(page, "scenes")).to_be_focused()
        assert current_hash(page) == "#/scenes"
        fits = page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
        assert fits, "the shell overflows at 390 px"


def test_the_drawer_slides_in_unless_reduced_motion_is_asked(page, registry):
    page.set_viewport_size(NARROW)
    animation = "getComputedStyle(document.querySelector('dialog[open]')).animationName"
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        menu = page.get_by_role("button", name="Menu", exact=True)
        menu.click()
        assert page.evaluate(animation) == "drawer-in"
        page.keyboard.press("Escape")
        page.emulate_media(reduced_motion="reduce")
        menu.click()
        assert page.evaluate(animation) == "none"


def test_skip_to_content_focuses_main_without_changing_the_route(page, registry):
    _frame(registry, "first")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "schedule")
        skip = page.get_by_role("button", name="Skip to content", exact=True)
        skip.focus()
        page.keyboard.press("Enter")
        expect(page.locator("main")).to_be_focused()
        assert current_hash(page) == "#/schedule"


# --- The session overlay and Log out (§6 cross-pass (a)-(e)).


def _fill_hand_picked_draft(page, portrait_a, portrait_b):
    """A hand-picked Scene draft named "kept-draft", left on its Review step."""
    form = start_scene(page, hand_picked=True)
    form.get_by_label("Source", exact=True).select_option(SOURCE)
    scene_continue(page, "Frames")
    form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
    form.get_by_label(f"Target frame {INVALID_FRAME}", exact=True).check()
    scene_continue(page, "Media per frame")
    valid = form.get_by_label(f"Media for frame {VALID_FRAME}", exact=True)
    invalid = form.get_by_label(f"Media for frame {INVALID_FRAME}", exact=True)
    expect(valid.get_by_role("option")).to_have_count(3)
    valid.select_option(portrait_a.asset.asset_id)
    invalid.select_option(portrait_b.asset.asset_id)
    scene_continue(page, "Playback")
    scene_continue(page, "Review")
    form.get_by_label("Scene name", exact=True).fill("kept-draft")
    return form, valid, invalid


def test_a_session_ending_mid_draft_overlays_sign_in_and_keeps_the_draft(page, registry):
    _seed(registry)
    portrait_a, portrait_b, landscape = _authored_photos(registry)
    queue = _seed_source(registry, (portrait_a, portrait_b, landscape))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes", paused_at=registry.clock.utc())
        form, valid, invalid = _fill_hand_picked_draft(page, portrait_a, portrait_b)

        # The session ends: the next poll answers 401.
        page.route(INVENTORY, lambda route: route.fulfill(
            status=401, content_type="application/json", body='{"error": "unauthorized"}'))
        with page.expect_response(INVENTORY):
            page.clock.run_for(5000)
        expect(page.get_by_role("heading", name="Sign in to Photo Wall")).to_be_visible()
        expect(page.get_by_role("alert")).to_contain_text("Signed out: the session expired")
        expect(page.get_by_role("banner")).to_be_hidden()
        assert page.evaluate("document.querySelector('.shell').inert") is True
        # The last snapshot and the draft are kept under the overlay: the hidden Review
        # still holds the name (get_by_label, unlike get_by_role, finds hidden elements).
        kept = page.get_by_label("Scene name", exact=True)
        expect(kept).to_be_attached()
        assert kept.evaluate("(input) => input.value") == "kept-draft"
        # The poll pauses until sign-in.
        seen = []
        page.on("request", lambda request: seen.append(request.url)
                if request.url.endswith("/v1/operator/inventory") else None)
        page.clock.run_for(20000)
        page.wait_for_timeout(200)
        assert seen == []

        page.unroute(INVENTORY)
        submit_sign_in(page)
        _expect_on(page, "scenes")
        # Bead 2: the draft is back on the step it was left on, and the earlier steps
        # keep their answers.
        expect(form.get_by_label("Scene name", exact=True)).to_have_value("kept-draft")
        steps = page.get_by_role("navigation", name="Steps", exact=True)
        steps.get_by_role("button", name="Media per frame", exact=True).click()
        expect(valid).to_have_value(portrait_a.asset.asset_id)
        expect(invalid).to_have_value(portrait_b.asset.asset_id)
        form.get_by_role("button", name="Back", exact=True).click()
        expect(form.get_by_label(f"Target frame {VALID_FRAME}", exact=True)).to_be_checked()
        expect(form.get_by_label(f"Target frame {INVALID_FRAME}", exact=True)).to_be_checked()
        page.get_by_role("navigation", name="Steps", exact=True).get_by_role(
            "button", name="Kind", exact=True).click()
        expect(form.get_by_label("Hand-picked per frame", exact=True)).to_be_checked()


def test_log_out_discards_the_draft(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        scenes_link = page.get_by_role("navigation", name="Sections", exact=True).get_by_role(
            "link", name="Scenes", exact=True)
        expect(scenes_link).to_have_accessible_description("Draft")

        page.get_by_role("button", name="Log out", exact=True).click()
        expect(page.get_by_role("heading", name="Sign in to Photo Wall")).to_be_visible()
        # Log out clears the snapshot: nothing is said to be kept.
        expect(page.get_by_text("Your unsaved work is kept")).to_have_count(0)
        submit_sign_in(page)
        go(page, "scenes")
        # Bead 2: no draft is left to resume, and a new Scene starts from the defaults.
        expect(scenes_link).to_have_accessible_description("")
        scenes = page.get_by_role("region", name="Scenes", exact=True)
        expect(scenes.get_by_role("button", name=re.compile("^Resume draft"))).to_have_count(0)
        form = start_scene(page)
        expect(form.get_by_label("Source", exact=True)).to_have_value("")
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        expect(form.get_by_label(f"Target frame {VALID_FRAME}", exact=True)).not_to_be_checked()
