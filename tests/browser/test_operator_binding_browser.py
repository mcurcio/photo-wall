"""Behavioral browser checks for onboarding & binding at /console (Bead 9).

Reuses the existing operator browser harness (real Chromium against the
production create_app on an ephemeral loopback listener, the disposable-schema
`registry` fixture, and the autouse `page_errors` guard). Equipment is seeded
directly through the registry (as the `installation`/`enroll` fixtures do) so the
console renders real inventory: pending Players, real Frame generations, and a
real returning-Pi reassociation.

This is the redesign's OWN /console binding coverage. It does NOT touch or
inherit the legacy tests/browser/test_operator_browser.py (which asserts the old
flat page at /, unchanged until the Bead 17 cutover).

Every assertion is BEHAVIORAL — role/text/visible state — and locates Players and
Frames by identity/label, never by SVG coordinates or DOM structure (design §1c).
"""

import json
import os
import re

import pytest
from console_tasks import connect, go, open_frame
from operator_harness import RequestGate, drive_poll, operator_server, pause_page_clock, sign_in
from playwright.sync_api import expect
from test_registry import ADMIN, enroll

from central.content_catalog.catalog import device_id_for_serial
from central.registry import FrameCreate
from central.runtime import Contribution, Scene
from central.runtime_store import RuntimeStore
from contracts.models import FrameProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

LANDSCAPE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)


def _placed_frame(registry, frame_id):
    # A frame with a distinct (non-origin) position so it renders on the plan and
    # is selectable by identity, rather than being routed to the Unplaced tray.
    registry.create_frame(FrameCreate(
        id=frame_id, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=400, height_mm=300, profile=LANDSCAPE))


def _option(scope, player_id, output_id="HDMI-A-1"):
    """A free Output in the chooser, by its one wording: handle · output id · Free (the
    handle is the Player id's last six characters when there is no netboot record)."""
    return scope.get_by_role("radio", name=f"{player_id[-6:]} · {output_id} · Free", exact=True)


def _poll(page):
    """Run the paused page clock one poll interval and wait for that poll to finish."""
    drive_poll(page)


def _dialog(page):
    return page.get_by_role("dialog")


def _type_handle_and_retire(page, player_id):
    dialog = _dialog(page)
    dialog.get_by_label(f"Type {player_id[-6:]} to confirm", exact=True).fill(player_id[-6:])
    dialog.get_by_role("button", name="Confirm retire", exact=True).click()


def _bound(registry, frame_id="bound-1", count=1):
    """A placed frame bound to a fresh Player's HDMI-A-1 (generation 1); the Player id."""
    identity, _, _ = enroll(registry, count=count)
    _placed_frame(registry, frame_id)
    registry.bind(frame_id, identity["player_id"], "HDMI-A-1", expected_generation=0)
    return identity["player_id"]


def _open_unbind(page, frame_id="bound-1"):
    inspector = open_frame(page, frame_id, "binding")
    inspector.get_by_role("button", name="Unbind", exact=True).click()
    dialog = _dialog(page)
    expect(dialog).to_be_visible()
    return inspector, dialog


def _binding_requests(page):
    """Every unbind request the page sends, in order."""
    sent = []
    page.on("request", lambda request: sent.append(request.url)
            if request.method == "DELETE" and request.url.endswith("/binding") else None)
    return sent


def _disconnect_output(registry, player_id, output_id):
    """The Player's last start reported no display on this Output (connected=false)."""
    with registry.db.transaction() as conn:
        conn.execute("UPDATE outputs SET observation=jsonb_set(observation,'{connected}','false') "
                     "WHERE player_id=%s AND output_id=%s", (player_id, output_id))


def test_pending_player_appears_in_the_pending_rail(page, registry):
    # A freshly enrolled Player is unbound and not retired -> Pending rail.
    identity, _, _ = enroll(registry, count=2)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")

        pending = page.get_by_role("group", name="Pending players", exact=True)
        expect(
            pending.get_by_role("button", name=identity["player_id"], exact=True)
        ).to_be_visible()


def test_pending_output_identify_requests_exact_output_and_explains_no_display(page, registry):
    identity, _, _ = enroll(registry, count=2)
    player_id = identity["player_id"]
    _disconnect_output(registry, player_id, "HDMI-A-2")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")
        pending = page.get_by_role("group", name="Pending players", exact=True)
        outputs = pending.get_by_role("list", name=f"Outputs of {player_id}", exact=True)
        identify = outputs.get_by_role(
            "button", name="Identify display HDMI-A-1", exact=True)
        no_display = outputs.get_by_role(
            "button", name="Identify display HDMI-A-2", exact=True)
        expect(identify).to_be_enabled()
        expect(no_display).to_be_disabled()
        expect(no_display).to_have_accessible_description(
            "Connect a display and restart the Player, then Refresh Equipment.")

        gate = RequestGate(page, "**/v1/operator/players/*/outputs/*/identify")
        gate.holding = True
        identify.click()
        gate.wait_held()
        assert gate.seen == 1
        assert gate.held[0].request.url.endswith(
            f"/v1/operator/players/{player_id}/outputs/HDMI-A-1/identify")
        expect(identify).to_be_disabled()  # a second click cannot duplicate the request
        gate.release(status=202, content_type="application/json", body=json.dumps({
            "request_id": "synthetic-request", "output_id": "HDMI-A-1",
            "expires_at": registry.clock.utc() + 15,
        }))
        expect(outputs.get_by_role("status")).to_have_text(
            "Identify requested for HDMI-A-1. Check the display; "
            "this request expires in 15 seconds.")
        expect(identify).to_be_enabled()


