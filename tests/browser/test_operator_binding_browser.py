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

import os
import re

import pytest
from operator_harness import RequestGate, operator_server, pause_page_clock
from playwright.sync_api import expect
from test_registry import ADMIN, enroll

from central.content_catalog.catalog import device_id_for_serial
from central.registry import FrameCreate
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


def _connect(page, origin):
    page.goto(origin + "/console")
    page.get_by_label("Operator token").fill(ADMIN)
    page.get_by_role("button", name="Connect", exact=True).click()


def _binding_facet(page, frame_id):
    """Select a frame on the plan and open its Binding facet; returns the Inspector."""
    page.get_by_role("button", name=f"Frame {frame_id}", exact=True).click()
    inspector = page.get_by_role("region", name=f"Frame {frame_id} inspector", exact=True)
    inspector.get_by_role("tab", name="Binding", exact=True).click()
    return inspector


def _option(scope, player_id, output_id="HDMI-A-1"):
    """A free Output in the chooser, by its one wording: handle · output id · Free (the
    handle is the Player id's last six characters when there is no netboot record)."""
    return scope.get_by_role("radio", name=f"{player_id[-6:]} · {output_id} · Free", exact=True)


def _poll(page):
    """Run the paused page clock one poll interval and wait for that read to answer."""
    with page.expect_response("**/v1/operator/inventory"):
        page.clock.run_for(5000)
    page.wait_for_timeout(300)


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
    inspector = _binding_facet(page, frame_id)
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
        _connect(page, origin)

        pending = page.get_by_role("group", name="Pending players", exact=True)
        expect(
            pending.get_by_role("button", name=identity["player_id"], exact=True)
        ).to_be_visible()


def test_binding_pending_output_shows_review_and_commission_cta(page, registry):
    identity, _, _ = enroll(registry, count=1)  # a pending Player with HDMI-A-1
    _placed_frame(registry, "wall-1")
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)

        pending = page.get_by_role("group", name="Pending players", exact=True)
        expect(
            pending.get_by_role("button", name=identity["player_id"], exact=True)
        ).to_be_visible()

        # Select the Frame on the plan, open the Binding facet, choose the pending
        # Output and bind it.
        inspector = _binding_facet(page, "wall-1")
        _option(inspector, identity["player_id"]).check()
        inspector.get_by_role("button", name="Bind to wall-1", exact=True).click()

        # The now-bound Player leaves the Pending rail (Plane A refreshed via
        # useMutate).
        expect(
            pending.get_by_role("button", name=identity["player_id"], exact=True)
        ).to_have_count(0)

        # The facet shows the amber "Review required" state and the CTA...
        expect(inspector.get_by_text("Review required", exact=False)).to_be_visible()
        cta = inspector.get_by_role("button", name="Commission the display", exact=True)
        expect(cta).to_be_visible()

        # ...and the CTA switches the Inspector to the Commissioning facet.
        cta.click()
        expect(
            inspector.get_by_role("tabpanel", name="Commissioning facet")
        ).to_be_visible()


def test_retiring_a_pending_player_moves_it_to_retired_and_drops_its_output(page, registry):
    # Bead G1 (SR-retire): the console must re-host the legacy "Retire a Player"
    # control (legacy test_operator_browser.py:137-141) so the cutover keeps
    # content parity. Retiring a pending Player moves it to the Retired rail AND
    # removes its Output from the Binding facet's bind choices.
    identity, _, _ = enroll(registry, count=1)  # a pending Player with HDMI-A-1
    _placed_frame(registry, "wall-r")
    player_id = identity["player_id"]
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)

        pending = page.get_by_role("group", name="Pending players", exact=True)
        retired = page.get_by_role("group", name="Retired players", exact=True)
        expect(
            pending.get_by_role("button", name=player_id, exact=True)
        ).to_be_visible()

        # Precondition: the Player's Output IS a bind candidate — the Binding
        # facet's chooser offers it.
        inspector = _binding_facet(page, "wall-r")
        expect(_option(inspector, player_id)).to_be_visible()

        # Retire the pending Player from the rail (a deliberate, labelled action),
        # typing its handle to confirm (slice 2 §7).
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
        # chooser says there is nothing free to bind.
        expect(inspector.get_by_role("radio")).to_have_count(0)
        expect(inspector.get_by_text("No free outputs with a detected display",
                                     exact=False)).to_be_visible()


def test_connect_with_a_rejected_token_shows_not_accepted_and_returns_to_login(page, registry):
    # Bead G3 (SR-parity, GAP 4): a rejected operator token must surface an
    # explicit "not accepted" message and drop back to the token-entry state —
    # NOT silently blank (legacy test_operator_browser.py:99-104,164-177). The
    # console is REST (per-request bearer auth), so this is the ONLY token-
    # rejection behavior with a console equivalent; the legacy operator-websocket
    # fencing (test_operator_browser.py:185-226) has none by architecture.
    identity, _, _ = enroll(registry, count=1)
    _placed_frame(registry, "auth-1")
    with operator_server(registry.db, registry.clock) as origin:
        page.goto(origin + "/console")

        # Connect with a WRONG token: the production auth dependency 401s the
        # inventory/runtime/media fetch on connect.
        page.get_by_label("Operator token").fill("not-the-admin-token")
        page.get_by_role("button", name="Connect", exact=True).click()

        # The 401 surfaces an explicit auth-rejected message...
        expect(page.get_by_role("alert")).to_contain_text("not accepted")
        # ...and the console stays on the token form (never enters the connected
        # state): the token input + Connect button remain, and NO connected
        # content (the Pending rail) rendered.
        expect(page.get_by_label("Operator token")).to_be_visible()
        expect(page.get_by_role("button", name="Connect", exact=True)).to_be_visible()
        expect(
            page.get_by_role("group", name="Pending players", exact=True)
        ).to_have_count(0)

        # Recovery: the CORRECT token connects and the real inventory renders,
        # and the rejection message is gone.
        page.get_by_label("Operator token").fill(ADMIN)
        page.get_by_role("button", name="Connect", exact=True).click()
        expect(page.get_by_role("button", name="Frame auth-1", exact=True)).to_be_visible()
        expect(page.get_by_role("alert")).to_have_count(0)


