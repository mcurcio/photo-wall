"""Behavioral checks for the console shell's review fixes (bead 1b review; flow design §6):
the landing route's race with a chosen section, the sign-in overlay over an open confirmation,
confirmations on Show pages that browser Back hides, the Surface filter under a typed frame
route, and where focus goes after "Show all" and a drawer link opened elsewhere.

Reuses the operator browser harness (real Chromium against the production create_app on an
ephemeral loopback listener, the disposable-schema `registry` fixture and the autouse
`page_errors` guard) and the task helpers of console_tasks.py.
"""

import os
import re
import sys

import pytest
from console_tasks import (
    LABELS,
    author_scene,
    connect,
    current_hash,
    go,
    open_player,
    player_name,
    show_now,
    visit,
)
from operator_harness import SNAPSHOT, RequestGate, operator_server, sign_in
from playwright.sync_api import expect
from test_operator_showrunner_browser import SCENE_ID, SOURCE, VALID_FRAME, _seed, _seed_source
from test_registry import ADMIN, enroll

from central.registry import FrameCreate
from contracts.models import FrameProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

PORTRAIT = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)
NARROW = {"width": 390, "height": 844}



def _heading(page, section):
    return page.get_by_role("heading", level=1, name=LABELS[section], exact=True)


def _frame(registry, frame_id, *, x_mm=100, surface="wall"):
    registry.create_frame(FrameCreate(
        id=frame_id, surface_id=surface, x_mm=x_mm, y_mm=100,
        width_mm=300, height_mm=500, profile=PORTRAIT))


# --- Landing (item 9: CI's race).

# When the first snapshot's content is committed (its status bar appears) and BEFORE React
# runs that render's effects, the hash moves to #/wall as a sidebar link does: the rendered
# route is still null, and the `hashchange` is queued behind the effect. A MutationObserver
# callback runs at the end of the commit's task, between the two.
_CHOOSE_WALL_AT_FIRST_SNAPSHOT = """
(() => {
  let done = false;
  new MutationObserver(() => {
    if (!done && document.querySelector('[aria-label="Snapshot status"]') !== null) {
      done = true;
      window.location.hash = "#/wall";
    }
  }).observe(document, {childList: true, subtree: true});
})();
"""


def test_a_section_chosen_as_the_first_snapshot_renders_is_not_replaced_by_landing(
        page, registry):
    _frame(registry, "first")  # with a frame the landing route is #/now, not #/wall
    page.add_init_script(_CHOOSE_WALL_AT_FIRST_SNAPSHOT)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        expect(_heading(page, "wall")).to_be_visible()
        page.wait_for_timeout(300)  # any landing replace would have run by now
        assert current_hash(page) == "#/wall"
        expect(_heading(page, "wall")).to_be_visible()


def _expire_on_next_poll(page):
    """The session ends: the next poll's snapshot read answers 401 (the test runs the
    paused clock to the poll). Returns the undo."""
    page.route(SNAPSHOT, lambda route: route.fulfill(
        status=401, content_type="application/json", body='{"error": "unauthorized"}'))
    with page.expect_response(SNAPSHOT):
        page.clock.run_for(5000)
    return lambda: page.unroute(SNAPSHOT)


def _expect_overlay(page):
    """The sign-in overlay is up, over a confirmation the shell still holds open, and its
    token field has focus."""
    expect(page.get_by_role("heading", name="Sign in to Photo Wall")).to_be_visible()
    expect(page.get_by_text("Your unsaved work is kept until you sign in again.")).to_be_visible()
    expect(page.get_by_role("banner")).to_be_hidden()
    token = page.get_by_label("Operator token", exact=True)
    expect(token).to_be_focused()
    return token


# --- The sign-in overlay over an open confirmation (item 1).


def test_a_poll_401_under_an_open_confirmation_signs_in_by_mouse_and_keeps_it(page, registry):
    identity, _key, _request = enroll(registry, count=1)
    player_id = identity["player_id"]
    handle = player_id[-6:]
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, paused_at=registry.clock.utc())
        player = open_player(page, player_name(registry, player_id))
        player.get_by_role("button", name=f"Retire player {player_id}", exact=True).click()
        dialog = page.get_by_role("dialog", name=f"Retire player {handle}?", exact=True)
        typed = dialog.get_by_label(f"Type {handle} to confirm", exact=True)
        typed.press_sequentially(handle[:3])

        restore = _expire_on_next_poll(page)
        token = _expect_overlay(page)
        restore()
        # By mouse: the token field takes a real click and real keys (a modal dialog in
        # the hidden shell would leave it inert).
        token.click()
        page.keyboard.type(ADMIN)
        expect(token).to_have_value(ADMIN)
        page.get_by_role("button", name="Sign in", exact=True).click()

        # The confirmation is where it was, with what was typed and focus back in it.
        expect(page.get_by_role("banner")).to_be_visible()
        expect(dialog).to_be_visible()
        expect(typed).to_have_value(handle[:3])
        expect(typed).to_be_focused()
        page.keyboard.type(handle[3:])
        dialog.get_by_role("button", name="Confirm retire", exact=True).click()
        expect(player).to_contain_text("Standing: Retired")