@pytest.mark.parametrize("status, expected", [
    (404, "This Output changed or the Player is no longer eligible. "
          "Refresh Equipment before trying again."),
    (409, "This Output changed or the Player is no longer eligible. "
          "Refresh Equipment before trying again."),
    (503, "The request outcome is unknown. Check the display before trying again."),
])
def test_pending_output_identify_failure_is_honest(page, registry, status, expected):
    enroll(registry, count=1)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")
        pending = page.get_by_role("group", name="Pending players", exact=True)
        identify = pending.get_by_role(
            "button", name="Identify display HDMI-A-1", exact=True)
        gate = RequestGate(page, "**/v1/operator/players/*/outputs/*/identify")
        gate.holding = True
        identify.click()
        gate.wait_held()
        gate.release(status=status, content_type="application/json", body=json.dumps({
            "error": {404: "unknown_output", 409: "output_disconnected"}.get(
                status, "server_unavailable"),
        }))
        expect(pending.get_by_role("alert")).to_have_text(expected)
        expect(identify).to_be_enabled()


def test_binding_pending_output_shows_review_and_commission_cta(page, registry):
    identity, _, _ = enroll(registry, count=1)  # a pending Player with HDMI-A-1
    _placed_frame(registry, "wall-1")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")

        pending = page.get_by_role("group", name="Pending players", exact=True)
        expect(
            pending.get_by_role("button", name=identity["player_id"], exact=True)
        ).to_be_visible()

        # Select the Frame on the plan, open the Binding facet, choose the pending
        # Output and bind it.
        inspector = open_frame(page, "wall-1", "binding")
        _option(inspector, identity["player_id"]).check()
        inspector.get_by_role("button", name="Bind to wall-1", exact=True).click()

        # The facet shows the amber "Review required" state and the CTA...
        expect(inspector.get_by_text("Review required", exact=False)).to_be_visible()
        cta = inspector.get_by_role("button", name="Commission the display", exact=True)
        expect(cta).to_be_visible()

        # ...and the CTA switches the Inspector to the Commissioning facet.
        cta.click()
        expect(
            inspector.get_by_role("tabpanel", name="Commissioning facet")
        ).to_be_visible()

        # On Equipment, the now-bound Player has left the Pending group for the Bound one
        # (Plane A refreshed via useMutate).
        go(page, "equipment")
        expect(
            page.get_by_role("group", name="Bound players", exact=True).get_by_role(
                "button", name=identity["player_id"], exact=True)
        ).to_be_visible()
        expect(
            pending.get_by_role("button", name=identity["player_id"], exact=True)
        ).to_have_count(0)


def test_retiring_a_pending_player_moves_it_to_retired_and_drops_its_output(page, registry):
    # Bead G1 (SR-retire): the console must re-host the legacy "Retire a Player"
    # control (legacy test_operator_browser.py:137-141) so the cutover keeps
    # content parity. Retiring a pending Player moves it to the Retired rail AND
    # removes its Output from the Binding facet's bind choices.
    identity, _, _ = enroll(registry, count=1)  # a pending Player with HDMI-A-1
    _placed_frame(registry, "wall-r")
    player_id = identity["player_id"]
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")

        pending = page.get_by_role("group", name="Pending players", exact=True)
        retired = page.get_by_role("group", name="Retired players", exact=True)
        expect(
            pending.get_by_role("button", name=player_id, exact=True)
        ).to_be_visible()

        # Precondition: the Player's Output IS a bind candidate — the Binding
        # facet's chooser offers it.
        inspector = open_frame(page, "wall-r", "binding")
        expect(_option(inspector, player_id)).to_be_visible()

        # Retire the pending Player from the rail (a deliberate, labelled action),
        # typing its handle to confirm (slice 2 §7).
        go(page, "equipment")
        pending.get_by_role(
            "button", name=f"Retire player {player_id}", exact=True
        ).click()
        _type_handle_and_retire(page, player_id)

        # It moves to the Retired rail (by identity) and leaves the Pending rail.
        expect(retired.get_by_role("button", name=player_id, exact=True)).to_be_visible()
        expect(
            pending.get_by_role("button", name=player_id, exact=True)
        ).to_have_count(0)

        # The retired Player's Output is not an option: no radio names it, and the
        # chooser says there is nothing free to bind (the Wall link returns to the frame).
        go(page, "wall")
        expect(inspector).to_be_visible()
        expect(inspector.get_by_role("radio")).to_have_count(0)
        expect(inspector.get_by_text("No free outputs with a detected display",
                                     exact=False)).to_be_visible()


