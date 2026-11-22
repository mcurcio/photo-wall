"""Output interrupted, end to end (console DDD §15, bead C1).

The production app on the loopback harness, with a real Output loss recorded through
Central's own services (tests/support/node_rig.py `rig`): two Frames bound to one Player's two
Outputs inside one Run, and Display Host reporting Output 0 disconnected on its display line. Central serves the loss on the operator
snapshot (current Bindings only), and the Player page's Output row states it. Since decision 0036
a Frame's tile, the Run's Frame chips and Home say only the status Central composes, whose table
has no row for an Output's interruption: they claim nothing about it (and never Showing for a Frame
whose Pi sent no report). Every assertion is behavioural.
"""

import os
import re

import pytest
from console_tasks import connect, go, open_player
from operator_harness import operator_server, tile_health
from playwright.sync_api import expect
from support.node_rig import rig
from support.output_report import report_modes
from support.player_state import heard_in
from test_fleet_attempts import DEVICE_ID, SERIAL
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


def _seed(registry):
    """rig's two bound Frames, placed on the plan, with Output 0's loss recorded, and the Player
    heard (its session socket: liveness, central/infra/player_contact_store.py)."""
    _, player, _, surfaces = rig(registry)
    with registry.db.transaction() as conn:
        heard_in(conn, player["player_id"], player["authority_epoch"], registry.clock.utc())
    for index, frame_id in enumerate((LOST, KEPT)):
        registry.place_frame(frame_id, FramePlacement(x_mm=100 + 500 * index, y_mm=100), by="console")
    report_modes(registry, DEVICE_ID, (surfaces[0].output.output_id,), None)  # the display line
    return player["player_id"], surfaces[0]


NO_REPORT = re.compile(r"^Can't tell since \d\d:\d\d(:\d\d)?( UTC)? · Its Pi has sent no report about this Frame\.$")


def test_a_served_interruption_shows_on_the_output_row_and_no_tile_claims_the_frame_shows(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        # The tile says Central's status: its Pi is heard and sent no report about the Frame, so
        # nothing is claimed of either Frame, the interrupted one least of all.
        for frame_id in (LOST, KEPT):
            health = tile_health(page, frame_id)
            expect(health).to_have_text("Can't tell")
            expect(health).to_have_accessible_name(NO_REPORT)
            expect(health).to_have_class(re.compile(r"\bhealth--unknown\b"))

        go(page, "now")
        for frame_id in (LOST, KEPT):
            expect(page.get_by_text(f"{frame_id}: Can't tell", exact=True).first).to_be_visible()

        # The interruption itself is stated where the Output is: the Pi's page, on that Output alone.
        open_player(page, NAME)
        outputs = page.get_by_role("list", name=f"Outputs of {NAME}", exact=True)
        rows = outputs.get_by_role("listitem")
        expect(rows.filter(has_text="HDMI-A-1")).to_contain_text(f"Interruption: {FULL}")
        expect(rows.filter(has_text="HDMI-A-2")).not_to_contain_text("interrupted")


def test_a_frame_newly_bound_to_the_lost_output_shows_nothing(page, registry):
    player_id, old = _seed(registry)
    registry.unbind(LOST, expected_generation=old.binding_generation, by="console")
    frame(registry, "replacement")
    registry.place_frame("replacement", FramePlacement(x_mm=1100, y_mm=100), by="console")
    registry.bind("replacement", player_id, old.output.output_id, expected_generation=0, by="console")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        replacement = tile_health(page, "replacement")
        expect(replacement).to_have_text("Check its Position")
        expect(replacement).not_to_have_accessible_name(re.compile("interrupted"))
        # Its Position to check is the Wall's To finish item, not a problem (console DDD G2),
        # and nothing anywhere claims the old interruption for it.
        expect(page.get_by_role("list", name="To finish", exact=True)).to_contain_text(
            "replacement · needs calibration")
        go(page, "home")
        expect(page.get_by_role("main").get_by_role("heading", level=1, name="Home", exact=True)).to_be_visible()
        expect(page.get_by_role("main")).not_to_contain_text("interrupted")
        open_player(page, NAME)
        outputs = page.get_by_role("list", name=f"Outputs of {NAME}", exact=True)
        expect(outputs).to_contain_text("Bound to Frame replacement")
        expect(outputs).not_to_contain_text("Interruption")
