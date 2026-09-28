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

import pytest
from operator_harness import operator_server, report_readiness
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