def test_connect_with_a_rejected_token_shows_not_accepted_and_returns_to_login(page, registry):
    # Bead G3 (SR-parity, GAP 4), on the pass A sign-in screen: a rejected
    # operator token must surface an explicit "not accepted" message and stay on
    # the sign-in screen — NOT silently blank (legacy
    # test_operator_browser.py:99-104,164-177).
    identity, _, _ = enroll(registry, count=1)
    _placed_frame(registry, "auth-1")
    with operator_server(registry.db, registry.clock) as origin:
        # Sign in with a WRONG token: Central answers the sign-in with 401 and
        # issues no cookie.
        sign_in(page, origin, token="not-the-admin-token")

        # The 401 surfaces an explicit auth-rejected message...
        expect(page.get_by_role("alert")).to_contain_text("not accepted")
        # ...the field was cleared on submit (the token is kept nowhere), and the
        # console stays on the sign-in screen: NO connected content rendered.
        expect(page.get_by_label("Operator token")).to_have_value("")
        expect(page.get_by_role("button", name="Sign in", exact=True)).to_be_visible()
        expect(
            page.get_by_role("group", name="Pending players", exact=True)
        ).to_have_count(0)

        # Recovery: the CORRECT token signs in and the real inventory renders,
        # and the rejection message is gone.
        page.get_by_label("Operator token").fill(ADMIN)
        page.get_by_role("button", name="Sign in", exact=True).click()
        go(page, "wall")
        expect(page.get_by_role("button", name="Frame auth-1", exact=True)).to_be_visible()
        expect(page.get_by_role("alert")).to_have_count(0)
        expect(page.get_by_role("button", name="Sign in", exact=True)).to_have_count(0)


def test_stale_generation_bind_surfaces_the_reload_review_message(page, registry):
    identity, _, _ = enroll(registry, count=1)
    _placed_frame(registry, "stale-1")
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        sign_in(page, origin)

        # The operator chooses the Output while the console holds the Frame at
        # generation 0: the choice captures that generation.
        inspector = open_frame(page, "stale-1", "binding")
        _option(inspector, identity["player_id"]).check()

        # Server-side, advance the Frame's generation (bind then unbind each bump it),
        # and let a poll deliver the new generation to the console. The Output is free
        # again, so the choice stands.
        registry.bind("stale-1", identity["player_id"], "HDMI-A-1", expected_generation=0)
        registry.unbind("stale-1", expected_generation=1)
        _poll(page)
        expect(_option(inspector, identity["player_id"])).to_be_checked()

        # The bind carries the CAPTURED generation (0), not the live one (2) -> 409
        # binding_generation_conflict -> the distinctive reload/review wording.
        inspector.get_by_role("button", name="Bind to stale-1", exact=True).click()
        expect(
            inspector.get_by_text("This Frame changed", exact=False)
        ).to_be_visible()
        assert registry.inventory().frames[0].player_id is None

        # The attempt spent the stale choice: nothing is checked and Bind is disabled.
        # Choosing again captures the live generation (2), and that bind succeeds.
        option = _option(inspector, identity["player_id"])
        expect(option).not_to_be_checked()
        bind_button = inspector.get_by_role("button", name="Bind to stale-1", exact=True)
        expect(bind_button).to_be_disabled()
        option.check()
        bind_button.click()
        expect(inspector.get_by_text("Review required", exact=False)).to_be_visible()
        assert registry.inventory().frames[0].player_id == identity["player_id"]


def test_the_second_output_of_a_bound_player_is_bindable_and_stored(page, registry):
    # The tracer (slice 2 §10): a two-output Player has HDMI-A-1 bound; the operator
    # binds an unbound Frame to HDMI-A-2, chosen explicitly.
    identity, _, _ = enroll(registry, count=2)
    player_id = identity["player_id"]
    _placed_frame(registry, "left")
    registry.create_frame(FrameCreate(
        id="right", surface_id="wall", x_mm=600, y_mm=100,
        width_mm=400, height_mm=300, profile=LANDSCAPE))
    registry.bind("left", player_id, "HDMI-A-1", expected_generation=0)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, "right", "binding")

        # The bound HDMI-A-1 is not offered; HDMI-A-2 is, and nothing is selected.
        expect(inspector.get_by_role("radio")).to_have_count(1)
        second = _option(inspector, player_id, "HDMI-A-2")
        expect(second).not_to_be_checked()
        second.check()
        inspector.get_by_role("button", name="Bind to right", exact=True).click()
        expect(inspector.get_by_text("Review required", exact=False)).to_be_visible()

        frames = {frame.id: frame for frame in registry.inventory().frames}
        assert (frames["right"].player_id, frames["right"].output_id) == (player_id, "HDMI-A-2")
        assert (frames["left"].player_id, frames["left"].output_id) == (player_id, "HDMI-A-1")


def test_bind_is_disabled_until_the_operator_chooses(page, registry):
    identity, _, _ = enroll(registry, count=1)
    _placed_frame(registry, "choose-1")
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, "choose-1", "binding")
        # One option, and still nothing is chosen for the operator.
        option = _option(inspector, identity["player_id"])
        expect(option).not_to_be_checked()
        bind_button = inspector.get_by_role("button", name="Bind to choose-1", exact=True)
        expect(bind_button).to_be_disabled()
        option.check()
        expect(bind_button).to_be_enabled()


def test_a_chosen_output_that_vanishes_on_a_poll_is_cleared_and_announced(page, registry):
    identity, _, _ = enroll(registry, count=1)
    player_id = identity["player_id"]
    _placed_frame(registry, "mine")
    registry.create_frame(FrameCreate(
        id="theirs", surface_id="wall", x_mm=600, y_mm=100,
        width_mm=400, height_mm=300, profile=LANDSCAPE))
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        sign_in(page, origin)
        inspector = open_frame(page, "mine", "binding")
        _option(inspector, player_id).check()

        # Another operator binds that Output elsewhere; the next poll removes it.
        registry.bind("theirs", player_id, "HDMI-A-1", expected_generation=0)
        _poll(page)

        expect(inspector.get_by_role("status")).to_contain_text(
            f"{player_id[-6:]} · HDMI-A-1 · Free is no longer available")
        expect(inspector.get_by_role("radio")).to_have_count(0)
        expect(inspector.get_by_role("button", name="Bind to mine", exact=True)).to_be_disabled()


