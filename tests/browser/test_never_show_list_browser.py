"""Scenes › Never show list in a real browser (decision 0036, slice 3; setting #71).

Real Chromium against the production create_app (the operator browser harness), over a wall whose
photos were listed through the real Coordinator (tests/test_skip_never_show.py's `Wall`). The list
is Central's: every row the page draws is a photo Central holds listed, and Show again and its
Undo change Central's list, read back from Central, never from the page.

Mutation probes (run and restored): send Show again for another row's photo -> the test fails on
Central's list; offer no Undo after Show again -> the test fails.
"""

import os
import re

import pytest
from console_tasks import go
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect
from test_skip_never_show import PHOTOS, Wall

from central.everyday.policy import photo_of

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="browser tests are opt-in (PHOTO_WALL_BROWSER_TESTS=1)",
)

SOON = 15_000                       # ms: the list is read again after each change, and every 10 s


def _listed(wall):
    return {entry.asset_id for entry in wall.coordinator.never_show_entries().photos}


def test_show_again_takes_a_photo_off_the_list(page, registry):
    """Two listed photos, two rows, newest first; Show again on the newest takes it (and only it)
    off Central's list, said in a toast with Undo; Undo lists it again."""
    with operator_server(registry.db, registry.clock) as origin:
        wall = Wall(registry, {"hall": "wall"})
        shown = photo_of(wall.showing("hall"))
        older, newer = [item.asset_id for item in PHOTOS if item.asset_id != shown][:2]
        wall.never_show(older, request_id="never-1")
        registry.clock.advance(60)
        wall.never_show(newer, request_id="never-2")
        assert _listed(wall) == {older, newer}
        sign_in(page, origin)
        go(page, "never-show")
        section = page.get_by_role("main").locator("section:not([hidden])")
        again = section.get_by_role("button", name=re.compile(r"^Show again: "))
        expect(again).to_have_count(2, timeout=SOON)
        with page.expect_response(lambda r: r.request.method == "DELETE" and "never-show" in r.url) as info:
            again.first.click()
        assert info.value.ok, info.value.text()
        assert newer in info.value.url and older not in info.value.url, info.value.url
        toast = section.locator("[role=status].fixed")
        expect(toast).to_contain_text("It shows again.")
        expect(again).to_have_count(1, timeout=SOON)
        assert _listed(wall) == {older}
        # Undo lists it again.
        toast.get_by_role("button", name="Undo").click()
        expect(section.locator("[role=status].fixed")).to_contain_text("Won't show again.")
        expect(again).to_have_count(2, timeout=SOON)
        assert _listed(wall) == {older, newer}
