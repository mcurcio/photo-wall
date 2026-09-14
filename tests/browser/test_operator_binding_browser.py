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

import pytest
from operator_harness import operator_server
from playwright.sync_api import expect
from test_registry import ADMIN, enroll

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

        # Select the Frame on the plan, open the Binding facet, bind the pending
        # Output.
        page.get_by_role("button", name="Frame wall-1", exact=True).click()
        inspector = page.get_by_role("region", name="Frame wall-1 inspector", exact=True)
        inspector.get_by_role("tab", name="Binding", exact=True).click()
        inspector.get_by_role("button", name="Bind pending display", exact=True).click()

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
        # facet offers an ENABLED "Bind pending display".
        page.get_by_role("button", name="Frame wall-r", exact=True).click()
        inspector = page.get_by_role("region", name="Frame wall-r inspector", exact=True)
        inspector.get_by_role("tab", name="Binding", exact=True).click()
        bind_button = inspector.get_by_role("button", name="Bind pending display", exact=True)
        expect(bind_button).to_be_enabled()

        # Retire the pending Player from the rail (a deliberate, labelled action).
        pending.get_by_role(
            "button", name=f"Retire player {player_id}", exact=True
        ).click()

        # It moves to the Retired rail (by identity) and leaves the Pending rail.
        expect(retired.get_by_role("button", name=player_id, exact=True)).to_be_visible()
        expect(
            pending.get_by_role("button", name=player_id, exact=True)
        ).to_have_count(0)

        # Its Output is no longer offered: the bind control has no pending Output
        # to bind, so it is disabled (pendingOutput == null after retire).
        expect(bind_button).to_be_disabled()


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
        _connect(page, origin)

        # Wait for the console to hold the Frame at generation 0.
        page.get_by_role("button", name="Frame stale-1", exact=True).click()
        inspector = page.get_by_role("region", name="Frame stale-1 inspector", exact=True)
        inspector.get_by_role("tab", name="Binding", exact=True).click()
        expect(
            inspector.get_by_role("button", name="Bind pending display", exact=True)
        ).to_be_visible()

        # Server-side, advance the Frame's generation out from under the console
        # (bind then unbind each bump generation) without the console refreshing.
        registry.bind("stale-1", identity["player_id"], "HDMI-A-1", expected_generation=0)
        registry.unbind("stale-1", expected_generation=1)

        # The console still holds generation 0, so its bind carries a stale
        # expected_generation -> 409 binding_generation_conflict -> reload/review.
        # The distinctive generation-conflict wording (design §4b) — NOT a
        # generic bind failure — proves the stale expected_generation was carried
        # and the 409 binding_generation_conflict was surfaced.
        inspector.get_by_role("button", name="Bind pending display", exact=True).click()
        expect(
            inspector.get_by_text("This Frame changed", exact=False)
        ).to_be_visible()


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
