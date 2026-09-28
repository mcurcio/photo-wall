"""Behavioral checks for the console shell's review fixes (bead 1b review; flow design §6):
the landing route's race with a chosen section, the sign-in overlay over an open confirmation,
confirmations on Show pages that browser Back hides, the Surface filter under a typed frame
route, and where focus goes after "Show all" and a drawer link opened elsewhere.

Reuses the operator browser harness (real Chromium against the production create_app on an
ephemeral loopback listener, the disposable-schema `registry` fixture and the autouse
`page_errors` guard) and the task helpers of console_tasks.py.
"""

import os

import pytest
from console_tasks import LABELS, connect, visit
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect

from central.registry import FrameCreate
from contracts.models import FrameProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

PORTRAIT = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)


def _hash(page):
    return page.evaluate("window.location.hash")


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
        assert _hash(page) == "#/wall"
        expect(_heading(page, "wall")).to_be_visible()


def test_a_route_typed_before_the_first_snapshot_is_never_replaced(page, registry):
    _frame(registry, "first")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        # A fresh load at a known route: the landing route never applies.
        page.goto(origin + "/console#/equipment")
        expect(_heading(page, "equipment")).to_be_visible()
        page.wait_for_timeout(300)
        assert _hash(page) == "#/equipment"
        visit(page, "#/nope")
        expect(_heading(page, "now")).to_be_visible()
        assert _hash(page) == "#/now"