def test_stale_generation_bind_surfaces_the_reload_review_message(page, registry):
    identity, _, _ = enroll(registry, count=1)
    _placed_frame(registry, "stale-1")
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        _connect(page, origin)

        # The operator chooses the Output while the console holds the Frame at
        # generation 0: the choice captures that generation.
        inspector = _binding_facet(page, "stale-1")
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
        _connect(page, origin)
        inspector = _binding_facet(page, "right")

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
        _connect(page, origin)
        inspector = _binding_facet(page, "choose-1")
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
        _connect(page, origin)
        inspector = _binding_facet(page, "mine")
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
        _connect(page, origin)
        inspector = _binding_facet(page, "only")
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
        _connect(page, origin)

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
        _connect(page, origin)

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

        # Refresh the console (re-Connect performs one Plane A refresh); the diff
        # against the retained prior snapshot now surfaces the recovery banner.
        page.get_by_role("button", name="Connect", exact=True).click()
        banner = page.get_by_text("Recovered — already bound", exact=False)
        expect(banner).to_be_visible()
        expect(banner).to_contain_text(identity["player_id"])


# --- One confirmation pattern (slice 2 §7).


def test_retire_is_enabled_only_by_typing_the_handle(page, registry):
    identity, _, _ = enroll(registry, count=1)
    player_id = identity["player_id"]
    handle = player_id[-6:]
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
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
        expect(retired.get_by_role("heading", name="Retired", exact=True)).to_be_focused()
        expect(page.get_by_text(f"Player {player_id} retired.", exact=True)).to_be_visible()


def test_esc_is_blocked_while_an_unbind_is_in_flight_even_when_repeated(page, registry):
    _bound(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
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
        _connect(page, origin)
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
        _connect(page, origin)
        _inspector, dialog = _open_unbind(page)
        registry.unbind("bound-1", expected_generation=1)
        dialog.get_by_role("button", name="Confirm unbind", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text("Already done.")
        expect(dialog.get_by_role("alert")).to_have_count(0)


def test_an_unbind_that_gets_no_answer_reads_outcome_unknown(page, registry):
    _bound(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        _inspector, dialog = _open_unbind(page)
        page.route("**/v1/operator/frames/*/binding", lambda route: route.abort()
                   if route.request.method == "DELETE" else route.continue_())
        dialog.get_by_role("button", name="Confirm unbind", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text(
            "Central did not answer. Check this after the next refresh.")
        expect(dialog.get_by_role("button", name="Confirm unbind", exact=True)).to_have_count(0)


def test_a_refresh_failure_after_an_unbind_is_not_a_refusal(page, registry):
    _bound(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        inspector, dialog = _open_unbind(page)
        page.route("**/v1/operator/inventory", lambda route: route.fulfill(
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


def test_the_devices_serial_shows_in_the_chooser_and_the_rail(page, registry):
    _netbooted_player(registry)
    _placed_frame(registry, "boot-1")
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        inspector = _binding_facet(page, "boot-1")
        # The handle is the serial's suffix (joined on device_id, not the Player id).
        expect(_serial_option(inspector)).to_be_visible()
        pending = page.get_by_role("group", name="Pending players", exact=True)
        expect(pending).to_contain_text(f"{SERIAL} · Netboot seen, no image served yet")


def test_a_player_that_never_netbooted_reads_no_netboot_record(page, registry):
    identity, _, _ = enroll(registry, count=1)
    _placed_frame(registry, "boot-2")
    with operator_server(registry.db, registry.clock) as origin:
        _connect(page, origin)
        pending = page.get_by_role("group", name="Pending players", exact=True)
        expect(pending).to_contain_text("No netboot record")
        # Without a serial the handle is the Player id's hash suffix.
        expect(_option(_binding_facet(page, "boot-2"), identity["player_id"])).to_be_visible()


def test_a_failed_boot_facts_read_keeps_the_serials(page, registry):
    _netbooted_player(registry)
    _placed_frame(registry, "boot-3")
    with operator_server(registry.db, registry.clock) as origin:
        pause_page_clock(page, registry.clock.utc())
        _connect(page, origin)
        inspector = _binding_facet(page, "boot-3")
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
            _connect(page, origin)
        pending = page.get_by_role("group", name="Pending players", exact=True)
        expect(pending).to_contain_text("Boot records unavailable")
        # The session is untouched: a refresh still authenticates and applies.
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(page.get_by_text(re.compile(r"updated [01] s ago"))).to_be_visible()
        expect(page.get_by_text("not accepted", exact=False)).to_have_count(0)
        expect(pending.get_by_role("button", name=identity["player_id"], exact=True)
               ).to_be_visible()
