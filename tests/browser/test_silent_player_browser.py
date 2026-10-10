"""The console on the 2026-10-09 incident's state (1b C4, owner q3: "Central notices a silent
Player"): the production app with node management mounted, over the state
tests/test_central_silent_player.py builds through Central's owner services (`Incident`), read
at T + SILENT_AFTER_SECONDS + 1 on the registry's controlled clock. The Wall marks the Frame with
the liveness cause and links to the Player's page; there the Player app reads silent while Host
Management reads current, and the App Effect Broker's exit is shown.

The link lands on the Pi's Software and screens page (`#/players/<device>`), which shows the
Player app's last report with its age (no verdict: the Wall and the Frame page word the
silence); Host Management's reading is on the Pi's Hardware page, one link away."""

import os
import re

import pytest
from console_tasks import connect, open_frame
from operator_harness import operator_server, tile_health
from playwright.sync_api import expect
from test_central_silent_player import FRAME, NODE, Incident
from test_fleet_attempts import SERIAL

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

NAME = f"Player …{SERIAL[-6:]}"


def test_the_wall_marks_a_silent_players_frame_and_its_pages_show_the_apps_age_and_the_host_current(
        page, registry):
    Incident(registry)
    with operator_server(registry.db, registry.clock, node_control=NODE) as origin:
        connect(page, origin, "wall")
        label = tile_health(page, FRAME)
        expect(label).to_have_text("Player app silent")
        expect(label).to_have_accessible_name("Player app silent · last reported 32 s ago")
        expect(label).to_have_class(re.compile(r"\bhealth--alarm\b"))

        inspector = open_frame(page, FRAME, "hardware")
        expect(inspector).to_contain_text("The Pi: Not heard from lately")
        inspector.get_by_role("link", name="See why on its page", exact=True).click()
        expect(page.get_by_role("heading", level=1, name="Software and screens", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", level=2, name=NAME, exact=True)).to_be_visible()
        layers = page.get_by_role("region", name="Layers", exact=True)
        expect(layers.get_by_role("group", name="Player app", exact=True)).to_contain_text(
            "Last reported: Player app last reported 32 s ago")
        expect(layers.get_by_role("group", name="App Effect Broker", exact=True)).to_contain_text(
            "App process: App Effect Broker reported the app exited · first received 30 s ago")

        page.get_by_role("navigation", name=f"Other pages of {NAME}", exact=True).get_by_role(
            "link", name="Hardware", exact=True).click()
        expect(page.get_by_role("heading", level=1, name="Hardware", exact=True)).to_be_visible()
        expect(page.get_by_role("region", name="Health", exact=True)).to_contain_text(
            "Host Management: Host Management last reported 0 s ago")
        expect(page.get_by_role("region", name="Link and sessions", exact=True).get_by_role(
            "group", name="Host Management", exact=True)).to_contain_text(
            "Last reported: Host Management last reported 0 s ago")
        expect(page.get_by_text(re.compile("Host Management silent"))).to_have_count(0)
