"""Behavioral browser checks for wall health at a glance (console pass 2, slice 1; decision 0036).

A Frame's health is Central's status (central/frame_status/), served on Home's read beside the
snapshot; every chip in the console words that status and works nothing out (domain/frame-status.ts).
State is set up with the controlled ManualClock plus the production Coordinator's advance() and
readiness() (operator_harness.report_readiness: Central hears the Player), and the Player's Frame
report as Central's link records it (operator_harness.report_presenting: the picture is presented),
so the console shows genuine statuses. The six words from every kind of fact, the problems and the phone
layout of Home are tests/browser/test_home_browser.py's; here: the Wall's tile, the Frame page, and
the poll and the write fence the status rides.

Assertions are behavioral -- role/text/visible state, plus the severity class that decides
the colour -- and locate frames by identity, never by SVG coordinates.
"""

import os
import re
import time

import pytest
from console_tasks import (
    connect,
    frame_heading,
    frame_name,
    go,
    home_problems,
    house_word,
    open_frame,
    visible_page,
)
from operator_harness import (
    SNAPSHOT,
    RequestGate,
    assert_fits_width,
    operator_server,
    report_presenting,
    report_readiness,
    sign_in,
    tile_health,
    tile_status,
)
from playwright.sync_api import expect
from support.frames import frame_create
from support.position import PORTRAIT_ON_WIDE, save_position
from test_registry import ADMIN, enroll

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

FRAME = "lobby-left"
# A time of day as the console says it.
TIME = r"\d\d:\d\d(:\d\d)?"
STARTING = "Getting ready since {time} · Starting: no report yet"
NOT_HEARD = "Can't tell since {time} · Can't reach its Pi since {time}: it stopped reporting."


def _line(words):
    """A status line with any time of day where `words` says `{time}`."""
    return re.compile("^" + re.escape(words).replace(re.escape("{time}"), TIME) + "$")


def _bound_frame(registry, frame_id=FRAME, *, x_mm=100, commissioned=True):
    """A placed frame bound to a fresh Player's connected HDMI-A-1; returns the Player id."""
    identity, _key, _request = enroll(registry, count=1)
    registry.create_frame(frame_create(frame_id, x_mm=x_mm, y_mm=100), by="console")
    registry.bind(frame_id, identity["player_id"], "HDMI-A-1", expected_generation=0, by="console")
    if commissioned:
        save_position(registry, frame_id, PORTRAIT_ON_WIDE)
    return identity["player_id"]


def _tile(page, frame_id=FRAME):
    return tile_status(page, frame_id)


def _health(page, frame_id=FRAME):
    return tile_health(page, frame_id)


def _severity(level):
    return re.compile(rf"\bhealth--{level}\b")


def _frame_problems(page):
    """Home's cards for Central's Frame problems here (Pis it can't hear). A Pi's own host faults
    are listed after them (tests/browser/test_host_health_browser.py) and are not counted."""
    return home_problems(page).get_by_role("group").filter(has_text=re.compile(r": Can't tell since"))


def _home_tile(page, registry, frame_id):
    """The Frame's tile on Home: a group named by the Frame's name, and its link of that name."""
    name = frame_name(registry, frame_id)
    return page.get_by_role("region", name="Wall 1", exact=True).get_by_role("group", name=name, exact=True)