def test_no_display_and_retired_outputs_are_never_offered(page, registry):
    dark, _, _ = enroll(registry, count=2)
    gone, _, _ = enroll(registry, count=1)
    _disconnect_output(registry, dark["player_id"], "HDMI-A-2")
    registry.retire(gone["player_id"])
    _placed_frame(registry, "only")
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, "only", "binding")
        # Exactly the one free Output: the no-display HDMI-A-2 and the retired Player's
        # HDMI-A-1 are excluded.
        expect(inspector.get_by_role("radio")).to_have_count(1)
        expect(_option(inspector, dark["player_id"], "HDMI-A-1")).to_be_visible()
        expect(inspector.get_by_role("radio", name=gone["player_id"][-6:], exact=False)
               ).to_have_count(0)


def test_recovery_banner_is_suppressed_on_the_true_first_run(page, registry):
    # A returning-Pi shape (a Player already bound), but observed for the FIRST
    # time by this console -> no prior snapshot to diff -> no banner.
    identity, _, _ = enroll(registry, count=2)
    _placed_frame(registry, "rec-1")
    registry.bind("rec-1", identity["player_id"], "HDMI-A-1", expected_generation=0)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")

        # Force a wait for the first snapshot to load.
        expect(
            page.get_by_role("button", name="Frame rec-1", exact=True)
        ).to_be_visible()

        # On the true first run the recovery banner is suppressed.
        expect(
            page.get_by_text("Recovered — already bound", exact=False)
        ).to_have_count(0)


def test_recovery_banner_appears_for_a_returning_bound_player(page, registry):
    identity, key, request = enroll(registry, count=2)
    _placed_frame(registry, "rec-2")
    registry.bind("rec-2", identity["player_id"], "HDMI-A-1", expected_generation=0)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")

        # First snapshot: known bound Pi at authority_epoch 1, no banner yet.
        expect(
            page.get_by_role("button", name="Frame rec-2", exact=True)
        ).to_be_visible()
        expect(
            page.get_by_text("Recovered — already bound", exact=False)
        ).to_have_count(0)

        # The Pi reboots: re-enroll by the same serial reassociates the SAME
        # player_id, preserves its Frame binding (is_bound stays true), and bumps
        # authority_epoch.
        enroll(registry, key=key, device_id=request.device_id, count=2)

        # Refresh the console (one Plane A refresh); the diff
        # against the retained prior snapshot now surfaces the recovery banner.
        page.get_by_role("button", name="Refresh", exact=True).click()
        banner = page.get_by_text("Recovered — already bound", exact=False)
        expect(banner).to_be_visible()
        expect(banner).to_contain_text(identity["player_id"])


# --- One confirmation pattern (slice 2 §7).


def test_retire_is_enabled_only_by_typing_the_handle(page, registry):
    identity, _, _ = enroll(registry, count=1)
    player_id = identity["player_id"]
    handle = player_id[-6:]
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")
        pending = page.get_by_role("group", name="Pending players", exact=True)
        opener = pending.get_by_role("button", name=f"Retire player {player_id}", exact=True)
        opener.click()
        dialog = _dialog(page)
        expect(dialog).to_contain_text("No undo, even after re-imaging")
        confirm = dialog.get_by_role("button", name="Confirm retire", exact=True)
        typed = dialog.get_by_label(f"Type {handle} to confirm", exact=True)
        expect(confirm).to_be_disabled()
        typed.fill("x" + handle[1:])
        expect(confirm).to_be_disabled()
        typed.fill(handle.upper())  # case-insensitive
        expect(confirm).to_be_enabled()

        # Esc cancels while idle: nothing is sent and focus returns to the opener.
        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0)
        expect(opener).to_be_focused()
        assert registry.inventory().players[0].retired_at is None

        opener.click()
        _type_handle_and_retire(page, player_id)
        retired = page.get_by_role("group", name="Retired players", exact=True)
        expect(retired.get_by_role("button", name=player_id, exact=True)).to_be_visible()
        # Focus successor: the Retired heading; a status line says what happened.
        expect(retired.get_by_role("heading", name=re.compile(r"^Retired players"))).to_be_focused()
        expect(page.get_by_text(f"Player {player_id} retired.", exact=True)).to_be_visible()


def test_esc_is_blocked_while_an_unbind_is_in_flight_even_when_repeated(page, registry):
    _bound(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector, dialog = _open_unbind(page)
        gate = RequestGate(page, "**/v1/operator/frames/*/binding")
        gate.holding = True
        dialog.get_by_role("button", name="Confirm unbind", exact=True).click()
        gate.wait_held()

        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role("button", name="Cancel", exact=True)).to_be_disabled()

        gate.holding = False
        gate.release()
        expect(dialog).to_have_count(0)
        # Focus successor: the Frame's output chooser, now that it is unbound.
        expect(inspector.get_by_role("radiogroup", name="Choose an output")).to_be_focused()
        expect(inspector.get_by_role("status")).to_have_text("Frame bound-1 unbound.")
        assert registry.inventory().frames[0].player_id is None


