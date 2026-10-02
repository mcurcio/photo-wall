"""Behavioral browser checks for onboarding & binding at /console (Bead 9).

Reuses the existing operator browser harness (real Chromium against the
production create_app on an ephemeral loopback listener, the disposable-schema
`registry` fixture, and the autouse `page_errors` guard). Equipment is seeded
directly through the registry (as the `installation`/`enroll` fixtures do) so the
console renders real inventory: unbound Players, real Frame generations, and a
real returning-Pi reassociation. The boxes are on Players (console DDD pass 1): a list
with one row per box and a Player page per box, where Bind, Identify, Retire and Unbind
all start.

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
from console_tasks import connect, go, open_frame, open_player, player_name, visible_page
from operator_harness import (
    RequestGate,
    assert_fits_width,
    drive_poll,
    operator_server,
    pause_page_clock,
    sign_in,
)
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


def test_an_enrolled_player_appears_on_the_players_list(page, registry):
    # A freshly enrolled Player is unbound and not retired: one row on the list, by name.
    identity, _, _ = enroll(registry, count=2)
    name = player_name(registry, identity["player_id"])
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "players")

        players = page.get_by_role("list", name="Players", exact=True)
        row = players.get_by_role("listitem").filter(
            has=page.get_by_role("link", name=name, exact=True))
        expect(row).to_have_count(1)
        expect(row).to_contain_text("Standing: Unbound · enrolled")


def test_unbound_output_identify_requests_exact_output_and_explains_no_display(page, registry):
    identity, _, _ = enroll(registry, count=2)
    player_id = identity["player_id"]
    _disconnect_output(registry, player_id, "HDMI-A-2")
    name = player_name(registry, player_id)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        open_player(page, name)
        outputs = page.get_by_role("list", name=f"Outputs of {name}", exact=True)
        identify = outputs.get_by_role(
            "button", name="Identify display HDMI-A-1", exact=True)
        no_display = outputs.get_by_role(
            "button", name="Identify display HDMI-A-2", exact=True)
        expect(identify).to_be_enabled()
        expect(no_display).to_be_disabled()
        expect(no_display).to_have_accessible_description(
            "Connect a Panel and restart the Player, then press Refresh.")

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
            "Identify requested for HDMI-A-1. Check the Panel; "
            "this request expires in 15 seconds.")
        expect(identify).to_be_enabled()


@pytest.mark.parametrize("status, expected", [
    (404, "This Output changed or the Player is no longer eligible. "
          "Press Refresh before trying again."),
    (409, "This Output changed or the Player is no longer eligible. "
          "Press Refresh before trying again."),
    (503, "The request outcome is unknown. Check the display before trying again."),
])
def test_unbound_output_identify_failure_is_honest(page, registry, status, expected):
    identity, _, _ = enroll(registry, count=1)
    name = player_name(registry, identity["player_id"])
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        open_player(page, name)
        outputs = page.get_by_role("list", name=f"Outputs of {name}", exact=True)
        identify = outputs.get_by_role(
            "button", name="Identify display HDMI-A-1", exact=True)
        gate = RequestGate(page, "**/v1/operator/players/*/outputs/*/identify")
        gate.holding = True
        identify.click()
        gate.wait_held()
        gate.release(status=status, content_type="application/json", body=json.dumps({
            "error": {404: "unknown_output", 409: "output_disconnected"}.get(
                status, "server_unavailable"),
        }))
        expect(outputs.get_by_role("alert")).to_have_text(expected)
        expect(identify).to_be_enabled()


def test_binding_pending_output_shows_review_and_commission_cta(page, registry):
    identity, _, _ = enroll(registry, count=1)  # an unbound Player with HDMI-A-1
    _placed_frame(registry, "wall-1")
    name = player_name(registry, identity["player_id"])
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        player = open_player(page, name)
        expect(player).to_contain_text("Standing: Unbound")

        # Select the Frame on the plan, open the Binding facet, choose the unbound
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

        # On its Player page the Player is now Bound, its Output bound to the Frame (Plane A
        # refreshed via useMutate), with a link to the Frame's home.
        player = open_player(page, name)
        expect(player).to_contain_text("Standing: Bound")
        outputs = page.get_by_role("list", name=f"Outputs of {name}", exact=True)
        expect(outputs).to_contain_text("HDMI-A-1 · Bound to Frame wall-1")
        expect(outputs.get_by_role("link", name="Frame wall-1", exact=True)).to_be_visible()


def test_retiring_an_unbound_player_marks_it_retired_and_drops_its_output(page, registry):
    # Bead G1 (SR-retire): the console must re-host the legacy "Retire a Player"
    # control (legacy test_operator_browser.py:137-141) so the cutover keeps
    # content parity. Retiring an unbound Player marks it Retired on its page AND
    # removes its Output from the Binding facet's bind choices.
    identity, _, _ = enroll(registry, count=1)  # an unbound Player with HDMI-A-1
    _placed_frame(registry, "wall-r")
    player_id = identity["player_id"]
    name = player_name(registry, player_id)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)

        # Precondition: the Player's Output IS a bind candidate — the Binding
        # facet's chooser offers it.
        inspector = open_frame(page, "wall-r", "binding")
        expect(_option(inspector, player_id)).to_be_visible()

        # Retire the unbound Player from its page (a deliberate, labelled action),
        # typing its handle to confirm (slice 2 §7).
        player = open_player(page, name)
        player.get_by_role("button", name=f"Retire player {player_id}", exact=True).click()
        _type_handle_and_retire(page, player_id)

        # Its page now reads Retired, offers no Retire, and its node layers are not read.
        expect(player).to_contain_text("Standing: Retired")
        expect(player.get_by_role("button", name=f"Retire player {player_id}", exact=True)
               ).to_have_count(0)
        expect(player).to_contain_text("Not read: Player retired")

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
        expect(page.get_by_role("list", name="Players", exact=True)).to_have_count(0)

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
    name = player_name(registry, player_id)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        player = open_player(page, name)
        opener = player.get_by_role("button", name=f"Retire player {player_id}", exact=True)
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
        expect(player).to_contain_text("Standing: Retired")
        # Focus successor: the Player's name; a status line says what happened.
        expect(page.get_by_role("heading", level=2, name=name, exact=True)).to_be_focused()
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


def test_the_devices_serial_shows_in_the_chooser_and_on_the_player_page(page, registry):
    _netbooted_player(registry)
    _placed_frame(registry, "boot-1")
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        inspector = open_frame(page, "boot-1", "binding")
        # The handle is the serial's suffix (joined on device_id, not the Player id).
        expect(_serial_option(inspector)).to_be_visible()
        # The Player is named by its serial handle; the serial is its claim, and the
        # netboot record is labelled with the boot path that produced it.
        player = open_player(page, f"Player …{SERIAL[-6:]}")
        expect(player).to_contain_text(
            f"Serial: Serial {SERIAL} (claimed at boot by the box, unverified)")
        expect(page.get_by_role("region", name="Boot", exact=True)).to_contain_text(
            "Netboot base without an offer: Legacy netboot seen, no image served yet")


def test_the_players_list_names_boxes_by_their_distinct_serial_handles(page, registry):
    first_player = _netbooted_player(registry, SERIAL)
    second_player = _netbooted_player(registry, "10000000c0ffee93")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "players")
        players = page.get_by_role("list", name="Players", exact=True)
        first = players.get_by_role("link", name="Player …ffee42", exact=True)
        second = players.get_by_role("link", name="Player …ffee93", exact=True)
        expect(first).to_be_visible()
        expect(second).to_be_visible()

        # Each link opens that box's page; the Registry Player id is an identifier there.
        first.click()
        player = visible_page(page)
        expect(page.get_by_role("heading", level=2, name="Player …ffee42", exact=True)
               ).to_be_visible()
        expect(player).to_contain_text(f"Serial {SERIAL}")
        player.get_by_text("Identifiers", exact=True).click()
        expect(player).to_contain_text(f"Registry Player {first_player}")
        expect(player.get_by_role("button", name=f"Retire player {first_player}", exact=True)
               ).to_be_visible()
        expect(player.get_by_text(second_player, exact=False)).to_have_count(0)


def test_missing_boot_facts_do_not_show_a_fallback_serial_handle(page, registry):
    identity, _, _ = enroll(registry, count=1)
    player_id = identity["player_id"]
    name = player_name(registry, player_id)
    page.route(NETBOOT, lambda route: route.fulfill(
        status=503, content_type="application/json", body='{"error": "content_unavailable"}'))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "players")
        listing = page.get_by_role("region", name="Players list", exact=True)
        expect(listing).to_contain_text("Boot records unavailable")
        # Named by its device id, never by a guessed serial.
        expect(listing.get_by_role("link", name=name, exact=True)).to_be_visible()
        expect(listing.get_by_text(re.compile(r"Player …"))).to_have_count(0)
        # The existing action remains available even when serial enrichment fails.
        player = open_player(page, name)
        expect(player.get_by_role("button", name=f"Retire player {player_id}", exact=True)
               ).to_be_visible()


def test_a_player_that_never_netbooted_reads_no_netboot_record(page, registry):
    identity, _, _ = enroll(registry, count=1)
    _placed_frame(registry, "boot-2")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        open_player(page, player_name(registry, identity["player_id"]))
        # Once the boot records are read, no netboot base (and no V1 offer) is recorded for it.
        boot = page.get_by_role("region", name="Boot", exact=True)
        expect(boot).not_to_contain_text("not read yet")
        expect(boot).not_to_contain_text("Netboot base without an offer")
        expect(boot).not_to_contain_text("V1 boot offer")
        # Without a serial the handle is the Player id's hash suffix.
        inspector = open_frame(page, "boot-2", "binding")
        expect(_option(inspector, identity["player_id"])).to_be_visible()


OLD, NEW = "v1.4.2", "v1.5.0"


@pytest.mark.parametrize(("outcome", "served", "good", "failed", "label"), [
    ("healthy", OLD, OLD, None,
     f"Legacy health report for {OLD} · exact base acceptance unverified"),
    ("healthy", OLD, OLD, NEW,
     f"Legacy health report for {OLD} after {NEW} · exact base acceptance unverified"),
    ("pending", NEW, OLD, None,
     f"Legacy netboot served {NEW} · base acceptance unknown · legacy known-good tag {OLD}, bytes unverified"),
    ("pending", NEW, None, None,
     f"Legacy netboot served {NEW} · base acceptance unknown · no verified fallback"),
    ("pending", OLD, OLD, NEW,
     f"Legacy netboot served {OLD} · base acceptance unknown · legacy known-good tag {OLD}, bytes unverified"),
    ("pending", NEW, None, NEW,
     f"Legacy netboot served {NEW} · base acceptance unknown · no verified fallback"),
    ("failed", NEW, OLD, NEW,
     f"Legacy netboot marked {NEW} failed · physical failure unconfirmed · legacy known-good tag {OLD}, bytes unverified"),
    ("failed", NEW, None, NEW,
     f"Legacy netboot marked {NEW} failed · physical failure unconfirmed · no verified fallback"),
])
def test_the_legacy_boot_outcome_discloses_uncertainty(
        page, registry, outcome, served, good, failed, label):
    _netbooted_player(registry)
    row = {"device_id": device_id_for_serial(SERIAL), "serial": SERIAL, "attached_tag": None,
           "known_good_tag": good, "last_served_tag": served, "boot_outcome": outcome,
           "failed_tag": failed}
    page.route(NETBOOT, lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"frontier": NEW, "devices": [row]})))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        open_player(page, f"Player …{SERIAL[-6:]}")
        boot = page.get_by_role("region", name="Boot", exact=True)
        expect(boot.get_by_text(f"Netboot base without an offer: {label}", exact=True)
               ).to_be_visible()


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
    name = player_name(registry, identity["player_id"])
    page.route(NETBOOT, lambda route: route.fulfill(
        status=401, content_type="application/json", body='{"error": "unauthorized"}'))
    with operator_server(registry.db, registry.clock) as origin:
        with page.expect_response(NETBOOT):
            sign_in(page, origin)
        go(page, "players")
        listing = page.get_by_role("region", name="Players list", exact=True)
        expect(listing).to_contain_text("Boot records unavailable")
        # The session is untouched: a refresh still authenticates and applies.
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(page.get_by_text(re.compile(r"updated [01] s ago"))).to_be_visible()
        expect(page.get_by_text("not accepted", exact=False)).to_have_count(0)
        expect(listing.get_by_role("link", name=name, exact=True)).to_be_visible()


# --- The Player page's Outputs and danger zone (console DDD §9).


def _card_outputs(page, name):
    return page.get_by_role("list", name=f"Outputs of {name}", exact=True)


def _frame_select(page, player_id, output_id="HDMI-A-1"):
    label = f"Frame for {player_id[-6:]} · {output_id} · Free"
    return page.get_by_role("combobox", name=label, exact=True)


def _danger(page):
    return page.get_by_role("region", name="Danger zone", exact=True)


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


def _open_unbind_all(page, registry, player_id):
    open_player(page, player_name(registry, player_id))
    _danger(page).get_by_role(
        "button", name=f"Unbind all outputs of {player_id}", exact=True).click()
    dialog = _dialog(page)
    expect(dialog).to_be_visible()
    return dialog


def test_the_player_page_lists_each_output_with_its_state_and_offers_no_retire_when_bound(
        page, registry):
    identity, _, _ = enroll(registry, count=2)
    player_id = identity["player_id"]
    handle = player_id[-6:]
    name = player_name(registry, player_id)
    _placed_frame(registry, "lobby-left")
    registry.bind("lobby-left", player_id, "HDMI-A-1", expected_generation=0)
    _disconnect_output(registry, player_id, "HDMI-A-2")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        player = open_player(page, name)
        expect(player).to_contain_text("Standing: Bound · 0 of 2 outputs free")
        expect(player.get_by_role("link", name="Frame lobby-left").first).to_be_visible()
        outputs = _card_outputs(page, name).get_by_role("listitem")
        expect(outputs).to_have_count(2)
        expect(outputs.nth(0)).to_contain_text("HDMI-A-1 · Bound to Frame lobby-left")
        expect(outputs.nth(0)).to_contain_text(
            "Panel at last start (stale): Player app reported a 1920×1080 Panel on HDMI-A-1 "
            "at its last start · first received")
        expect(outputs.nth(1)).to_contain_text(
            f"{handle} · HDMI-A-2 · No display detected at last Player start")
        expect(outputs.nth(1)).to_contain_text("no Panel on HDMI-A-2 at its last start")
        expect(_danger(page).get_by_role("button", name=f"Retire player {player_id}", exact=True)
               ).to_have_count(0)
        expect(_danger(page).get_by_role("button", name=f"Unbind all outputs of {player_id}",
                                         exact=True)).to_be_visible()


def test_output_first_bind_opens_the_frame(page, registry):
    identity, _, _ = enroll(registry, count=1)
    player_id = identity["player_id"]
    name = player_name(registry, player_id)
    _placed_frame(registry, "lobby-left")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        open_player(page, name)
        _frame_select(page, player_id).select_option("lobby-left")
        _card_outputs(page, name).get_by_role("button", name="Bind HDMI-A-1", exact=True).click()
        inspector = page.get_by_role("region", name="Frame lobby-left inspector", exact=True)
        expect(inspector.get_by_role("heading", name="Frame lobby-left", exact=True)
               ).to_be_focused()
        expect(inspector.get_by_role("tab", name="Binding", exact=True)
               ).to_have_attribute("aria-selected", "true")
        frame = registry.inventory().frames[0]
        assert (frame.player_id, frame.output_id) == (player_id, "HDMI-A-1")


def test_a_player_page_bind_carries_the_generation_captured_on_selection(page, registry):
    identity, _, _ = enroll(registry, count=1)
    other, _, _ = enroll(registry, count=1)
    player_id = identity["player_id"]
    name = player_name(registry, player_id)
    _placed_frame(registry, "lobby-left")
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        connect(page, origin)
        open_player(page, name)
        _frame_select(page, player_id).select_option("lobby-left")  # captures generation 0
        # The Frame changes (bound elsewhere, then unbound: generation 2), and a poll
        # delivers that; it is still unbound, so the choice stands.
        registry.bind("lobby-left", other["player_id"], "HDMI-A-1", expected_generation=0)
        registry.unbind("lobby-left", expected_generation=1)
        _poll(page)
        expect(_frame_select(page, player_id)).to_have_value("lobby-left")
        outputs = _card_outputs(page, name)
        outputs.get_by_role("button", name="Bind HDMI-A-1", exact=True).click()
        expect(outputs.get_by_role("alert")).to_contain_text("This Frame changed")
        assert registry.inventory().frames[0].player_id is None


def test_a_player_page_pick_whose_frame_is_bound_elsewhere_is_dropped_and_announced(
        page, registry):
    identity, _, _ = enroll(registry, count=1)
    other, _, _ = enroll(registry, count=1)
    player_id = identity["player_id"]
    _placed_frame(registry, "lobby-left")
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        connect(page, origin)
        open_player(page, player_name(registry, player_id))
        _frame_select(page, player_id).select_option("lobby-left")
        registry.bind("lobby-left", other["player_id"], "HDMI-A-1", expected_generation=0)
        _poll(page)
        expect(visible_page(page).get_by_role("status").filter(
            has_text="Frame lobby-left is no longer available. Choose another frame.")
        ).to_have_count(1)


def test_unbind_all_lists_each_frame_and_its_live_runs(page, registry):
    player_id = _two_bound(registry, live_run_on="left")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        dialog = _open_unbind_all(page, registry, player_id)
        frames = dialog.get_by_role("list", name="Frames to unbind").get_by_role("listitem")
        expect(frames).to_have_text([
            "Frame left (HDMI-A-1) — live Runs: lobby-loop",
            "Frame right (HDMI-A-2)",
        ])


def test_unbind_all_reports_each_frame_and_never_resends_a_conflict(page, registry):
    player_id = _two_bound(registry)
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        connect(page, origin)
        sent = _binding_requests(page)
        dialog = _open_unbind_all(page, registry, player_id)  # captures left@1, right@1
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
        connect(page, origin)
        dialog = _open_unbind_all(page, registry, player_id)
        page.route("**/v1/operator/frames/*/binding", lambda route: route.abort()
                   if route.request.method == "DELETE" else route.continue_())
        dialog.get_by_role("button", name="Confirm unbind all", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text("0 of 2 unbound")
        expect(dialog.get_by_role("list", name="Result for each frame").get_by_role("listitem")
               ).to_have_text(["left: outcome unknown", "right: not attempted"])


def test_a_dialog_survives_a_poll_that_changes_its_players_standing(page, registry):
    identity, _, _ = enroll(registry, count=1)
    player_id = identity["player_id"]
    handle = player_id[-6:]
    name = player_name(registry, player_id)
    _placed_frame(registry, "lobby-left")
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        connect(page, origin)
        player = open_player(page, name)
        _danger(page).get_by_role("button", name=f"Retire player {player_id}", exact=True).click()
        dialog = _dialog(page)
        dialog.get_by_label(f"Type {handle} to confirm", exact=True).fill(handle)

        # Another operator binds the Player; the poll makes it Bound.
        registry.bind("lobby-left", player_id, "HDMI-A-1", expected_generation=0)
        _poll(page)
        expect(player).to_contain_text("Standing: Bound")

        # The dialog is still open with what was typed; Central refuses the retire.
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="Confirm retire", exact=True).click()
        expect(dialog.get_by_role("alert")).to_contain_text("has a bound output")
        assert registry.inventory().players[0].retired_at is None


def test_the_players_list_says_when_there_are_no_players(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "players")
        expect(page.get_by_role("region", name="Players list", exact=True)).to_contain_text(
            "No Players yet. Power on one Pi on this network; it appears here.")


def test_a_free_output_says_when_there_are_no_unbound_frames(page, registry):
    identity, _, _ = enroll(registry, count=1)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        player = open_player(page, player_name(registry, identity["player_id"]))
        expect(player).to_contain_text("No unbound frames. Draw one on the plan first.")


def test_the_players_pages_never_scroll_sideways_at_390_px(page, registry):
    long_id = "reception-" + "north-wall-left-of-the-main-entrance-" * 2 + "panel"
    identity, _, _ = enroll(registry, count=2)
    _two_bound(registry)
    registry.create_frame(FrameCreate(
        id=long_id, surface_id="wall", x_mm=100, y_mm=900,
        width_mm=400, height_mm=300, profile=LANDSCAPE))
    page.set_viewport_size({"width": 390, "height": 844})
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "players")
        expect(page.get_by_role("list", name="Players", exact=True)).to_be_visible()
        assert_fits_width(page, "Players list")
        player = open_player(page, player_name(registry, identity["player_id"]))
        expect(player.get_by_role("combobox").first).to_be_visible()
        player.get_by_text("Identifiers", exact=True).click()
        assert_fits_width(page, "Player page")