def test_a_presenting_frame_reads_showing_on_the_wall_and_on_its_page(page, registry):
    player_id = _bound_frame(registry)
    report_presenting(registry, player_id)
    registry.clock.advance(2)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        label = _health(page)
        expect(label).to_have_accessible_name(_line("Showing since {time}"))
        expect(label).to_have_text("Showing")
        expect(label).to_have_class(_severity("ok"))

        # The Frame page's header reads the same status; its Hardware tab says when its Pi reported.
        page.get_by_role("button", name=f"Frame {FRAME}", exact=True).click()
        inspector = visible_page(page)
        expect(inspector.locator("header [data-severity]")).to_have_text(re.compile(r"Showing$"))
        expect(inspector.locator("header [data-severity]")).to_have_attribute("data-severity", "ok")
        inspector.get_by_role("tab", name="Hardware", exact=True).click()
        expect(inspector.get_by_role("tabpanel")).to_contain_text("The Pi: Reporting")
        port = inspector.get_by_role("region", name="Pi and HDMI port", exact=True)
        port.get_by_role("button", name="Details", exact=True).click()
        expect(port).to_contain_text("Player app last reported 2 s ago")

        # Honesty: Showing is what the Pi reports it presents, never a claim about the panel. The
        # one "connected" allowed is the Hardware tab's Panel record, worded as Central's record
        # at the last enrollment (console DDD §19), never as liveness.
        for claim in ("LIVE", "online", r"connected(?! at the Player app's last enrollment)"):
            expect(page.get_by_text(re.compile(claim))).to_have_count(0)
        expect(page.get_by_text(re.compile("Panel connected at the Player app's last enrollment"))
               ).to_have_count(1)


def test_a_pi_not_heard_past_the_threshold_reads_cant_tell(page, registry):
    player_id = _bound_frame(registry)
    report_presenting(registry, player_id)
    registry.clock.advance(60)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        # The tile shows the word, so it fits; since when and why are in its name. What its Pi
        # reported before is no guide to it now: Can't tell, never Showing and never an alarm.
        label = _health(page)
        expect(label).to_have_text("Can't tell")
        expect(label).to_have_accessible_name(_line(NOT_HEARD))
        expect(label).to_have_class(_severity("unknown"))
        text = label.bounding_box()
        tile = page.get_by_role("button", name=f"Frame {FRAME}", exact=True).bounding_box()
        assert text["x"] + text["width"] <= tile["x"] + tile["width"], (text, tile)


def test_a_silent_players_binding_line_links_to_its_player_page_without_a_node_read(page, registry):
    player_id = _bound_frame(registry)
    report_readiness(registry, player_id)
    registry.clock.advance(60)
    with operator_server(registry.db, registry.clock) as origin:
        # The shell's one fleet host read is not a page's node record read.
        node_reads = []
        page.on("request", lambda request: node_reads.append(request.url)
                if "/v1/operator/node/" in request.url
                and "/v1/operator/node/hosts" not in request.url else None)
        connect(page, origin, "wall")
        inspector = open_frame(page, FRAME, "hardware")
        expect(inspector).to_contain_text("The Pi: Not heard from lately")
        link = inspector.get_by_role("link", name="See why on its page", exact=True)
        expect(link).to_have_attribute("href", re.compile(r"^#/players/device-"))
        page.wait_for_timeout(200)
        assert node_reads == [], "the Wall read node records"


def test_an_enrolled_pi_without_a_report_never_reads_ok(page, registry):
    _bound_frame(registry, "fresh", x_mm=100)
    registry.clock.advance(50)
    _bound_frame(registry, "overdue-pending", x_mm=500)  # enrolled 50 s after "fresh"
    registry.clock.advance(5)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        # Enrolled 5 s ago: Getting ready, not ok, although positioned and drawn on the plan.
        recent = _health(page, "overdue-pending")
        expect(recent).to_have_accessible_name(_line(STARTING))
        expect(recent).to_have_text("Getting ready")
        expect(recent).to_have_class(_severity("notice"))
        # Enrolled 55 s ago and still not heard: past the silence limit nothing can be said of it.
        stale = _health(page, "fresh")
        expect(stale).to_have_accessible_name(_line(NOT_HEARD))
        expect(stale).to_have_class(_severity("unknown"))


