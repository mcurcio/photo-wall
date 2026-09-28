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
from operator_harness import (
    RequestGate,
    operator_server,
    pause_page_clock,
    report_readiness,
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
INVENTORY = "**/v1/operator/inventory"


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


def _connect(page, origin):
    page.goto(origin + "/console")
    page.get_by_label("Operator token").fill(ADMIN)
    page.get_by_role("button", name="Connect", exact=True).click()


def _tile(page, frame_id=FRAME):
    return page.get_by_role("group", name=f"Frame {frame_id} status", exact=True)


def _severity(level):
    return re.compile(rf"\bhealth--{level}\b")


def test_a_reporting_player_reads_last_heard_with_centrals_age(page, registry):
    player_id = _bound_frame(registry)
    report_readiness(registry, player_id)
    registry.clock.advance(2)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        label = _tile(page).get_by_text("Last heard 2 s ago", exact=True)
        expect(label).to_be_visible()
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
        _connect(page, origin)
        label = _tile(page).get_by_text("Player silent · last heard 40 s ago", exact=True)
        expect(label).to_be_visible()
        expect(label).to_have_class(_severity("alarm"))


def test_an_enrolled_player_without_a_report_never_reads_ok(page, registry):
    _bound_frame(registry, "fresh", x_mm=100)
    registry.clock.advance(40)
    _bound_frame(registry, "overdue-pending", x_mm=500)  # enrolled 40 s after "fresh"
    registry.clock.advance(5)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        # Enrolled 5 s ago: a to-do, not ok, although commissioned and displayed.
        recent = _tile(page, "overdue-pending").get_by_text(
            "Enrolled 5 s ago, no report yet", exact=True)
        expect(recent).to_be_visible()
        expect(recent).to_have_class(_severity("todo"))
        # Enrolled 45 s ago and still no report: past the threshold it is an alarm.
        stale = _tile(page, "fresh").get_by_text("Enrolled 45 s ago, no report yet", exact=True)
        expect(stale).to_be_visible()
        expect(stale).to_have_class(_severity("alarm"))


def test_a_never_commissioned_frame_is_a_todo_not_an_alarm(page, registry):
    player_id = _bound_frame(registry, commissioned=False)
    report_readiness(registry, player_id)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        label = _tile(page).get_by_text("Needs commissioning", exact=True)
        expect(label).to_be_visible()
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
        _connect(page, origin)
        expect(_tile(page, "newer").get_by_text("Last heard 25 s ago", exact=True)).to_be_visible()
        expect(_tile(page, "older")).to_contain_text("Player silent · last heard 32 s ago")


# --- Polling and the write fence (pass 2 §7). The page clock is paused, so the 5 s poll
# fires only when the test runs the clock; slow responses are held with RequestGate.


def _paused_connect(page, registry, origin):
    pause_page_clock(page, registry.clock.utc())
    _connect(page, origin)


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
        _paused_connect(page, registry, origin)
        expect(_tile(page)).to_contain_text("Enrolled 0 s ago, no report yet")
        report_readiness(registry, player_id)
        page.clock.run_for(5000)
        expect(_tile(page)).to_contain_text("Last heard 0 s ago")


def test_a_hidden_tab_does_not_poll_and_refreshes_on_return(page, registry):
    _bound_frame(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _paused_connect(page, registry, origin)
        expect(_tile(page)).to_be_visible()
        gate = RequestGate(page, INVENTORY)
        _set_visibility(page, "hidden")
        page.clock.run_for(30000)
        _settle(page)
        assert gate.seen == 0, "a hidden tab polled the inventory"
        _set_visibility(page, "visible")
        deadline = time.monotonic() + 5
        while gate.seen == 0 and time.monotonic() < deadline:
            page.wait_for_timeout(20)
        assert gate.seen == 1, "returning to the tab did not refresh at once"


def test_a_stale_poll_is_dropped_after_a_newer_refresh(page, registry):
    player_id = _bound_frame(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _paused_connect(page, registry, origin)
        expect(_tile(page)).to_contain_text("Enrolled 0 s ago, no report yet")
        stale = page.request.get(origin + "/v1/operator/inventory",
                                 headers={"Authorization": "Bearer " + ADMIN}).text()
        gate = RequestGate(page, INVENTORY)
        gate.holding = True
        page.clock.run_for(5000)
        gate.wait_held()
        gate.holding = False

        report_readiness(registry, player_id)
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(_tile(page)).to_contain_text("Last heard 0 s ago")

        # The poll started first but answers last, with what it read before the report.
        gate.release(status=200, content_type="application/json", body=stale)
        _settle(page)
        expect(_tile(page)).to_contain_text("Last heard 0 s ago")


def test_a_stale_401_does_not_log_the_operator_out(page, registry):
    player_id = _bound_frame(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _paused_connect(page, registry, origin)
        expect(_tile(page)).to_be_visible()
        gate = RequestGate(page, INVENTORY)
        gate.holding = True
        page.clock.run_for(5000)
        gate.wait_held()
        gate.holding = False
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(page.get_by_text(re.compile(r"updated 0 s ago"))).to_be_visible()

        gate.release(status=401, content_type="application/json",
                     body='{"error": "unauthorized"}')
        _settle(page)
        expect(page.get_by_text("Operator token was not accepted", exact=False)).to_have_count(0)
        expect(page.get_by_text("last refresh failed", exact=False)).to_have_count(0)
        # The token is still held: the next poll reads and applies.
        report_readiness(registry, player_id)
        page.clock.run_for(5000)
        expect(_tile(page)).to_contain_text("Last heard 0 s ago")


def test_a_poll_in_flight_when_a_bind_completes_is_dropped_and_polling_continues(
        page, registry):
    registry.create_frame(FrameCreate(
        id=FRAME, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    identity, _key, _request = enroll(registry, count=1)
    with operator_server(registry.db, registry.clock) as origin:
        _paused_connect(page, registry, origin)
        expect(_tile(page)).to_contain_text("Needs a Player")
        page.get_by_role("button", name=f"Frame {FRAME}", exact=True).click()
        inspector = page.get_by_role("region", name=f"Frame {FRAME} inspector", exact=True)
        inspector.get_by_role("tab", name="Binding", exact=True).click()

        writes = RequestGate(page, "**/v1/operator/frames/*/binding")
        reads = RequestGate(page, INVENTORY)
        writes.holding = True
        inspector.get_by_role("button", name="Bind pending display", exact=True).click()
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
        expect(_tile(page)).to_contain_text("Needs a Player")
        # The refresh the bind issued lands.
        reads.release(0)
        expect(_tile(page)).to_contain_text("Enrolled 0 s ago, no report yet")

        # The dropped poll released its slot: the next tick reads and applies.
        report_readiness(registry, identity["player_id"])
        page.clock.run_for(5000)
        expect(_tile(page)).to_contain_text("Needs commissioning")
