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


# --- Bead 7: calibration direct-manipulation + client convex guard (Plane B) ---
#
# The Commissioning facet gains an "Adjust calibration" draft editor (design
# §J2/§4a). Assertions here are BEHAVIORAL — role/text/visible state — never SVG
# coordinates: a drag ACTION may use pixel coordinates, but every ASSERTION is on
# the visible draft status, the inline convex message, or a control's value. No
# network write exists yet (preview/commit are Bead 8), so an invalid edit is
# proven "sent nothing" by the committed read-back staying put.


def test_calibration_drag_to_convex_updates_draft(page, registry):
    """Dragging a corner handle to a still-convex quad updates the Plane B draft.

    Asserted by the visible draft-status flipping to "Unsaved draft changes"
    (behavioral), never by the handle's coordinates.
    """
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        inspector = _open_commissioning(page)

        editor = inspector.get_by_role("group", name="Adjust calibration")
        expect(editor.get_by_role("status")).to_have_text("Draft matches committed")

        # Drag the top-left corner handle inward — the handle sits at the SVG's
        # padded top-left; the drag ACTION uses coordinates, the ASSERTION does
        # not. (0,0) -> ~ (0.2, 0.2) stays convex.
        svg = inspector.get_by_role("img", name="Calibration editor")
        box = svg.bounding_box()
        start_x, start_y = box["x"] + 16, box["y"] + 16
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        page.mouse.move(start_x + 60, start_y + 60, steps=6)
        page.mouse.up()

        expect(editor.get_by_role("status")).to_have_text("Unsaved draft changes")
        expect(editor.get_by_role("alert")).to_have_count(0)


def test_calibration_folded_quad_snaps_back_no_request(page, registry):
    """A folded quad snaps back with the inline convex message and sends nothing.

    Editing corner 1 to (0.9, 0.9) folds the aperture (min(cross) < 0). The draft
    does not mutate (the handle/input snaps back), the inline message appears, and
    — there being no preview write yet — the COMMITTED read-back is unchanged,
    which is how "no request was sent" is proven.
    """
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        inspector = _open_commissioning(page)

        inspector.get_by_role("spinbutton", name="Corner 1 x").fill("0.9")
        inspector.get_by_role("spinbutton", name="Corner 1 y").fill("0.9")

        expect(
            inspector.get_by_text("corners must form a convex aperture")
        ).to_be_visible()

        # No request was sent: the committed calibration read-back is untouched.
        committed = inspector.get_by_role("group", name="Committed calibration")
        expect(committed).to_contain_text(str(GAIN))


def test_calibration_thin_quad_is_rejected_client_side(page, registry):
    """A `1e-6`-thin quad the server would 400 is rejected client-side.

    Corner 3 y -> 5e-7 leaves min(cross) = 5e-7 <= 1e-6, so the client convex
    guard (convex.js, EPSILON=1e-6, server winding) rejects it. Mutation probe:
    set the client EPSILON to 0 -> this thin quad slips past the client -> the
    inline message never appears -> this test goes RED. Restore -> GREEN.
    """
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        inspector = _open_commissioning(page)

        inspector.get_by_role("spinbutton", name="Corner 3 y").fill("0.0000005")

        expect(
            inspector.get_by_text("corners must form a convex aperture")
        ).to_be_visible()


def test_calibration_draft_survives_snapshot_refresh(page, registry):
    """A snapshot refresh mid-edit leaves the Plane B draft intact (two-plane).

    The operator edits the trying SDR gain; meanwhile committed calibration moves
    underneath (a commit from elsewhere), and a Plane A refresh (Connect) is
    triggered. Plane A visibly updates (the committed read-back shows the NEW
    gain, proving the refresh was real and non-vacuous) while Plane B (the draft
    input) persists — because useDraft is a separate, refresh-proof state cell.

    Mutation probe: have useDraft re-seed from committed on refresh -> the draft
    resets to the newly committed gain -> this test goes RED. Restore -> GREEN.
    """
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        inspector = _open_commissioning(page)

        committed = inspector.get_by_role("group", name="Committed calibration")
        expect(committed).to_contain_text(str(GAIN))  # 1.5, the seeded commit

        gain = inspector.get_by_role("spinbutton", name="SDR gain (draft)")
        gain.fill("1.9")
        expect(gain).to_have_value("1.9")

        # Committed calibration moves underneath the open draft (a commit from
        # another session): revision 2 -> 3, gain 1.5 -> 1.2.
        registry.calibrate(
            FRAME, "commit", expected_revision=2,
            calibration=Calibration(gain=1.2), expected_generation=1)

        # Trigger a Plane A refresh. Plane A updates (committed now 1.2) — the
        # refresh is real — but Plane B (the draft) must NOT be clobbered.
        page.get_by_role("button", name="Connect", exact=True).click()
        expect(committed).to_contain_text("1.2")
        expect(gain).to_have_value("1.9")
