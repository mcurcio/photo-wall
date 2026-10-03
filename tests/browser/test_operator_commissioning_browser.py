"""Behavioral browser checks for the Wall's Calibration facet at /console (console DDD §19-§20).

Reuses the existing operator browser harness (real Chromium against the
production create_app on an ephemeral loopback listener, the disposable-schema
`registry` fixture, and the autouse `page_errors` guard). A bound frame is seeded
with a committed SDR gain and a connected OutputReport so the facet joins against
genuine /inventory state.

Every assertion is BEHAVIORAL — role/text/visible state — never SVG coordinates
or DOM structure (design §1c). Live calibration has two paths, chosen by Central's served
calibration capability: without acknowledgment (`legacy_preview`, the real server here) and
with Display Host acknowledgment (`native_trial`, whose answers a test stubs at the network).
"""

import os
import re
import time

import pytest
from console_tasks import open_frame, visit
from operator_harness import drive_poll, inventory, operator_server, pause_page_clock, sign_in
from playwright.sync_api import expect
from test_registry import enroll

from central.registry import FrameCreate
from contracts.models import Calibration, FrameProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

# A portrait Frame profile: distinct from the OutputReport (1920x1080) so the
# provenance assertions can tell a Frame profile fact from the Panel record.
PORTRAIT = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)

FRAME = "commission-frame"
OUTPUT = "HDMI-A-1"
GAIN = 1.5


def _seed(registry, profile=PORTRAIT, rotation=0):
    """Bind a frame to a connected output and commit a distinctive SDR gain.

    enroll(count=1) reports HDMI-A-1 connected=True, giving a Panel record to
    show. bind bumps generation 0->1 and sets calibration_valid=false; the commit
    then writes the committed calibration (gain=1.5, revision 2) the facet reads.
    Returns the bound player id for the binding assertions.
    """
    identity, _key, _request = enroll(registry, count=1)
    player_id = identity["player_id"]

    landscape = profile.width_px > profile.height_px
    registry.create_frame(FrameCreate(
        id=FRAME, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=500 if landscape else 300,
        height_mm=300 if landscape else 500, profile=profile))
    registry.bind(FRAME, player_id, OUTPUT, expected_generation=0)
    # Commit a calibration with a distinctive SDR gain against the current
    # (post-bind) generation/revision so the committed value is read-back real.
    registry.calibrate(
        FRAME, "commit", expected_revision=1,
        calibration=Calibration(gain=GAIN, rotation=rotation), expected_generation=1)
    return player_id


