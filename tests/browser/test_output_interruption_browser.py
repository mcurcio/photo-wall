"""Output interrupted, end to end (console DDD §15, bead C1).

The production app on the loopback harness, with a real Output loss recorded through
Central's own node owner services and Runtime reconciliation (tests/test_node_runtime_
reconciliation.py `rig`): two Frames bound to one Player's two Outputs inside one Run, and
Display Host reporting Output 0's surface invalidated. Central serves the loss on the operator
snapshot (current Bindings only), and the one classifier shows it on the plan tile, the Run's
Frame chips, Attention and the Player page's Output row. Every assertion is behavioural.
"""

import os
import re

import pytest
from console_tasks import connect, go, open_player
from operator_harness import operator_server, tile_health
from playwright.sync_api import expect
from test_fleet_attempts import SERIAL
from test_node_runtime_reconciliation import rig
from test_operator_output_interruptions import _lose_first_output
from test_registry import frame

from central.registry import FramePlacement

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

NAME = f"Player …{SERIAL[-6:]}"
LOST, KEPT = "node-f0", "node-f1"
FULL = ("Output interrupted (Central's inference: Display Host reported the app surface invalidated"
        " or withdrawn · recorded 0 s ago) · the Run continues")
SHORT = "Output interrupted · the Run continues"


def _seed(registry):
    """rig's two bound Frames, placed on the plan, with Output 0's loss recorded."""
    _, player, _, sessions, claims, grants, surfaces, reconciler, _ = rig(registry)
    for index, frame_id in enumerate((LOST, KEPT)):
        registry.place_frame(frame_id, FramePlacement(x_mm=100 + 500 * index, y_mm=100))
    _lose_first_output(sessions, claims, grants, surfaces, reconciler)
    return player["player_id"], surfaces[0]


def _attention(page):
    strip = page.get_by_role("region", name="Wall attention", exact=True)
    toggle = strip.get_by_role("button", name=re.compile(r"^(Show|Hide) list$"))
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
    return strip.get_by_role("list", name="Frames and Players needing attention", exact=True)


def test_a_served_interruption_shows_on_the_tile_run_chip_attention_and_output_row(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        lost = tile_health(page, LOST)
        expect(lost).to_have_text(SHORT)
        expect(lost).to_have_accessible_name(FULL)
        expect(lost).to_have_class(re.compile(r"\bhealth--alarm\b"))
        # The other Output's Frame, in the same Run on the same Player, shows nothing of it.
        expect(tile_health(page, KEPT)).to_have_text("Heard recently")

        expect(_attention(page)).to_contain_text(f"{LOST} — {FULL}")
        expect(_attention(page)).not_to_contain_text(f"{KEPT} —")
        page.keyboard.press("Escape")  # the list overlays the page

        go(page, "now")
        expect(page.get_by_text(f"{LOST}: {SHORT}", exact=True).first).to_be_visible()
        expect(page.get_by_text(f"{KEPT}: Heard recently", exact=True).first).to_be_visible()

        open_player(page, NAME)
        outputs = page.get_by_role("list", name=f"Outputs of {NAME}", exact=True)
        rows = outputs.get_by_role("listitem")
        expect(rows.filter(has_text="HDMI-A-1")).to_contain_text(f"Interruption: {FULL}")
        expect(rows.filter(has_text="HDMI-A-2")).not_to_contain_text("interrupted")


def test_a_frame_newly_bound_to_the_lost_output_shows_nothing(page, registry):
    player_id, old = _seed(registry)
    registry.unbind(LOST, expected_generation=old.binding_generation)
    frame(registry, "replacement")
    registry.place_frame("replacement", FramePlacement(x_mm=1100, y_mm=100))
    registry.bind("replacement", player_id, old.output.output_id, expected_generation=0)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        replacement = tile_health(page, "replacement")
        expect(replacement).to_have_text("Needs calibration")
        expect(replacement).not_to_have_accessible_name(re.compile("interrupted"))
        # Needs calibration is the Wall's To finish item, not an incident (console DDD G2),
        # and nothing anywhere claims the old interruption for it.
        expect(page.get_by_role("list", name="To finish", exact=True)).to_contain_text(
            "replacement · needs calibration")
        expect(page.get_by_role("region", name="Wall attention", exact=True)).not_to_contain_text(
            "interrupted")
        open_player(page, NAME)
        outputs = page.get_by_role("list", name=f"Outputs of {NAME}", exact=True)
        expect(outputs).to_contain_text("Bound to Frame replacement")
        expect(outputs).not_to_contain_text("Interruption")