def test_an_unbind_with_a_stale_generation_is_changed_terminal_and_never_resent(page, registry):
    player_id = _bound(registry)
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        sign_in(page, origin)
        sent = _binding_requests(page)
        _inspector, dialog = _open_unbind(page)  # captures generation 1

        # The Frame changes under the open dialog (unbound, then bound again: generation
        # 3), and a poll delivers that to the console.
        registry.unbind("bound-1", expected_generation=1)
        registry.bind("bound-1", player_id, "HDMI-A-1", expected_generation=2)
        _poll(page)

        dialog.get_by_role("button", name="Confirm unbind", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text(
            "Changed since you opened this. Reopen to review.")
        # Terminal: no Confirm remains, and exactly one request was ever sent.
        expect(dialog.get_by_role("button", name="Confirm unbind", exact=True)).to_have_count(0)
        page.wait_for_timeout(300)
        assert len(sent) == 1
        assert registry.inventory().frames[0].player_id == player_id


def test_an_unbind_that_already_happened_reads_already_done(page, registry):
    _bound(registry)
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        sign_in(page, origin)
        _inspector, dialog = _open_unbind(page)
        registry.unbind("bound-1", expected_generation=1)
        dialog.get_by_role("button", name="Confirm unbind", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text("Already done.")
        expect(dialog.get_by_role("alert")).to_have_count(0)


def test_an_unbind_that_gets_no_answer_reads_outcome_unknown(page, registry):
    _bound(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        _inspector, dialog = _open_unbind(page)
        page.route("**/v1/operator/frames/*/binding", lambda route: route.abort()
                   if route.request.method == "DELETE" else route.continue_())
        dialog.get_by_role("button", name="Confirm unbind", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text(
            "Central did not answer. Check this after the next refresh.")
        expect(dialog.get_by_role("button", name="Confirm unbind", exact=True)).to_have_count(0)


def test_an_unbind_answered_by_a_gateway_error_reads_outcome_unknown(page, registry):
    # A 5xx from Central or a gateway says nothing about whether the write applied.
    _bound(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        _inspector, dialog = _open_unbind(page)
        page.route("**/v1/operator/frames/*/binding", lambda route: route.fulfill(
            status=502, content_type="text/html", body="<h1>Bad Gateway</h1>")
            if route.request.method == "DELETE" else route.continue_())
        dialog.get_by_role("button", name="Confirm unbind", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text(
            "Central did not answer. Check this after the next refresh.")
        expect(dialog.get_by_role("alert")).to_have_count(0)
        expect(dialog.get_by_role("button", name="Confirm unbind", exact=True)).to_have_count(0)


def test_a_refresh_failure_after_an_unbind_is_not_a_refusal(page, registry):
    _bound(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector, dialog = _open_unbind(page)
        page.route("**/v1/operator/snapshot", lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"error": "boom"}'))
        dialog.get_by_role("button", name="Confirm unbind", exact=True).click()

        # The write is done: the dialog closes with the status line, and the failed
        # refresh shows only as "last refresh failed" -- never as a refusal.
        expect(dialog).to_have_count(0)
        expect(inspector.get_by_role("status")).to_have_text("Frame bound-1 unbound.")
        expect(page.get_by_text("last refresh failed", exact=False)).to_be_visible()
        expect(page.get_by_role("alert")).to_have_count(0)
        # The stale snapshot still reads bound, so no chooser: focus falls back to the
        # facet heading.
        expect(inspector.get_by_role("heading", name="Binding", exact=True)).to_be_focused()
        assert registry.inventory().frames[0].player_id is None


# --- Boot facts (slice 2 §5): one optional read of the netboot records.

SERIAL = "10000000c0ffee42"
NETBOOT = "**/v1/operator/netboot"


def _netbooted_player(registry, serial=SERIAL):
    """A Player whose Pi netbooted: a `devices` row (seeded by SQL, as the netboot seam
    writes it) shares the Player's device_id, both derived from the serial."""
    device_id = device_id_for_serial(serial)
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen) "
                     "VALUES(%s,%s,%s,%s)",
                     (device_id, serial, registry.clock.utc(), registry.clock.utc()))
    identity, _, _ = enroll(registry, count=1, device_id=device_id)
    return identity["player_id"]


def _serial_option(scope, serial=SERIAL, output_id="HDMI-A-1"):
    return scope.get_by_role("radio", name=f"{serial[-6:]} · {output_id} · Free", exact=True)


def test_the_devices_serial_shows_in_the_chooser_and_the_roster(page, registry):
    _netbooted_player(registry)
    _placed_frame(registry, "boot-1")
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, "boot-1", "binding")
        # The handle is the serial's suffix (joined on device_id, not the Player id).
        expect(_serial_option(inspector)).to_be_visible()
        go(page, "equipment")
        pending = page.get_by_role("group", name="Pending players", exact=True)
        expect(pending).to_contain_text(f"Reported serial {SERIAL} · Netboot seen, no image served yet")


def test_collapsed_pending_cards_show_distinct_reported_serial_handles(page, registry):
    first_serial = SERIAL
    second_serial = "10000000c0ffee93"
    first_player = _netbooted_player(registry, first_serial)
    second_player = _netbooted_player(registry, second_serial)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")
        pending = _group(page, "Pending players")
        first = pending.get_by_role("button", name=first_player, exact=True)
        second = pending.get_by_role("button", name=second_player, exact=True)
        expect(first).to_be_visible()
        expect(second).to_be_visible()

        # The disclosure name remains the Player ID; the description carries the
        # serial suffix so the two newly enrolled devices are distinguishable.
        expect(first).to_have_accessible_description(re.compile(r"Serial …ffee42"))
        expect(second).to_have_accessible_description(re.compile(r"Serial …ffee93"))
        first.click()
        second.click()
        expect(first).to_have_attribute("aria-expanded", "false")
        expect(second).to_have_attribute("aria-expanded", "false")
        expect(first).to_have_accessible_name(first_player)
        expect(second).to_have_accessible_name(second_player)
        expect(first).to_have_accessible_description(re.compile(r"Serial …ffee42"))
        expect(second).to_have_accessible_description(re.compile(r"Serial …ffee93"))

        # Expanding still exposes the full serial and the existing commissioning action.
        first.click()
        expect(pending.get_by_text(f"Reported serial {first_serial}", exact=False)).to_be_visible()
        expect(pending.get_by_role("button", name=f"Retire player {first_player}", exact=True)
               ).to_be_visible()


def test_missing_boot_facts_do_not_show_a_fallback_serial_handle(page, registry):
    identity, _, _ = enroll(registry, count=1)
    player_id = identity["player_id"]
    page.route(NETBOOT, lambda route: route.fulfill(
        status=503, content_type="application/json", body='{"error": "content_unavailable"}'))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")
        pending = _group(page, "Pending players")
        player = pending.get_by_role("button", name=player_id, exact=True)
        expect(player).to_be_visible()
        expect(pending).to_contain_text("Boot records unavailable")
        expect(pending.get_by_text(re.compile(r"Serial …"))).to_have_count(0)
        # The existing action remains available even when serial enrichment fails.
        expect(pending.get_by_role("button", name=f"Retire player {player_id}", exact=True)
               ).to_be_visible()


def test_a_player_that_never_netbooted_reads_no_netboot_record(page, registry):
    identity, _, _ = enroll(registry, count=1)
    _placed_frame(registry, "boot-2")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")
        pending = page.get_by_role("group", name="Pending players", exact=True)
        expect(pending).to_contain_text("No netboot record")
        # Without a serial the handle is the Player id's hash suffix.
        inspector = open_frame(page, "boot-2", "binding")
        expect(_option(inspector, identity["player_id"])).to_be_visible()


OLD, NEW = "v1.4.2", "v1.5.0"


@pytest.mark.parametrize(("outcome", "served", "good", "failed", "label"), [
    # healthy: netboot_base writes known_good = last_served with it.
    ("healthy", OLD, OLD, None, f"Last netboot healthy on {OLD}"),
    ("healthy", OLD, OLD, NEW, f"Rolled back from {NEW} · last netboot healthy on {OLD}"),
    # pending: record_served moved last_served and left known_good behind.
    ("pending", NEW, OLD, None, f"Netboot served {NEW}, base health not reported · last healthy on {OLD}"),
    ("pending", NEW, None, None, f"Netboot served {NEW}, base health not reported"),
    ("pending", OLD, OLD, NEW, f"Rolled back from {NEW} · netboot served {OLD}, base health not reported"),
    # fenced with no known-good: the failed tag is served again (boot_policy.py).
    ("pending", NEW, None, NEW,
     f"Retrying {NEW} after a failed netboot · no healthy version to roll back to"),
    ("failed", NEW, OLD, NEW, f"Last netboot of {NEW} failed · last healthy on {OLD}"),
    ("failed", NEW, None, NEW, f"Last netboot of {NEW} failed · no healthy version to roll back to"),
])
def test_the_boot_outcome_names_each_tag_by_what_central_recorded(
        page, registry, outcome, served, good, failed, label):
    player_id = _netbooted_player(registry)
    row = {"device_id": device_id_for_serial(SERIAL), "serial": SERIAL, "attached_tag": None,
           "known_good_tag": good, "last_served_tag": served, "boot_outcome": outcome,
           "failed_tag": failed}
    page.route(NETBOOT, lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"frontier": NEW, "devices": [row]})))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")
        pending = _group(page, "Pending players")
        expect(pending.get_by_role("button", name=player_id, exact=True)).to_be_visible()
        expect(pending.get_by_text(f"Reported serial {SERIAL} · {label}", exact=True)
               ).to_be_visible()
        if outcome == "pending":
            expect(pending).to_contain_text(
                "Base health is separate from Player connection")


def test_a_failed_boot_facts_read_keeps_the_serials(page, registry):
    _netbooted_player(registry)
    _placed_frame(registry, "boot-3")
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        sign_in(page, origin)
        inspector = open_frame(page, "boot-3", "binding")
        expect(_serial_option(inspector)).to_be_visible()

        # 30 s later the next snapshot re-reads the boot facts, and Central answers 503.
        page.route(NETBOOT, lambda route: route.fulfill(
            status=503, content_type="application/json", body='{"error": "content_unavailable"}'))
        with page.expect_response(NETBOOT):
            page.clock.run_for(35000)
        expect(inspector.get_by_text("Boot records unavailable", exact=False)).to_be_visible()
        # The last known serial is kept.
        expect(_serial_option(inspector)).to_be_visible()


def test_a_401_from_the_boot_facts_read_does_not_log_the_operator_out(page, registry):
    identity, _, _ = enroll(registry, count=1)
    _placed_frame(registry, "boot-4")
    page.route(NETBOOT, lambda route: route.fulfill(
        status=401, content_type="application/json", body='{"error": "unauthorized"}'))
    with operator_server(registry.db, registry.clock) as origin:
        with page.expect_response(NETBOOT):
            sign_in(page, origin)
        go(page, "equipment")
        pending = page.get_by_role("group", name="Pending players", exact=True)
        expect(pending).to_contain_text("Boot records unavailable")
        # The session is untouched: a refresh still authenticates and applies.
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(page.get_by_text(re.compile(r"updated [01] s ago"))).to_be_visible()
        expect(page.get_by_text("not accepted", exact=False)).to_have_count(0)
        expect(pending.get_by_role("button", name=identity["player_id"], exact=True)
               ).to_be_visible()


# --- The Equipment roster (slice 2 §5).


def _group(page, title):
    return page.get_by_role("group", name=title, exact=True)


def _card_outputs(page, player_id):
    return page.get_by_role("list", name=f"Outputs of {player_id}", exact=True)


def _frame_select(page, player_id, output_id="HDMI-A-1"):
    label = f"Frame for {player_id[-6:]} · {output_id} · Free"
    return page.get_by_role("combobox", name=label, exact=True)


def _two_bound(registry, *, live_run_on=None):
    """A two-output Player with frames `left` (HDMI-A-1) and `right` (HDMI-A-2), each at
    generation 1; optionally a live Run targeting one of them."""
    identity, _, _ = enroll(registry, count=2)
    player_id = identity["player_id"]
    _placed_frame(registry, "left")
    registry.create_frame(FrameCreate(
        id="right", surface_id="wall", x_mm=600, y_mm=100,
        width_mm=400, height_mm=300, profile=LANDSCAPE))
    registry.bind("left", player_id, "HDMI-A-1", expected_generation=0)
    registry.bind("right", player_id, "HDMI-A-2", expected_generation=0)
    if live_run_on is not None:
        store = RuntimeStore(registry.db, registry.clock)
        store.command("set_scene", Scene(
            scene_id="lobby-loop", loop=True, cycle_seconds=30,
            contributions=(Contribution(target="frame:" + live_run_on,
                                        source_refs=("lobby-photos:1",)),)))
        store.command("activate", "lobby-loop", "lobby-activation", registry.clock.utc())
    return player_id


def _open_unbind_all(page, player_id):
    _group(page, "Bound players").get_by_role(
        "button", name=f"Unbind all outputs of {player_id}", exact=True).click()
    dialog = _dialog(page)
    expect(dialog).to_be_visible()
    return dialog


def test_a_card_lists_each_output_with_its_state_and_offers_no_retire_in_service(page, registry):
    identity, _, _ = enroll(registry, count=2)
    player_id = identity["player_id"]
    handle = player_id[-6:]
    _placed_frame(registry, "lobby-left")
    registry.bind("lobby-left", player_id, "HDMI-A-1", expected_generation=0)
    _disconnect_output(registry, player_id, "HDMI-A-2")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")
        bound = _group(page, "Bound players")
        card = bound.get_by_role("button", name=player_id, exact=True)
        expect(card).to_have_accessible_description(
            re.compile(r"^In service · 0 of 2 outputs free · Enrolled"))
        outputs = _card_outputs(page, player_id).get_by_role("listitem")
        expect(outputs).to_have_text([
            f"{handle} · HDMI-A-1 · Shows frame lobby-left",
            f"{handle} · HDMI-A-2 · No display detected at last Player start",
        ])
        expect(bound.get_by_role("button", name=f"Retire player {player_id}", exact=True)
               ).to_have_count(0)
        expect(bound.get_by_role("button", name=f"Unbind all outputs of {player_id}", exact=True)
               ).to_be_visible()


def test_output_first_bind_opens_the_frame(page, registry):
    identity, _, _ = enroll(registry, count=1)
    player_id = identity["player_id"]
    _placed_frame(registry, "lobby-left")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")
        _frame_select(page, player_id).select_option("lobby-left")
        _card_outputs(page, player_id).get_by_role("button", name="Bind HDMI-A-1", exact=True
                                                   ).click()
        inspector = page.get_by_role("region", name="Frame lobby-left inspector", exact=True)
        expect(inspector.get_by_role("heading", name="Frame lobby-left", exact=True)
               ).to_be_focused()
        expect(inspector.get_by_role("tab", name="Binding", exact=True)
               ).to_have_attribute("aria-selected", "true")
        frame = registry.inventory().frames[0]
        assert (frame.player_id, frame.output_id) == (player_id, "HDMI-A-1")


def test_a_roster_bind_carries_the_generation_captured_on_selection(page, registry):
    identity, _, _ = enroll(registry, count=1)
    other, _, _ = enroll(registry, count=1)
    player_id = identity["player_id"]
    _placed_frame(registry, "lobby-left")
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        connect(page, origin, "equipment")
        _frame_select(page, player_id).select_option("lobby-left")  # captures generation 0
        # The Frame changes (bound elsewhere, then unbound: generation 2), and a poll
        # delivers that; it is still unbound, so the choice stands.
        registry.bind("lobby-left", other["player_id"], "HDMI-A-1", expected_generation=0)
        registry.unbind("lobby-left", expected_generation=1)
        _poll(page)
        expect(_frame_select(page, player_id)).to_have_value("lobby-left")
        outputs = _card_outputs(page, player_id)
        outputs.get_by_role("button", name="Bind HDMI-A-1", exact=True).click()
        expect(outputs.get_by_role("alert")).to_contain_text("This Frame changed")
        assert registry.inventory().frames[0].player_id is None


def test_a_roster_pick_whose_frame_is_bound_elsewhere_is_dropped_and_announced(page, registry):
    identity, _, _ = enroll(registry, count=1)
    other, _, _ = enroll(registry, count=1)
    player_id = identity["player_id"]
    _placed_frame(registry, "lobby-left")
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        connect(page, origin, "equipment")
        _frame_select(page, player_id).select_option("lobby-left")
        registry.bind("lobby-left", other["player_id"], "HDMI-A-1", expected_generation=0)
        _poll(page)
        roster = page.get_by_role("region", name="Equipment", exact=True)
        expect(roster.get_by_role("status")).to_have_text(
            "Frame lobby-left is no longer available. Choose another frame.")


def test_unbind_all_lists_each_frame_and_its_live_runs(page, registry):
    player_id = _two_bound(registry, live_run_on="left")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")
        dialog = _open_unbind_all(page, player_id)
        frames = dialog.get_by_role("list", name="Frames to unbind").get_by_role("listitem")
        expect(frames).to_have_text([
            "Frame left (HDMI-A-1) — live Runs: lobby-loop",
            "Frame right (HDMI-A-2)",
        ])


def test_unbind_all_reports_each_frame_and_never_resends_a_conflict(page, registry):
    player_id = _two_bound(registry)
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        connect(page, origin, "equipment")
        sent = _binding_requests(page)
        dialog = _open_unbind_all(page, player_id)  # captures left@1, right@1
        # `right` changes under the open dialog (generation 3), and a poll delivers it.
        registry.unbind("right", expected_generation=1)
        registry.bind("right", player_id, "HDMI-A-2", expected_generation=2)
        _poll(page)

        dialog.get_by_role("button", name="Confirm unbind all", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text("1 of 2 unbound")
        expect(dialog.get_by_role("list", name="Result for each frame").get_by_role("listitem")
               ).to_have_text(["left: unbound", "right: changed since you opened this"])
        page.wait_for_timeout(300)
        assert len(sent) == 2
        frames = {frame.id: frame for frame in registry.inventory().frames}
        assert frames["left"].player_id is None and frames["right"].player_id == player_id


def test_unbind_all_stops_at_an_unknown_outcome(page, registry):
    player_id = _two_bound(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")
        dialog = _open_unbind_all(page, player_id)
        page.route("**/v1/operator/frames/*/binding", lambda route: route.abort()
                   if route.request.method == "DELETE" else route.continue_())
        dialog.get_by_role("button", name="Confirm unbind all", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text("0 of 2 unbound")
        expect(dialog.get_by_role("list", name="Result for each frame").get_by_role("listitem")
               ).to_have_text(["left: outcome unknown", "right: not attempted"])


def test_a_dialog_survives_a_poll_that_regroups_its_player(page, registry):
    identity, _, _ = enroll(registry, count=1)
    player_id = identity["player_id"]
    handle = player_id[-6:]
    _placed_frame(registry, "lobby-left")
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        connect(page, origin, "equipment")
        _group(page, "Pending players").get_by_role(
            "button", name=f"Retire player {player_id}", exact=True).click()
        dialog = _dialog(page)
        dialog.get_by_label(f"Type {handle} to confirm", exact=True).fill(handle)

        # Another operator binds the Player; the poll moves it to Bound players.
        registry.bind("lobby-left", player_id, "HDMI-A-1", expected_generation=0)
        _poll(page)
        expect(_group(page, "Bound players").get_by_role("button", name=player_id, exact=True)
               ).to_be_attached()

        # The dialog is still open with what was typed; Central refuses the retire.
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="Confirm retire", exact=True).click()
        expect(dialog.get_by_role("alert")).to_contain_text("has a bound output")
        assert registry.inventory().players[0].retired_at is None


def test_the_roster_says_when_there_are_no_players(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")
        expect(_group(page, "Pending players")).to_contain_text(
            "No Players yet. Power on one Pi on this network; it appears under Pending.")


def test_a_free_output_says_when_there_are_no_unbound_frames(page, registry):
    enroll(registry, count=1)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")
        expect(_group(page, "Pending players")).to_contain_text(
            "No unbound frames. Draw one on the plan first.")


def test_the_roster_never_scrolls_sideways_at_390_px(page, registry):
    long_id = "reception-" + "north-wall-left-of-the-main-entrance-" * 2 + "panel"
    enroll(registry, count=2)
    _two_bound(registry)
    registry.create_frame(FrameCreate(
        id=long_id, surface_id="wall", x_mm=100, y_mm=900,
        width_mm=400, height_mm=300, profile=LANDSCAPE))
    page.set_viewport_size({"width": 390, "height": 844})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "equipment")
        expect(_group(page, "Pending players").get_by_role("combobox").first).to_be_visible()
        fits = page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
        assert fits, page.evaluate("""() => [...document.querySelectorAll("body *")]
            .filter((el) => el.getBoundingClientRect().right > window.innerWidth + 0.5)
            .map((el) => el.tagName + "." + [...el.classList].join(".")).slice(0, 12)""")