def test_calibration_shows_committed_gain_and_no_equipment_or_gated_area(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")

        # R4's positive control in a browser: the Calibration facet is reachable in
        # the Wall's Inspector. That the Show and neutral pages never reach it is
        # tests/test_console_routes_r4.py (the module graph) and the sample-path visits
        # in tests/browser/test_console_shell_browser.py.
        expect(inspector.get_by_role("tablist", name="Inspector facets").get_by_role("tab")).to_have_text(
            ["Status", "Binding", "Calibration"])
        calibration = inspector.get_by_role("group", name="Committed calibration")
        expect(calibration).to_be_visible()

        # The committed SDR gain is shown, READ-ONLY, labelled "SDR gain"
        # (never "brightness/color", design §7.3).
        expect(calibration).to_contain_text("SDR gain")
        expect(calibration).to_contain_text(str(GAIN))

        # A committed calibration on this binding hands directly into Scene
        # authoring with the persistent Frame explicitly selected.
        content_link = inspector.get_by_role(
            "link", name="Choose content for this Frame", exact=True)
        expect(content_link).to_be_visible()
        expect(content_link).to_have_attribute(
            "href", "#/scenes/new/kind?target=commission-frame")

        # The equipment block moved to Binding (console DDD §19), and the two capability-gated
        # placeholders are gone with no control in their place.
        expect(inspector.get_by_role("group", name="Bound Output")).to_have_count(0)
        expect(inspector.get_by_role("group", name=re.compile("Panel at"))).to_have_count(0)
        expect(inspector.get_by_text("not yet available")).to_have_count(0)
        expect(inspector.get_by_role("button", name="Adjust panel color correction")).to_have_count(0)
        expect(inspector.get_by_role("button", name="Set display power")).to_have_count(0)


def test_the_old_commissioning_bookmark_opens_calibration(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        visit(page, f"#/wall/frames/{FRAME}/commissioning")
        inspector = page.get_by_role("region", name=f"Frame {FRAME} inspector", exact=True)
        expect(inspector.get_by_role("tab", name="Calibration", exact=True)).to_have_attribute(
            "aria-selected", "true")
        expect(inspector.get_by_role("group", name="Committed calibration")).to_be_visible()
        expect(page.get_by_role("tab", name="Commissioning")).to_have_count(0)


def test_calibration_handoff_waits_for_valid_calibration(page, registry):
    identity, _key, _request = enroll(registry, count=1)
    registry.create_frame(FrameCreate(
        id=FRAME, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    registry.bind(FRAME, identity["player_id"], OUTPUT, expected_generation=0)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")
        expect(inspector.get_by_role(
            "link", name="Choose content for this Frame", exact=True)).to_have_count(0)


def test_an_unbound_frame_is_sent_to_the_binding_facet(page, registry):
    registry.create_frame(FrameCreate(
        id=FRAME, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")
        expect(inspector.get_by_role("status").filter(
            has_text="No Output bound. Bind one on the Binding facet.")).to_be_visible()


def test_calibration_provenance_frame_profile_vs_panel_at_enrollment(page, registry):
    """Provenance probe (c): FrameProfile fields are the Frame profile, on Calibration; the
    ONLY Panel record is OutputReport, sent at the Player app's last enrollment (not a live
    readback), on Binding (console DDD §19).

    The frame's diagonal (24 in) is a FrameProfile fact and appears under "Frame profile",
    NOT under the Binding facet's Panel at enrollment (OutputReport carries no diagonal).
    Mutation: render a FrameProfile field inside the Panel region -> the not_to_contain_text
    assertion goes RED. Restore -> GREEN.
    """
    player_id = _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")

        frame_profile = inspector.get_by_role("group", name="Frame profile")
        # The diagonal is operator-declared (FrameProfile), so it is the Frame profile's.
        expect(frame_profile).to_contain_text("24")

        inspector = open_frame(page, FRAME, "binding")
        # The bound Output and its Player, which links to the Player's home.
        expect(inspector.get_by_role("link", name=player_id, exact=True)).to_have_attribute(
            "href", re.compile(r"^#/players/device-"))
        expect(inspector).to_contain_text(OUTPUT)
        panel = inspector.get_by_role(
            "group", name="Panel at the Player app's last enrollment (may be stale)")
        # The one Panel record is OutputReport.connected, from the last enrollment.
        expect(panel).to_contain_text(
            "Player app reported Panel connected at the Player app's last enrollment (may be stale)")
        expect(panel).to_contain_text("Output resolution at that enrollment: 1920 × 1080")
        # Provenance: a FrameProfile-only fact (diagonal) must NOT appear as a Panel record.
        expect(panel).not_to_contain_text("24")


def test_calibration_warns_on_reported_resolution_mismatch_without_mutating_profile(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")

        warning = inspector.get_by_role("status").filter(
            has_text="The Player app reported 1920 × 1080 at its last enrollment"
        )
        expect(warning).to_be_visible()
        expect(warning).to_contain_text("this record may be stale")
        expect(warning).to_contain_text("restart the Player app if the Panel changed")
        expect(warning).to_contain_text("The Frame profile is 1080 × 1920")
        expect(warning).to_contain_text("Committed rotation 0° was considered")
        expect(warning).to_contain_text("unbind this Frame, edit its profile, then bind and calibrate it")

        facts = inspector.get_by_role("group", name="Frame profile")
        expect(facts).to_contain_text("1080 px")
        expect(facts).to_contain_text("1920 px")
        expect(inspector.get_by_role("group", name="Adjust calibration")).to_be_visible()
        stored = next(frame for frame in inventory(page, origin).frames if frame.id == FRAME)
        assert (stored.profile.width_px, stored.profile.height_px) == (1080, 1920)


def test_calibration_does_not_warn_when_reported_resolution_matches(page, registry):
    _seed(registry, FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24))
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")

        expect(inspector.get_by_role("status").filter(
            has_text="The Player app reported 1920 × 1080 at its last enrollment"
        )).to_have_count(0)
        expect(inspector.get_by_role("group", name="Adjust calibration")).to_be_visible()
        stored = next(frame for frame in inventory(page, origin).frames if frame.id == FRAME)
        assert (stored.profile.width_px, stored.profile.height_px) == (1920, 1080)


def test_calibration_accepts_reported_resolution_swapped_by_quarter_turn(page, registry):
    _seed(registry, rotation=90)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")

        expect(inspector.get_by_role("status").filter(
            has_text="The Player app reported 1920 × 1080 at its last enrollment"
        )).to_have_count(0)
        expect(inspector.get_by_role("group", name="Committed calibration")).to_contain_text("90°")
        expect(inspector.get_by_role("group", name="Adjust calibration")).to_be_visible()
        stored = next(frame for frame in inventory(page, origin).frames if frame.id == FRAME)
        assert (stored.profile.width_px, stored.profile.height_px) == (1080, 1920)


def test_calibration_does_not_trust_rotation_after_binding_invalidates_calibration(page, registry):
    player_id = _seed(registry, rotation=90)
    registry.unbind(FRAME, expected_generation=1)
    registry.bind(FRAME, player_id, OUTPUT, expected_generation=2)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")
        warning = inspector.get_by_role("status").filter(
            has_text="The Player app reported 1920 × 1080 at its last enrollment"
        )
        expect(warning).to_contain_text("Calibration is not yet valid for this binding")
        expect(inspector.get_by_role("group", name="Adjust calibration")).to_be_visible()


# --- Bead 7: calibration direct-manipulation + client convex guard (Plane B) ---
#
# The Calibration facet carries an "Adjust calibration" draft editor (design
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
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")

        editor = inspector.get_by_role("group", name="Adjust calibration")
        expect(editor.get_by_role("status")).to_have_text("Draft matches committed")

        # Drag the top-left corner handle inward — the handle sits at the SVG's
        # padded top-left; the drag ACTION uses coordinates, the ASSERTION does
        # not. (0,0) -> ~ (0.2, 0.2) stays convex.
        svg = inspector.get_by_role("img", name="Calibration editor")
        svg.scroll_into_view_if_needed()
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
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")

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
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")

        inspector.get_by_role("spinbutton", name="Corner 3 y").fill("0.0000005")

        expect(
            inspector.get_by_text("corners must form a convex aperture")
        ).to_be_visible()


def test_calibration_draft_survives_snapshot_refresh(page, registry):
    """A snapshot refresh mid-edit leaves the Plane B draft intact (two-plane).

    The operator edits the trying SDR gain; meanwhile committed calibration moves
    underneath (a commit from elsewhere), and a Plane A refresh (Refresh) is
    triggered. Plane A visibly updates (the committed read-back shows the NEW
    gain, proving the refresh was real and non-vacuous) while Plane B (the draft
    input) persists — because useDraft is a separate, refresh-proof state cell.

    Mutation probe: have useDraft re-seed from committed on refresh -> the draft
    resets to the newly committed gain -> this test goes RED. Restore -> GREEN.
    """
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")

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
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(committed).to_contain_text("1.2")
        expect(gain).to_have_value("1.9")


# --- Bead 8: preview/commit/revert + honest lease countdown + conflict states ---
#
# Plane B gains NETWORK writes against the existing
# POST /v1/operator/frames/{id}/calibration (design §4b/§6b/J2). The 30s lease is
# exercised DETERMINISTICALLY via the ManualClock the `registry` fixture builds
# (conftest.py:40) and that operator_server hands straight to create_app — so
# advancing `registry.clock` advances the server's clock, and lease expiry is
# observed through the SERVER state a poll reads (effective_calibration reverting
# to committed once preview_expires <= clock.utc()), never by a wall-clock wait.
#
# HIGH-RISK concurrency invariants proven here: both tokens ride every op (a stale
# commit 409s, never silently succeeds); the countdown is the server's expires_at
# (advancing the server clock past it does NOT tick the client countdown to zero —
# only the poll flips the panel to expired); a refresh never clobbers Plane B
# (Re-preview reuses the retained trying values).


def _sync_clock(registry):
    """Advance the fixture's ManualClock (starts at 1000) to real wall time.

    The lease countdown displays `expires_at - Date.now()` (design §6b): with the
    server clock parked at 1000 the browser's real-time clock would read the lease
    as long expired. Syncing to time.time() (the same wall clock the browser reads)
    makes the countdown honest (~30s) while leaving the deterministic
    `clock.advance(31)` expiry mechanism intact.
    """
    delta = time.time() - registry.clock.utc()
    if delta > 0:
        registry.clock.advance(delta)


def _lease(inspector):
    return inspector.get_by_role("group", name="Live calibration")


def test_calibration_preview_shows_server_lease_countdown(page, registry):
    """Preview holds the lease and shows a visible, server-driven countdown.

    Status is "previewing" (the countdown timer is present) and the countdown is
    derived from the server's expires_at (= now + 30), not a hardcoded per-tab
    timer.
    """
    _seed(registry)
    _sync_clock(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")
        lease = _lease(inspector)

        lease.get_by_role("button", name="Show on the Panel", exact=True).click()

        timer = inspector.get_by_role("timer", name="Live calibration countdown")
        expect(timer).to_be_visible()
        expect(timer).to_contain_text("live calibration ends in")
        # The panel is genuinely previewing on the server (preview slot set).
        assert inventory(page, origin).frames[0].preview is not None


def test_calibration_preview_keeps_its_draft_and_countdown_across_polls(page, registry):
    """Pass 2 §7: the console's one 5 s poll replaces Plane A while a preview is held; the
    draft (Plane B) and the server-driven countdown survive, and the panel stays previewing.
    The page clock is paused at the server's clock, so the countdown is exact."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")
        gain = inspector.get_by_role("spinbutton", name="SDR gain (draft)")
        gain.fill("1.9")
        _lease(inspector).get_by_role("button", name="Show on the Panel", exact=True).click()
        timer = inspector.get_by_role("timer", name="Live calibration countdown")
        expect(timer).to_contain_text("live calibration ends in 29 s")

        for _ in range(2):
            drive_poll(page)
        expect(timer).to_contain_text("live calibration ends in 19 s")
        expect(gain).to_have_value("1.9")
        expect(inspector.get_by_role("alert")).to_have_count(0)
        assert inventory(page, origin).frames[0].preview is not None


def test_calibration_lease_expiry_reverts_to_committed_no_auto_renew(page, registry):
    """On lease expiry the panel reverts to committed and the UI says so.

    The server clock is advanced past preview_expires; the poll's /inventory then
    reads preview == null (the lease lapsed), so status flips to "expired" and the
    explicit "panel is back on committed" banner appears. The trying values remain
    in Plane B (Re-preview is offered) and committed is unchanged.

    Mutation probe (no-auto-renew): make the expiry branch of useCalibration
    re-issue `preview` instead of setting status "expired" -> the panel does not
    revert, the "panel is back on committed" banner never renders -> this test goes
    RED. Restore -> GREEN.
    """
    _seed(registry)
    _sync_clock(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")
        lease = _lease(inspector)

        lease.get_by_role("button", name="Show on the Panel", exact=True).click()
        expect(inspector.get_by_role("timer", name="Live calibration countdown")).to_be_visible()

        # Advance the SERVER clock past the 30s lease. The client countdown does
        # not move (server-authoritative expiry, not countdown-driven).
        registry.clock.advance(31)
        # Deterministically drive the overtake/expiry poll via a Plane A refresh.
        page.get_by_role("button", name="Refresh", exact=True).click()

        expect(lease.get_by_role("alert")).to_contain_text("Live calibration expired; your draft is kept")
        # Trying retained in Plane B -> Show again offered.
        expect(lease.get_by_role("button", name="Show again", exact=True)).to_be_visible()
        # Committed calibration is unchanged; the server reverted the preview.
        expect(inspector.get_by_role("group", name="Committed calibration")).to_contain_text(str(GAIN))
        assert inventory(page, origin).frames[0].preview is None


def test_calibration_stale_commit_conflicts_on_revision(page, registry):
    """A commit against a stale revision is refused with the exact 409 message.

    Another session commits (revision 2 -> 3) under the open facet; this session
    still holds baseline revision 2, so its commit carries expected_revision=2 and
    the server returns calibration_revision_conflict.

    Mutation probe (both-tokens): drop expected_revision from the commit payload in
    useCalibration -> the server 422s (invalid_request) instead of returning the
    revision conflict -> the revision-specific banner never renders -> this test
    goes RED. Restore -> GREEN. (A required server token means the concrete failure
    is a 422 rather than a silent success; the invariant — a stale commit is never
    accepted — holds either way.)
    """
    _seed(registry)
    _sync_clock(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")
        lease = _lease(inspector)

        # Out-of-band commit advances revision under this session.
        registry.calibrate(
            FRAME, "commit", expected_revision=2,
            calibration=Calibration(gain=1.2), expected_generation=1)

        lease.get_by_role("button", name="Save without acknowledgment", exact=True).click()

        # The revision-specific copy (design §4b), unique to this conflict.
        expect(lease.get_by_role("alert")).to_contain_text(
            "Another session changed this frame's calibration"
        )


def test_calibration_stale_commit_conflicts_on_generation(page, registry):
    """A commit after a binding change is refused with the generation 409 message.

    An unbind bumps generation 1 -> 2 under the open facet; this session's commit
    still carries expected_generation=1, so the server returns
    binding_generation_conflict (checked before the binding/frame_unbound guard).
    """
    _seed(registry)
    _sync_clock(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")
        lease = _lease(inspector)

        # A binding change bumps generation under this session.
        registry.unbind(FRAME, expected_generation=1)

        lease.get_by_role("button", name="Save without acknowledgment", exact=True).click()

        expect(lease.get_by_role("alert")).to_contain_text("no longer under your control")


def test_calibration_overtaken_detected_by_inventory_poll(page, registry):
    """An out-of-band revision advance is surfaced by the poll as "overtaken".

    While previewing, another session commits (revision 2 -> 3). The inventory
    poll (driven here by a Plane A refresh) sees the token advance under the
    baseline and flips status to "overtaken" with the design §4b copy.
    """
    _seed(registry)
    _sync_clock(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")
        lease = _lease(inspector)

        lease.get_by_role("button", name="Show on the Panel", exact=True).click()
        expect(inspector.get_by_role("timer", name="Live calibration countdown")).to_be_visible()

        registry.calibrate(
            FRAME, "commit", expected_revision=2,
            calibration=Calibration(gain=1.2), expected_generation=1)
        # Drive the overtake poll deterministically via a Plane A refresh.
        page.get_by_role("button", name="Refresh", exact=True).click()

        expect(lease.get_by_role("alert")).to_contain_text(
            "Someone saved a calibration for this Frame meanwhile; review it, then start again")


def test_calibration_foreign_preview_overtakes_by_inventory_poll(page, registry):
    """A SECOND tab's preview overtaking the single slot is surfaced as "overtaken".

    The overtake trigger in design §4b includes `configuration_revision` advancing
    during calibration — not just `revision`/`generation`. A foreign PREVIEW takes
    the single, last-writer-wins preview slot (registry.py:327-331): it advances
    ONLY `configuration_revision` (registry.py:339) while `preview` stays non-null
    and `revision`/`generation` are untouched — so the optimistic-token check the
    other overtaken/conflict paths rely on never fires. Our own preview causes
    exactly ONE `configuration_revision` bump; a FURTHER advance beyond that, with
    the slot still occupied, is a foreign write that took the panel from us.

    The poll must therefore flip to "overtaken" (§4b: "your preview was
    superseded") and STOP asserting we own the panel (§4c: never imply exclusive
    control) — the countdown timer must disappear.

    This test is RED against the pre-fix hook (which treated any non-null
    `live.preview` as our own preview still active and silently absorbed the
    foreign `configuration_revision` bump, keeping status "previewing" and the
    timer lying) and GREEN after the fix.
    """
    _seed(registry)
    _sync_clock(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")
        lease = _lease(inspector)

        lease.get_by_role("button", name="Show on the Panel", exact=True).click()
        expect(inspector.get_by_role("timer", name="Live calibration countdown")).to_be_visible()

        # A second tab previews the SAME frame: it overtakes the single preview
        # slot, advancing ONLY configuration_revision (not revision/generation)
        # and leaving `preview` non-null (last-writer-wins).
        registry.calibrate(
            FRAME, "preview", expected_revision=2,
            calibration=Calibration(gain=1.7), expected_generation=1)
        # Drive the overtake poll deterministically via a Plane A refresh.
        page.get_by_role("button", name="Refresh", exact=True).click()

        # The overtaken banner shows (§4b copy)...
        expect(lease.get_by_role("alert")).to_contain_text(
            "Someone saved a calibration for this Frame meanwhile; review it, then start again")
        # ...and the timer no longer claims the panel (§4c: no false exclusivity).
        expect(inspector.get_by_role("timer", name="Live calibration countdown")).to_have_count(0)
        # The single preview slot is still occupied — by the FOREIGN preview.
        assert inventory(page, origin).frames[0].preview is not None


# --- Bead G3 (SR-parity, GAP 3): manual Revert -----------------------------
#
# The manual Stop control (CalibrationFacet.jsx -> calibrate("revert")) already
# exists; this closes the missing /console TEST for it (legacy
# test_operator_browser.py:118-120). Revert clears the panel preview slot
# server-side AND discards the local draft back to committed — the operator is
# returned to truth, unlike lease EXPIRY (which retains trying for Re-preview).


def test_manual_revert_clears_preview_and_returns_draft_to_committed(page, registry):
    """Previewing, then clicking Revert, returns the panel to committed.

    The operator previews a changed draft gain onto the panel (preview slot set),
    then clicks Revert: the server preview slot is cleared and the editable draft
    gain resets to the committed value — parity with the legacy page's single-
    field Revert.

    Mutation probe (Revert no-ops): make the Revert button not clear the preview
    (skip calibrate("revert")/clearDraft) -> the preview slot stays set and the
    draft never returns to committed -> this test goes RED. Restore -> GREEN.
    """
    _seed(registry)  # committed gain 1.5
    _sync_clock(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, FRAME, "calibration")
        lease = _lease(inspector)

        # Change the draft gain away from committed and preview it onto the panel.
        gain = inspector.get_by_role("spinbutton", name="SDR gain (draft)")
        gain.fill("0.75")
        expect(gain).to_have_value("0.75")
        lease.get_by_role("button", name="Show on the Panel", exact=True).click()
        expect(inspector.get_by_role("timer", name="Live calibration countdown")).to_be_visible()
        assert inventory(page, origin).frames[0].preview is not None

        # Manual Revert: the draft returns to committed (1.5). Waiting on the
        # draft value is the synchronization point — it flips only AFTER the
        # revert POST resolves, so the server preview is cleared by then.
        lease.get_by_role("button", name="Stop live calibration", exact=True).click()
        expect(gain).to_have_value("1.5")

        # Committed calibration is unchanged, and the panel preview is cleared.
        expect(
            inspector.get_by_role("group", name="Committed calibration")
        ).to_contain_text(str(GAIN))
        observed = inventory(page, origin).frames[0]
        assert observed.preview is None
        assert observed.calibration.gain == GAIN


# --- Live calibration: one noun, two honest verbs (console DDD §20) -----------------------


def test_live_calibration_without_acknowledgment_offers_only_save_without_acknowledgment(
        page, registry):
    """The legacy path (no Display Host on this Player) can never have an acknowledgment, so
    its commit is "Save without acknowledgment" and nothing on it is called Save calibration."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        lease = _lease(open_frame(page, FRAME, "calibration"))
        expect(lease.get_by_role("heading", name="Live calibration", exact=True)).to_be_visible()
        expect(lease.get_by_role("button")).to_have_text(
            ["Show on the Panel", "Save without acknowledgment", "Stop live calibration"])
        expect(lease).not_to_contain_text("Save calibration")
        expect(lease).to_contain_text("No layer acknowledges what is presented on this path")


def _native_trial(page, presented):
    """Stub Central's answers for live calibration with Display Host acknowledgment: the
    capability read says `native_trial`, and every trial request answers one active row for
    the committed draft, acknowledged by Display Host only once `presented[0]` is set. Returns
    the list of trial operations the console sent."""
    sent = []
    row = {"trial_id": "trial-1", "state": "active", "sequence": 1, "calibration_revision": 2,
           "calibration": {"corners": [[0, 0], [1, 0], [1, 1], [0, 1]], "crop": [0, 0, 1, 1],
                           "rotation": 0, "gain": GAIN},
           "candidate_sha256": "a" * 64, "presented_sequence": None, "presented_sha256": None}

    def trial(route):
        body = route.request.post_data_json or {}
        sent.append(body.get("operation", "begin"))
        answer = dict(row)
        if presented[0]:
            answer.update(presented_sequence=1, presented_sha256="a" * 64)
        if body.get("operation") == "end":
            answer["state"] = "ended"
        route.fulfill(json=answer)

    page.route("**/v1/operator/frames/*/calibration-capability",
               lambda route: route.fulfill(json={"mode": "native_trial"}))
    page.route("**/v1/operator/frames/*/calibration-trials**", trial)
    return sent


def test_save_calibration_waits_for_display_hosts_acknowledgment_of_the_latest_edit(
        page, registry):
    """On the native path, Save calibration stays disabled until Display Host acknowledges the
    latest edit (R7), and the acknowledgment is worded as a compositor receipt, not the Panel.

    Mutation: drop `!presented` from the Save button's disabled state -> Save is enabled
    before the acknowledgment -> the first to_be_disabled assertion goes RED."""
    _seed(registry)
    presented = [False]
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        sent = _native_trial(page, presented)
        lease = _lease(open_frame(page, FRAME, "calibration"))
        lease.get_by_role("button", name="Start live calibration", exact=True).click()

        save = lease.get_by_role("button", name="Save calibration", exact=True)
        expect(lease.get_by_role("status")).to_have_text(
            "Waiting for Display Host to acknowledge edit 1.")
        expect(save).to_be_disabled()
        expect(lease.get_by_role("button", name="Stop live calibration", exact=True)).to_be_enabled()

        presented[0] = True  # the next status poll carries Display Host's acknowledgment
        expect(lease.get_by_role("status")).to_have_text(
            "Edit 1 presented to the compositor by Display Host · not proof of what the Panel shows")
        expect(save).to_be_enabled()
        expect(lease).not_to_contain_text("Save without acknowledgment")
        assert sent[0] == "begin" and "status" in sent