def test_a_write_401_from_a_confirmation_signs_in_by_keyboard_and_keeps_it(page, registry):
    _frame(registry, "first")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall", paused_at=registry.clock.utc())
        page.get_by_role("button", name="Frame first", exact=True).click()
        page.get_by_role("button", name="Delete frame first", exact=True).click()
        dialog = page.get_by_role("dialog", name="Delete frame first?", exact=True)
        expect(dialog).to_be_visible()

        # The session has ended server-side; the confirmed write discovers it.
        page.context.clear_cookies()
        dialog.get_by_role("button", name="Confirm delete", exact=True).click()
        _expect_overlay(page)
        # By keyboard: the field already has focus; type and press Enter.
        page.keyboard.type(ADMIN)
        page.keyboard.press("Enter")

        expect(page.get_by_role("banner")).to_be_visible()
        expect(page.get_by_role("heading", name="Sign in to Photo Wall")).to_be_hidden()
        # The confirmation is still open and usable, and focus is back inside it.
        expect(dialog).to_be_visible()
        assert page.evaluate(
            "document.activeElement.closest('dialog.confirm') !== null"), "focus left the dialog"
        dialog.get_by_role("button", name="Confirm delete", exact=True).click()
        expect(dialog).to_be_hidden()
        expect(page.get_by_role("button", name="Frame first", exact=True)).to_have_count(0)


# --- A Show page's confirmation when browser Back hides the page (item 2).


def _live_run_with_cancel_open(page):
    """Show the Scene now, open "Cancel run" on its Run from the Schedule page's history
    (Schedule, then Now showing), and return the Runs region and the open dialog."""
    author_scene(page, SCENE_ID, SOURCE, (VALID_FRAME,))
    go(page, "schedule")
    show_now(page, SCENE_ID, 0)
    runs = page.get_by_role("region", name="Runs", exact=True)
    runs.get_by_role("button", name=re.compile(r"^Cancel run ")).first.click()
    dialog = page.get_by_role("dialog", name=f"Cancel the Run of {SCENE_ID}?", exact=True)
    expect(dialog).to_be_visible()
    return runs, dialog