def test_a_missing_read_time_never_prints_an_age_and_keeps_centrals_words(page, registry):
    player_id = _bound_frame(registry)
    report_presenting(registry, player_id)
    _bound_frame(registry, "fresh", x_mm=500)

    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
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
        expect(status).not_to_have_attribute("aria-busy", "true")
        # The status is Central's, so a snapshot with no read time changes no word; and with
        # no read time there is no age to print, never a made-up one.
        expect(_health(page)).to_have_text("Showing")
        expect(_health(page)).to_have_class(_severity("ok"))
        expect(_health(page, "fresh")).to_have_text("Getting ready")
        expect(page.get_by_text(re.compile("NaN|-\\d+ s"))).to_have_count(0)


def test_a_frame_whose_position_is_not_checked_is_a_todo_not_a_problem(page, registry):
    player_id = _bound_frame(registry, commissioned=False)
    report_readiness(registry, player_id)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        label = _health(page)
        expect(label).to_have_text("Check its Position")
        expect(label).to_have_class(_severity("todo"))
        expect(label).not_to_have_class(_severity("alarm"))
        # Setup is not status: it counts for nothing in the house word and raises no problem.
        expect(house_word(page)).to_have_accessible_name("House: All good")
        go(page, "home")
        expect(home_problems(page)).to_have_count(0)


def test_the_threshold_is_centrals_not_a_constant_in_the_console(page, registry):
    """45 s sits between the old 36.5 s threshold and Central's 51 s."""
    older = _bound_frame(registry, "older", x_mm=100)
    newer = _bound_frame(registry, "newer", x_mm=500)
    report_presenting(registry, older)
    registry.clock.advance(7)
    report_presenting(registry, newer)
    registry.clock.advance(45)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        expect(_health(page, "newer")).to_have_accessible_name(_line("Showing since {time}"))
        expect(_health(page, "older")).to_have_accessible_name(_line(NOT_HEARD))


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
        expect(_health(page)).to_have_accessible_name(_line(STARTING))
        report_presenting(registry, player_id)
        page.clock.run_for(5000)
        expect(_health(page)).to_have_accessible_name(_line("Showing since {time}"))


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
        expect(_health(page)).to_have_accessible_name(_line(STARTING))
        stale = page.request.get(origin + "/v1/operator/snapshot",
                                 headers={"Authorization": "Bearer " + ADMIN}).text()
        gate = RequestGate(page, SNAPSHOT)
        gate.holding = True
        page.clock.run_for(5000)
        gate.wait_held()
        gate.holding = False

        report_presenting(registry, player_id)
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(_health(page)).to_have_text("Showing")

        # The poll started first but answers last, with what it read before the report.
        gate.release(status=200, content_type="application/json", body=stale)
        _settle(page)
        expect(_health(page)).to_have_text("Showing")


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
        report_presenting(registry, player_id)
        page.clock.run_for(5000)
        expect(_health(page)).to_have_text("Showing")


def test_a_poll_in_flight_when_a_bind_completes_is_dropped_and_polling_continues(
        page, registry):
    registry.create_frame(frame_create(FRAME, x_mm=100, y_mm=100), by="console")
    identity, _key, _request = enroll(registry, count=1)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall", paused_at=registry.clock.utc())
        expect(_health(page)).to_have_text("Needs a Pi")
        inspector = open_frame(page, FRAME, "hardware")

        writes = RequestGate(page, "**/v1/operator/frames/*/binding")
        reads = RequestGate(page, SNAPSHOT)
        writes.holding = True
        handle = identity["player_id"][-6:]
        inspector.get_by_role("radio", name=f"{handle} · HDMI 1 · Free", exact=True).check()
        inspector.get_by_role("button", name=f"Connect Frame {FRAME}", exact=True).click()
        writes.wait_held()

        # A poll starts while the bind is in flight...
        reads.holding = True
        page.clock.run_for(5000)
        reads.wait_held(1)
        # ...the bind completes, and the refresh after it starts (also held).
        writes.release()
        reads.wait_held(2)
        reads.holding = False

        # The Frame page's header states the Frame's status, as its tile does.
        health = inspector.locator("header [data-severity]")
        # The poll now answers with post-bind data, but it overlapped the write: dropped.
        reads.release(0)
        _settle(page)
        expect(health).to_contain_text("Needs a Pi")
        # The refresh the bind issued lands: fed by a Pi now, its Position still to check.
        reads.release(0)
        expect(health).to_contain_text("Check its Position")

        # The dropped poll released its slot: the next tick reads and applies. Positioned, and
        # its Pi enrolled within the silence limit with no report yet: Getting ready.
        save_position(registry, FRAME, PORTRAIT_ON_WIDE)
        page.clock.run_for(5000)
        expect(health).to_contain_text("Getting ready")


