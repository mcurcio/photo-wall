"""Behavioral browser checks for wall health at a glance (console pass 2, slice 1).

Liveness is Central's record of the last readiness report it accepted from a Player on its
current authority epoch; every age is Central-relative (the inventory's `read_at`). State is
set up with the controlled ManualClock plus the production Coordinator's advance() and
readiness() (operator_harness.report_readiness), so the console classifies genuine inventory.

Assertions are behavioral -- role/text/visible state, plus the severity class that decides
the colour -- and locate frames by identity, never by SVG coordinates.
"""

import os
import re
import time

import pytest
from console_tasks import connect, go, open_frame
from operator_harness import (
    SNAPSHOT,
    RequestGate,
    assert_fits_width,
    operator_server,
    report_readiness,
    sign_in,
    tile_health,
    tile_status,
)
from playwright.sync_api import expect
from test_registry import ADMIN, enroll

from central.registry import FrameCreate
from contracts.models import Calibration, FrameProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

PORTRAIT = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)
FRAME = "lobby-left"


def _bound_frame(registry, frame_id=FRAME, *, x_mm=100, commissioned=True):
    """A placed frame bound to a fresh Player's connected HDMI-A-1; returns the Player id."""
    identity, _key, _request = enroll(registry, count=1)
    registry.create_frame(FrameCreate(
        id=frame_id, surface_id="wall", x_mm=x_mm, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    registry.bind(frame_id, identity["player_id"], "HDMI-A-1", expected_generation=0)
    if commissioned:
        registry.calibrate(frame_id, "commit", expected_revision=1,
                           calibration=Calibration(), expected_generation=1)
    return identity["player_id"]


def _tile(page, frame_id=FRAME):
    return tile_status(page, frame_id)


def _health(page, frame_id=FRAME):
    return tile_health(page, frame_id)


def _severity(level):
    return re.compile(rf"\bhealth--{level}\b")


def test_a_reporting_player_reads_last_heard_with_centrals_age(page, registry):
    player_id = _bound_frame(registry)
    report_readiness(registry, player_id)
    registry.clock.advance(2)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        label = _health(page)
        expect(label).to_have_accessible_name("Last heard 2 s ago")
        expect(label).to_have_text("Heard recently")
        expect(label).to_have_class(_severity("ok"))

        # The Inspector header and the Binding facet read the same fact.
        page.get_by_role("button", name=f"Frame {FRAME}", exact=True).click()
        inspector = page.get_by_role("region", name=f"Frame {FRAME} inspector", exact=True)
        expect(inspector).to_contain_text("Last heard 2 s ago")
        inspector.get_by_role("tab", name="Binding", exact=True).click()
        expect(inspector.get_by_role("tabpanel")).to_contain_text("Last heard 2 s ago")

        # Honesty: ok states when Central last heard the Player, never playback.
        for claim in ("LIVE", "online", "connected"):
            expect(page.get_by_text(re.compile(claim))).to_have_count(0)


def test_a_player_not_heard_past_the_threshold_reads_silent(page, registry):
    player_id = _bound_frame(registry)
    report_readiness(registry, player_id)
    registry.clock.advance(40)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        # The tile shows the fact without its age, so it fits; the age is in its name.
        label = _health(page)
        expect(label).to_have_text("Player silent")
        expect(label).to_have_accessible_name("Player silent · last heard 40 s ago")
        expect(label).to_have_class(_severity("alarm"))
        text = label.bounding_box()
        tile = page.get_by_role("button", name=f"Frame {FRAME}", exact=True).bounding_box()
        assert text["x"] + text["width"] <= tile["x"] + tile["width"], (text, tile)


def test_an_enrolled_player_without_a_report_never_reads_ok(page, registry):
    _bound_frame(registry, "fresh", x_mm=100)
    registry.clock.advance(40)
    _bound_frame(registry, "overdue-pending", x_mm=500)  # enrolled 40 s after "fresh"
    registry.clock.advance(5)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        # Enrolled 5 s ago: a to-do, not ok, although commissioned and displayed.
        recent = _health(page, "overdue-pending")
        expect(recent).to_have_accessible_name("Enrolled 5 s ago, no report yet")
        expect(recent).to_have_text("No report yet")
        expect(recent).to_have_class(_severity("todo"))
        # Enrolled 45 s ago and still no report: past the threshold it is an alarm.
        stale = _health(page, "fresh")
        expect(stale).to_have_accessible_name("Enrolled 45 s ago, no report yet")
        expect(stale).to_have_class(_severity("alarm"))


def test_a_missing_read_time_fails_closed_and_never_prints_an_age(page, registry):
    player_id = _bound_frame(registry)
    report_readiness(registry, player_id)
    _bound_frame(registry, "fresh", x_mm=500)

    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        status = page.get_by_role("group", name="Snapshot status", exact=True)
        expect(status).not_to_have_attribute("aria-busy", "true")
        response = page.request.get(origin + "/v1/operator/snapshot",
                                    headers={"Authorization": "Bearer " + ADMIN})
        assert response.status == 200, response.text()
        body = response.json()
        body["inventory"]["read_at"] = None
        page.route(SNAPSHOT, lambda route: route.fulfill(json=body))
        with page.expect_response(SNAPSHOT):
            status.get_by_role("button", name="Refresh", exact=True).click()
        go(page, "wall")
        # With no Central read time there is no age: silence is assumed, never health.
        expect(_health(page)).to_have_accessible_name("Player silent")
        expect(_health(page)).to_have_class(_severity("alarm"))
        expect(_health(page, "fresh")).to_have_accessible_name("Enrolled, no report yet")
        expect(_health(page, "fresh")).to_have_class(_severity("alarm"))
        expect(page.get_by_text(re.compile("NaN|-\\d+ s"))).to_have_count(0)


def test_a_never_commissioned_frame_is_a_todo_not_an_alarm(page, registry):
    player_id = _bound_frame(registry, commissioned=False)
    report_readiness(registry, player_id)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        label = _health(page)
        expect(label).to_have_text("Needs commissioning")
        expect(label).to_have_class(_severity("todo"))
        expect(label).not_to_have_class(_severity("alarm"))


def test_the_threshold_is_centrals_not_a_constant_in_the_console(page, registry):
    """25 s sits between the old 21 s threshold and the served 31.5 s."""
    older = _bound_frame(registry, "older", x_mm=100)
    newer = _bound_frame(registry, "newer", x_mm=500)
    report_readiness(registry, older)
    registry.clock.advance(7)
    report_readiness(registry, newer)
    registry.clock.advance(25)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        expect(_health(page, "newer")).to_have_accessible_name("Last heard 25 s ago")
        expect(_health(page, "older")).to_have_accessible_name(
            "Player silent · last heard 32 s ago")


# --- Polling and the write fence (pass 2 §7). The page clock is paused, so the 5 s poll
# fires only when the test runs the clock; slow responses are held with RequestGate.


def _set_visibility(page, state):
    page.evaluate("""(state) => {
        Object.defineProperty(document, "visibilityState", {configurable: true, get: () => state});
        document.dispatchEvent(new Event("visibilitychange"));
    }""", state)


def _settle(page):
    """Give a released response time to be applied, were it going to be."""
    page.wait_for_timeout(500)


def test_a_backend_change_shows_after_one_poll(page, registry):
    player_id = _bound_frame(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall", paused_at=registry.clock.utc())
        expect(_health(page)).to_have_accessible_name("Enrolled 0 s ago, no report yet")
        report_readiness(registry, player_id)
        page.clock.run_for(5000)
        expect(_health(page)).to_have_accessible_name("Last heard 0 s ago")


def test_a_hidden_tab_does_not_poll_and_refreshes_on_return(page, registry):
    _bound_frame(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall", paused_at=registry.clock.utc())
        expect(_tile(page)).to_be_visible()
        gate = RequestGate(page, SNAPSHOT)
        _set_visibility(page, "hidden")
        page.clock.run_for(30000)
        _settle(page)
        assert gate.seen == 0, "a hidden tab polled the snapshot"
        _set_visibility(page, "visible")
        deadline = time.monotonic() + 5
        while gate.seen == 0 and time.monotonic() < deadline:
            page.wait_for_timeout(20)
        assert gate.seen == 1, "returning to the tab did not refresh at once"


def test_a_stale_poll_is_dropped_after_a_newer_refresh(page, registry):
    player_id = _bound_frame(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall", paused_at=registry.clock.utc())
        expect(_health(page)).to_have_accessible_name("Enrolled 0 s ago, no report yet")
        stale = page.request.get(origin + "/v1/operator/snapshot",
                                 headers={"Authorization": "Bearer " + ADMIN}).text()
        gate = RequestGate(page, SNAPSHOT)
        gate.holding = True
        page.clock.run_for(5000)
        gate.wait_held()
        gate.holding = False

        report_readiness(registry, player_id)
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(_health(page)).to_have_accessible_name("Last heard 0 s ago")

        # The poll started first but answers last, with what it read before the report.
        gate.release(status=200, content_type="application/json", body=stale)
        _settle(page)
        expect(_health(page)).to_have_accessible_name("Last heard 0 s ago")


def test_a_stale_401_does_not_log_the_operator_out(page, registry):
    player_id = _bound_frame(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall", paused_at=registry.clock.utc())
        expect(_tile(page)).to_be_visible()
        gate = RequestGate(page, SNAPSHOT)
        gate.holding = True
        page.clock.run_for(5000)
        gate.wait_held()
        gate.holding = False
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(page.get_by_text(re.compile(r"updated 0 s ago"))).to_be_visible()

        gate.release(status=401, content_type="application/json",
                     body='{"error": "unauthorized"}')
        _settle(page)
        expect(page.get_by_role("button", name="Sign in", exact=True)).to_have_count(0)
        expect(page.get_by_text("Signed out", exact=False)).to_have_count(0)
        expect(page.get_by_text("last refresh failed", exact=False)).to_have_count(0)
        # Still signed in: the next poll reads and applies.
        report_readiness(registry, player_id)
        page.clock.run_for(5000)
        expect(_health(page)).to_have_accessible_name("Last heard 0 s ago")


def test_a_poll_in_flight_when_a_bind_completes_is_dropped_and_polling_continues(
        page, registry):
    registry.create_frame(FrameCreate(
        id=FRAME, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    identity, _key, _request = enroll(registry, count=1)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall", paused_at=registry.clock.utc())
        expect(_health(page)).to_have_accessible_name("Needs a Player")
        inspector = open_frame(page, FRAME, "binding")

        writes = RequestGate(page, "**/v1/operator/frames/*/binding")
        reads = RequestGate(page, SNAPSHOT)
        writes.holding = True
        handle = identity["player_id"][-6:]
        inspector.get_by_role("radio", name=f"{handle} · HDMI-A-1 · Free", exact=True).check()
        inspector.get_by_role("button", name=f"Bind to {FRAME}", exact=True).click()
        writes.wait_held()

        # A poll starts while the bind is in flight...
        reads.holding = True
        page.clock.run_for(5000)
        reads.wait_held(1)
        # ...the bind completes, and the refresh after it starts (also held).
        writes.release()
        reads.wait_held(2)
        reads.holding = False

        # The poll now answers with post-bind data, but it overlapped the write: dropped.
        reads.release(0)
        _settle(page)
        expect(_health(page)).to_have_accessible_name("Needs a Player")
        # The refresh the bind issued lands.
        reads.release(0)
        expect(_health(page)).to_have_accessible_name("Enrolled 0 s ago, no report yet")

        # The dropped poll released its slot: the next tick reads and applies.
        report_readiness(registry, identity["player_id"])
        page.clock.run_for(5000)
        expect(_health(page)).to_have_accessible_name("Needs commissioning")


# --- The attention strip and navigation (pass 2 §5).


def _strip(page):
    return page.get_by_role("region", name="Wall attention", exact=True)


def _open_list(page):
    """Expand the strip's disclosure (it may already be open) and return its list."""
    toggle = _strip(page).get_by_role("button", name=re.compile(r"^(Show|Hide) frames$"))
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
    return _strip(page).get_by_role("list", name="Frames needing attention", exact=True)


def _seed_attention(registry):
    """Two silent frames (one also uncommissioned: silence precedes commissioning), one
    frame whose Player enrolled long ago and never reported, one heard frame needing
    commissioning, one unbound frame, and one ok frame."""
    silent_a = _bound_frame(registry, "silent-a", x_mm=100, commissioned=False)
    silent_b = _bound_frame(registry, "silent-b", x_mm=500)
    for player_id in (silent_a, silent_b):
        report_readiness(registry, player_id)
    _bound_frame(registry, "never-reported", x_mm=2100)
    registry.clock.advance(240)
    heard = _bound_frame(registry, "to-commission", x_mm=900, commissioned=False)
    fine = _bound_frame(registry, "all-good", x_mm=1300)
    for player_id in (heard, fine):
        report_readiness(registry, player_id)
    registry.create_frame(FrameCreate(
        id="no-player", surface_id="wall", x_mm=1700, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))


def test_the_strip_counts_alarms_apart_from_todos(page, registry):
    _seed_attention(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        expect(_strip(page).get_by_role("status")).to_have_text(
            "3 frames need attention · 2 to set up")
        entries = _open_list(page).get_by_role("listitem")
        # Alarms first, then to-dos; each names the frame and states its fact and age.
        expect(entries).to_have_text([
            "never-reported — Enrolled 4 min ago, no report yet",
            "silent-a — Player silent · last heard 4 min ago",
            "silent-b — Player silent · last heard 4 min ago",
            "no-player — Needs a Player",
            "to-commission — Needs commissioning",
        ])


def test_strip_navigation_opens_the_facet_showing_the_cause_and_focuses_the_inspector(
        page, registry):
    _seed_attention(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        _open_list(page).get_by_role("button", name="to-commission — Needs commissioning").click()
        inspector = page.get_by_role("region", name="Frame to-commission inspector", exact=True)
        expect(inspector.get_by_role("tab", name="Commissioning", exact=True)).to_have_attribute(
            "aria-selected", "true")
        expect(inspector.get_by_role("heading", name="Frame to-commission", exact=True)
               ).to_be_focused()

        _open_list(page).get_by_role(
            "button", name="silent-a — Player silent · last heard 4 min ago").click()
        inspector = page.get_by_role("region", name="Frame silent-a inspector", exact=True)
        expect(inspector.get_by_role("tab", name="Binding", exact=True)).to_have_attribute(
            "aria-selected", "true")
        expect(inspector.get_by_role("heading", name="Frame silent-a", exact=True)).to_be_focused()

        # Plain selection keeps the open facet (no reset) and never moves focus.
        page.get_by_role("button", name="Frame all-good", exact=True).click()
        inspector = page.get_by_role("region", name="Frame all-good inspector", exact=True)
        expect(inspector.get_by_role("tab", name="Binding", exact=True)).to_have_attribute(
            "aria-selected", "true")
        expect(inspector.get_by_role("heading", name="Frame all-good", exact=True)
               ).not_to_be_focused()


def test_a_strip_focus_request_is_spent_once_and_not_replayed_on_remount(page, registry):
    _seed_attention(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        _open_list(page).get_by_role("button", name="no-player — Needs a Player").click()
        heading = page.get_by_role("heading", name="Frame no-player", exact=True)
        expect(heading).to_be_focused()
        # Away to a Show page (the Wall unmounts) and back through the sidebar, which
        # returns to the Wall as it was: the frame is open again, but the spent request
        # moves no focus; it stays on the sidebar link.
        go(page, "now")
        go(page, "wall")
        wall = page.get_by_role("navigation", name="Sections", exact=True).get_by_role(
            "link", name="Wall", exact=True)
        expect(heading).to_be_visible()
        expect(heading).not_to_be_focused()
        expect(wall).to_be_focused()


def test_escape_closes_the_list_and_returns_focus_to_its_toggle(page, registry):
    _seed_attention(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        entries = _open_list(page)
        entries.get_by_role("button").first.focus()
        page.keyboard.press("Escape")
        expect(entries).to_have_count(0)
        expect(_strip(page).get_by_role("button", name="Show frames", exact=True)).to_be_focused()


def test_a_player_just_enrolled_is_not_yet_counted_as_a_todo(page, registry):
    """Within two served report intervals of enrolling, a Player that has not reported
    yet is settling: never ok on its tile, but not a to-do in the strip."""
    _bound_frame(registry, "earlier", x_mm=100)
    registry.clock.advance(5)
    _bound_frame(registry, "just-enrolled", x_mm=500)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        settling = _health(page, "just-enrolled")
        expect(settling).to_have_accessible_name("Enrolled 0 s ago, no report yet")
        expect(settling).to_have_class(_severity("todo"))
        expect(_strip(page).get_by_role("status")).to_have_text("1 to set up")
        expect(_open_list(page).get_by_role("listitem")).to_have_text([
            "earlier — Enrolled 5 s ago, no report yet",
        ])


def test_showrunner_strip_entries_are_text_not_navigation(page, registry):
    _seed_attention(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "now")
        entries = _open_list(page)
        expect(entries).to_contain_text("silent-a — Player silent · last heard 4 min ago")
        expect(entries.get_by_role("button")).to_have_count(0)


def test_a_stalled_scheduler_collapses_silent_frames_into_one_causal_line(page, registry):
    _seed_attention(registry)
    page.route("**/healthz", lambda route: route.fulfill(
        status=503, content_type="application/json",
        body='{"status": "unavailable", "database": true, "protocol": 1, "scheduler": '
             '{"enabled": true, "running": true, "status": "stale", "last_tick": null, '
             '"error": null}}'))
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        expect(page.get_by_role("status", name="Central health: scheduler stale")).to_have_text(
            "Central: scheduler stale")
        # Every liveness alarm -- silent, or enrolled and never reported -- is one line.
        entries = _open_list(page).get_by_role("listitem")
        expect(entries).to_have_text([
            "3 frames silent — Central's scheduler is stale; Players may be unable to report "
            "until it recovers.",
            "no-player — Needs a Player",
            "to-commission — Needs commissioning",
        ])


def test_a_long_list_is_capped_with_a_count_of_the_rest(page, registry):
    for index in range(10):
        registry.create_frame(FrameCreate(
            id=f"frame-{index:02d}", surface_id="wall", x_mm=100 + 400 * index, y_mm=100,
            width_mm=300, height_mm=500, profile=PORTRAIT))
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        expect(_strip(page).get_by_role("status")).to_have_text("10 to set up")
        entries = _open_list(page).get_by_role("listitem")
        expect(entries).to_have_count(9)
        expect(entries.last).to_have_text("and 2 more")


def test_first_run_has_no_strip(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        expect(page.get_by_role("note", name="Getting started")).to_be_visible()
        expect(_strip(page)).to_have_count(0)


# --- Layout and theme (pass 2 §6).

LONG_ID = "reception-" + "north-wall-left-of-the-main-entrance-" * 2 + "panel"

def _seed_layout(registry):
    _seed_attention(registry)
    enroll(registry, count=1)  # a pending Player on the rail
    registry.create_frame(FrameCreate(  # an origin-stacked frame with a long id -> tray
        id=LONG_ID, surface_id="wall", x_mm=0, y_mm=0,
        width_mm=300, height_mm=500, profile=PORTRAIT))


@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 900}, {"width": 390, "height": 844}])
def test_strip_navigation_leaves_the_inspector_in_view(page, registry, viewport):
    _seed_layout(registry)
    page.set_viewport_size(viewport)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        _open_list(page).get_by_role("button", name="no-player — Needs a Player").click()
        inspector = page.get_by_role("region", name="Frame no-player inspector", exact=True)
        expect(inspector).to_be_in_viewport()
        expect(inspector.get_by_role("heading", name="Frame no-player", exact=True)).to_be_focused()


def test_a_phone_width_page_never_scrolls_sideways(page, registry):
    _seed_layout(registry)
    page.set_viewport_size({"width": 390, "height": 844})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        page.get_by_role("button", name="Frame silent-b", exact=True).click()
        expect(page.get_by_role("region", name="Frame silent-b inspector", exact=True)
               ).to_be_visible()
        _open_list(page)
        for facet in ("Commissioning", "Binding", "Now-showing"):
            page.get_by_role("tab", name=facet, exact=True).click()
            assert_fits_width(page, facet)
        # Every Show page too, not only the last one visited.
        for section in ("now", "scenes", "schedule", "sources"):
            go(page, section)
            assert_fits_width(page, section)


def test_the_console_follows_a_light_colour_scheme(page, registry):
    _seed_attention(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        expect(_strip(page)).to_be_visible()
        background = "() => getComputedStyle(document.body).backgroundColor"
        page.emulate_media(color_scheme="dark")
        dark = page.evaluate(background)
        page.emulate_media(color_scheme="light")
        assert page.evaluate(background) != dark