def test_browser_back_cancels_an_idle_show_confirmation_and_leaves_the_page_usable(
        page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        runs, dialog = _live_run_with_cancel_open(page)

        page.go_back()
        expect(_heading(page, "schedule")).to_be_visible()
        # Nothing invisible holds the page: no dialog is open, and the sidebar answers.
        assert page.evaluate("document.querySelector('dialog[open]')") is None
        go(page, "scenes")
        assert current_hash(page) == "#/scenes"

        # Back on Now showing the idle confirmation is gone (cancelled, as Esc would), and
        # the Run was not cancelled.
        go(page, "now")
        expect(page.get_by_role("dialog")).to_have_count(0)
        expect(runs.get_by_role("button", name=re.compile(r"^Cancel run ")).first).to_be_visible()


def test_a_show_confirmation_in_flight_when_back_hides_it_ends_as_usual(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        runs, dialog = _live_run_with_cancel_open(page)
        gate = RequestGate(page, "**/v1/operator/runs/*/cancel")
        gate.holding = True
        dialog.get_by_role("button", name="Confirm cancel", exact=True).click()
        gate.wait_held()

        page.go_back()
        expect(_heading(page, "schedule")).to_be_visible()
        assert page.evaluate("document.querySelector('dialog[open]')") is None
        go(page, "sources")

        gate.holding = False
        gate.release()
        go(page, "now")
        # The write finished while its page was hidden: the confirmation ended "done" and
        # the owner's status line says so; no dialog comes back.
        expect(runs.get_by_role("status").filter(
            has_text=f"Run of {SCENE_ID} cancelled.")).to_be_visible()
        expect(page.get_by_role("dialog")).to_have_count(0)


def test_a_show_confirmation_in_flight_comes_back_with_its_page(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        runs, dialog = _live_run_with_cancel_open(page)
        gate = RequestGate(page, "**/v1/operator/runs/*/cancel")
        gate.holding = True
        dialog.get_by_role("button", name="Confirm cancel", exact=True).click()
        gate.wait_held()

        page.go_back()
        expect(_heading(page, "schedule")).to_be_visible()
        assert page.evaluate("document.querySelector('dialog[open]')") is None
        # Still in flight on return: shown again, focused, and still not dismissible.
        page.go_forward()
        expect(_heading(page, "now")).to_be_visible()
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role("status")).to_have_text("Sending…")
        expect(dialog).to_be_focused()
        gate.holding = False
        gate.release()
        expect(dialog).to_be_hidden()
        expect(runs.get_by_role("status").filter(
            has_text=f"Run of {SCENE_ID} cancelled.")).to_be_visible()


def test_a_route_typed_before_the_first_snapshot_is_never_replaced(page, registry):
    _frame(registry, "first")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        # A fresh load at a known route: the landing route never applies.
        page.goto(origin + "/console#/players")
        expect(_heading(page, "players")).to_be_visible()
        page.wait_for_timeout(300)
        assert current_hash(page) == "#/players"
        visit(page, "#/nope")
        expect(_heading(page, "now")).to_be_visible()
        assert current_hash(page) == "#/now"


# --- The Surface follows a routed frame (item 4).


def test_a_typed_frame_route_shows_that_frames_surface(page, registry):
    _frame(registry, "a1", surface="A")
    _frame(registry, "b1", x_mm=500, surface="B")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        surface = page.get_by_role("combobox", name="Surface", exact=True)
        # Plain selection on Surface A remembers A.
        page.get_by_role("button", name="Frame a1", exact=True).click()
        expect(surface).to_have_value("A")
        go(page, "players")

        visit(page, "#/wall/frames/b1/binding")
        expect(page.get_by_role("region", name="Frame b1 inspector", exact=True)).to_be_visible()
        expect(surface).to_have_value("B")
        expect(page.get_by_role("button", name="Frame b1", exact=True)).to_be_visible()
        # Back to a1's route: its Surface again.
        page.go_back()
        expect(_heading(page, "players")).to_be_visible()
        page.go_back()
        expect(page.get_by_role("region", name="Frame a1 inspector", exact=True)).to_be_visible()
        expect(surface).to_have_value("A")


# --- Focus after "Show all" and a drawer link opened elsewhere (item 5).


def test_show_all_focuses_the_needs_attention_heading(page, registry):
    _frame(registry, "no-player")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "schedule")
        strip = page.get_by_role("region", name="Wall attention", exact=True)
        strip.get_by_role("button", name="Show frames", exact=True).click()
        strip.get_by_role("link", name="Show all", exact=True).click()
        expect(_heading(page, "attention")).to_be_focused()
        assert current_hash(page) == "#/attention"
        # Again from the page itself: the route does not change, and focus still moves.
        strip.get_by_role("button", name="Show frames", exact=True).click()
        strip.get_by_role("link", name="Show all", exact=True).click()
        expect(_heading(page, "attention")).to_be_focused()


def test_a_drawer_link_opened_in_another_tab_leaves_no_focus_request(page, registry):
    _frame(registry, "first")
    page.set_viewport_size(NARROW)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        menu = page.get_by_role("button", name="Menu", exact=True)
        menu.click()
        drawer = page.get_by_role("dialog", name="Menu", exact=True)
        new_tab_modifier = "Meta" if sys.platform == "darwin" else "Control"
        with page.context.expect_page() as other:
            drawer.get_by_role("link", name="Scenes", exact=True).click(
                modifiers=[new_tab_modifier])
        other.value.close()
        # This tab did not follow the link: still on Now showing, the drawer still open.
        assert current_hash(page) == "#/now"
        expect(drawer).to_be_visible()
        page.keyboard.press("Escape")
        expect(menu).to_be_focused()

        # Reaching Scenes later by a typed URL does not pull focus to its heading.
        visit(page, "#/scenes")
        expect(_heading(page, "scenes")).to_be_visible()
        page.wait_for_timeout(200)
        expect(_heading(page, "scenes")).not_to_be_focused()
        expect(menu).to_be_focused()


def test_the_snapshot_status_is_busy_exactly_while_a_read_is_in_flight(page, registry):
    """`aria-busy` on the snapshot status marks a Plane A read in flight and clears only once
    it is applied: the signal drive_poll waits on so back-to-back polls never race the
    poller's single-flight slot (a tick finding the previous poll unsettled is skipped)."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "now")
        status = page.get_by_role("group", name="Snapshot status", exact=True)
        expect(status).not_to_have_attribute("aria-busy", "true")
        gate = RequestGate(page, SNAPSHOT)
        gate.holding = True
        status.get_by_role("button", name="Refresh", exact=True).click()
        gate.wait_held()
        # The aggregate read is held, so the refresh is not settled yet.
        expect(status).to_have_attribute("aria-busy", "true")
        gate.holding = False
        gate.release()
        expect(status).not_to_have_attribute("aria-busy", "true")