# --- Home's problems and navigation (decision 0036: the attention strip and the Needs attention
# page folded into Home).


def _seed_attention(registry):
    """Two Frames whose Pis went silent (one also with its Position unchecked: setup comes first),
    one Frame whose Pi enrolled long ago and was never heard, one heard Frame with its Position
    unchecked, one unbound Frame, and one Frame showing."""
    silent_a = _bound_frame(registry, "silent-a", x_mm=100, commissioned=False)
    silent_b = _bound_frame(registry, "silent-b", x_mm=500)
    for player_id in (silent_a, silent_b):
        report_presenting(registry, player_id)
    _bound_frame(registry, "never-reported", x_mm=2100)
    registry.clock.advance(240)
    heard = _bound_frame(registry, "to-commission", x_mm=900, commissioned=False)
    fine = _bound_frame(registry, "all-good", x_mm=1300)
    for player_id in (heard, fine):
        report_presenting(registry, player_id)
    registry.create_frame(frame_create("no-player", x_mm=1700, y_mm=100), by="console")


def test_home_lists_the_problems_and_leaves_setup_to_the_wall(page, registry):
    _seed_attention(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        # Problems only: the two Frames whose Pi Central can't hear. Setup is no problem.
        expect(house_word(page)).to_have_accessible_name("House: Can't tell")
        expect(page.get_by_role("main").get_by_role("group", name="House", exact=True)).to_contain_text(
            "2 Frames can't tell, 1 showing, 3 to set up")
        cards = _frame_problems(page)
        expect(cards).to_have_count(2)
        for frame_id in ("never-reported", "silent-b"):
            card = cards.filter(has_text=f"{frame_name(registry, frame_id)}: ")
            expect(card).to_contain_text(re.compile(rf"Can't tell since {TIME}\."))
            expect(card).to_contain_text("it stopped reporting. Photo Wall keeps trying to reach it.")
        for frame_id, step in (("silent-a", "Check its Position"), ("to-commission", "Check its Position"),
                               ("no-player", "Needs a Pi")):
            expect(_home_tile(page, registry, frame_id).locator("[data-severity]")).to_have_attribute(
                "data-severity", "todo")
            expect(_home_tile(page, registry, frame_id)).to_contain_text(step)
        # The unbound and unpositioned Frames are the Wall's To finish items (G2).
        go(page, "wall")
        expect(page.get_by_role("list", name="To finish", exact=True).get_by_role("listitem")
               ).to_have_text(["no-player · needs a Player Hardware",
                               "silent-a · needs calibration Position",
                               "to-commission · needs calibration Position"])


def test_a_home_tile_opens_the_tab_showing_the_cause_and_focuses_the_frame_page(
        page, registry):
    _seed_attention(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        # A Wall tile click opens the Frame's page at Overview, and its heading takes focus.
        page.get_by_role("button", name="Frame all-good", exact=True).click()
        expect(page.get_by_role("tab", name="Overview", exact=True)).to_have_attribute(
            "aria-selected", "true")
        expect(frame_heading(page, frame_name(registry, "all-good"))).to_be_focused()

        # A Home tile opens it at the tab its status is about: a Pi Central can't hear, on Hardware.
        house_word(page).click()
        name = frame_name(registry, "silent-b")
        _home_tile(page, registry, "silent-b").get_by_role("link", name=name, exact=True).click()
        expect(page.get_by_role("tab", name="Hardware", exact=True)).to_have_attribute(
            "aria-selected", "true")
        expect(frame_heading(page, name)).to_be_focused()
        assert page.evaluate("window.location.hash") == "#/wall/frames/silent-b/hardware"


def test_a_home_tiles_focus_request_is_spent_once_and_not_replayed_on_remount(page, registry):
    _seed_attention(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        name = frame_name(registry, "silent-b")
        _home_tile(page, registry, "silent-b").get_by_role("link", name=name, exact=True).click()
        heading = frame_heading(page, name)
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


def test_a_pi_awaiting_its_first_report_is_getting_ready_and_never_all_good(page, registry):
    """Within the silence limit a Pi that has not been heard yet is no problem, and never ok:
    its Frame is Getting ready, and so is the house, which does not claim All good for a report
    that has not arrived (console DDD G2, R0)."""
    _bound_frame(registry, "earlier", x_mm=100)
    registry.clock.advance(5)
    _bound_frame(registry, "just-enrolled", x_mm=500)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        settling = _health(page, "just-enrolled")
        expect(settling).to_have_accessible_name(_line(STARTING))
        expect(settling).to_have_class(_severity("notice"))
        expect(house_word(page)).to_have_accessible_name("House: Getting ready")
        go(page, "home")
        expect(page.get_by_role("main").get_by_role("group", name="House", exact=True)).to_contain_text(
            "2 Frames getting ready")
        expect(home_problems(page)).to_have_count(0)


def test_a_stale_scheduler_shows_on_centrals_pill_and_each_frame_keeps_its_own_problem(page, registry):
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
        # The pill is /healthz's; Home's problems are Central's statuses, one per Frame.
        expect(_frame_problems(page)).to_have_count(2)


# --- Layout and theme (pass 2 §6).

LONG_ID = "reception-" + "north-wall-left-of-the-main-entrance-" * 2 + "panel"

def _seed_layout(registry):
    _seed_attention(registry)
    enroll(registry, count=1)  # a pending Player on the rail
    registry.create_frame(frame_create(LONG_ID, x_mm=0, y_mm=0), by="console")


@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 900}, {"width": 390, "height": 844}])
def test_a_home_tile_leaves_the_frame_page_in_view(page, registry, viewport):
    _seed_layout(registry)
    page.set_viewport_size(viewport)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        name = frame_name(registry, "silent-b")
        _home_tile(page, registry, "silent-b").get_by_role("link", name=name, exact=True).click()
        heading = frame_heading(page, name)
        expect(heading).to_be_in_viewport()
        expect(heading).to_be_focused()


def test_a_phone_width_page_never_scrolls_sideways(page, registry):
    _seed_layout(registry)
    page.set_viewport_size({"width": 390, "height": 844})
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        expect(_frame_problems(page)).to_have_count(2)
        assert_fits_width(page, "home")
        go(page, "wall")
        page.get_by_role("button", name="Frame silent-b", exact=True).click()
        expect(frame_heading(page, frame_name(registry, "silent-b"))).to_be_visible()
        for tab in ("Overview", "Position", "Picture", "Hardware"):
            page.get_by_role("tab", name=tab, exact=True).click()
            assert_fits_width(page, tab)
        # Every Show page too, not only the last one visited.
        for section in ("now", "scenes", "schedule", "sources"):
            go(page, section)
            assert_fits_width(page, section)


def test_the_console_follows_a_light_colour_scheme(page, registry):
    _seed_attention(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        expect(home_problems(page)).to_be_visible()
        background = "() => getComputedStyle(document.body).backgroundColor"
        page.emulate_media(color_scheme="dark")
        dark = page.evaluate(background)
        page.emulate_media(color_scheme="light")
        assert page.evaluate(background) != dark
