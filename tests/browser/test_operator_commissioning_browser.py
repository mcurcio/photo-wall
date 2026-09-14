"""Behavioral browser checks for the read-only Commissioning facet at /console (Bead 4).

Reuses the existing operator browser harness (real Chromium against the
production create_app on an ephemeral loopback listener, the disposable-schema
`registry` fixture, and the autouse `page_errors` guard). A bound frame is seeded
with a committed SDR gain and a connected OutputReport so the facet joins against
genuine /inventory state.

Every assertion is BEHAVIORAL — role/text/visible state — never SVG coordinates
or DOM structure (design §1c). The facet is READ-ONLY at T0 (editing is Bead 7);
the two hardware areas are default-closed by the derived capability gate (§7.6).
"""

import os

import pytest
from playwright.sync_api import expect
from test_operator_browser import operator_server
from test_registry import ADMIN, enroll

from central.registry import FrameCreate
from contracts.models import Calibration, FrameProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

# A portrait Frame profile: distinct from the OutputReport (1920x1080) so the
# provenance assertions can tell a Frame fact from a live Display readback.
PORTRAIT = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)

FRAME = "commission-frame"
OUTPUT = "HDMI-A-1"
GAIN = 1.5


def _seed(registry):
    """Bind a frame to a connected output and commit a distinctive SDR gain.

    enroll(count=1) reports HDMI-A-1 connected=True, giving a live readback to
    show. bind bumps generation 0->1 and sets calibration_valid=false; the commit
    then writes the committed calibration (gain=1.5, revision 2) the facet reads.
    Returns the bound player id for the binding assertions.
    """
    identity, _key, _request = enroll(registry, count=1)
    player_id = identity["player_id"]

    registry.create_frame(FrameCreate(
        id=FRAME, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    registry.bind(FRAME, player_id, OUTPUT, expected_generation=0)
    # Commit a calibration with a distinctive SDR gain against the current
    # (post-bind) generation/revision so the committed value is read-back real.
    registry.calibrate(
        FRAME, "commit", expected_revision=1,
        calibration=Calibration(gain=GAIN), expected_generation=1)
    return player_id


def _connect(page, origin):
    page.goto(origin + "/console")
    page.get_by_label("Operator token").fill(ADMIN)
    page.get_by_role("button", name="Connect", exact=True).click()


def _open_commissioning(page):
    """Select the seeded frame and open its Commissioning facet; return the facet.

    Placeholder for the R4 rule (probe b): there is no Showrunner mode yet
    (Bead 12), so this only asserts Commissioning is reachable WITHIN the
    Wall/Inspector context. Bead 12 strengthens this to assert it is UNreachable
    in the show layer.
    """
    page.get_by_role("button", name=f"Frame {FRAME}", exact=True).click()
    inspector = page.get_by_role("region", name=f"Frame {FRAME} inspector", exact=True)
    expect(inspector).to_be_visible()
    inspector.get_by_role("tab", name="Commissioning", exact=True).click()
    return inspector


def test_commissioning_shows_committed_gain_and_gates_hardware_off(page, registry):
    player_id = _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        inspector = _open_commissioning(page)

        # (R4 placeholder, probe b) The Commissioning facet is reachable within
        # the Wall/Inspector context. Strengthened to show-layer-unreachable in
        # Bead 12 once Showrunner mode exists.
        calibration = inspector.get_by_role("group", name="Committed calibration")
        expect(calibration).to_be_visible()

        # The committed SDR gain is shown, READ-ONLY, labelled "SDR gain"
        # (never "brightness/color", design §7.3).
        expect(calibration).to_contain_text("SDR gain")
        expect(calibration).to_contain_text(str(GAIN))

        # The bound Player/Output — the equipment path.
        equipment = inspector.get_by_role("group", name="Display equipment")
        expect(equipment).to_contain_text(player_id)
        expect(equipment).to_contain_text(OUTPUT)

        # The two hardware areas render "not yet available" and NO enabled
        # control — the default-closed capability gate (§7.6). The would-be
        # controls do not exist in the DOM.
        expect(inspector.get_by_text("not yet available")).to_have_count(2)
        expect(inspector.get_by_role("button", name="Adjust panel color correction")).to_have_count(0)
        expect(inspector.get_by_role("button", name="Set display power")).to_have_count(0)


def test_commissioning_hardware_areas_are_honest_no_dead_control(page, registry):
    """Honesty probe (a): with the gate closed (T0 default), ONLY the "not yet
    available" notices are present and no enabled hardware control renders.

    Mutation: force `derive` in capability.js to return "derived-true" with no
    wired path -> the GatedArea children (the hardware buttons) render -> the
    button-count assertions below go RED. Restore -> GREEN.
    """
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        inspector = _open_commissioning(page)

        expect(inspector.get_by_text("not yet available")).to_have_count(2)
        # No dead/ungrounded hardware control appears from a bare (would-be) flag.
        expect(inspector.get_by_role("button", name="Adjust panel color correction")).to_have_count(0)
        expect(inspector.get_by_role("button", name="Set display power")).to_have_count(0)


def test_commissioning_provenance_frame_facts_vs_live_readback(page, registry):
    """Provenance probe (c): FrameProfile fields are Frame facts; the ONLY live
    Display readback is OutputReport.

    The frame's diagonal (24 in) is a FrameProfile fact and appears under
    "Frame facts", NOT under "Live Display readback" (OutputReport carries no
    diagonal). Mutation: render a FrameProfile field inside the Live Display
    readback region (mislabel it as live readback) -> the not_to_contain_text
    assertion goes RED. Restore -> GREEN.
    """
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        inspector = _open_commissioning(page)

        frame_facts = inspector.get_by_role("group", name="Frame facts")
        # The diagonal is a Frame fact (operator-declared FrameProfile), so it is
        # shown as a Frame fact.
        expect(frame_facts).to_contain_text("24")

        live = inspector.get_by_role("group", name="Live Display readback")
        # The one live Display readback is OutputReport.connected.
        expect(live).to_contain_text("Connected")
        # Provenance: a FrameProfile-only fact (diagonal) must NOT appear as a
        # live Display readback — OutputReport has no diagonal.
        expect(live).not_to_contain_text("24")
