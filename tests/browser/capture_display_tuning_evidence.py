"""Screenshots of display tuning (0024): the Frame page's Picture tab before and after, the only
console view the delivery changes.

Not a test (no `test_` prefix, so CI never collects it). Run it by path, against a built console
and the test database, as the browser tier runs (AGENTS.md Verify):

    PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q \
        tests/browser/capture_display_tuning_evidence.py --browser chromium -k after

`-k before` runs on a checkout of main with this file copied there: the `before` case imports
nothing main lacks. PNGs go to $PHOTO_WALL_EVIDENCE_DIR (default docs/evidence/display-tuning), one
per tab state and colour scheme, each the tab's own region. Seeding reuses the browser tests' own
helpers (test_frame_picture_browser.py), so every state shown here is one those tests prove.
"""

import os
from pathlib import Path

import pytest
from console_tasks import connect, open_frame
from operator_harness import operator_server
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OUT = Path(os.environ.get("PHOTO_WALL_EVIDENCE_DIR", "docs/evidence/display-tuning"))
SCHEMES = ["dark", "light"]


def _picture(page, registry, scheme, name, choose=None):
    from test_frame_picture_browser import FRAME, PRESENTED, FakePi, _bar, _seen

    _seen(registry)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        if choose:
            choose(page)
            expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        OUT.mkdir(parents=True, exist_ok=True)
        page.wait_for_timeout(400)
        page.get_by_role("region", name="Picture", exact=True).screenshot(
            path=str(OUT / f"{name}.{scheme}.png"), animations="disabled", caret="hide")


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_picture(page, registry, scheme):
    """Base: the Picture tab as main left it (no test pattern, no signal range)."""
    _picture(page, registry, scheme, "before-picture")


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_picture(page, registry, scheme):
    """The tab as it opens: Test pattern Off, Signal range Auto (what the Pi chooses)."""
    from test_frame_picture_browser import SIGNAL, _group

    def choose(page):
        expect(_group(page, SIGNAL)).to_contain_text("The Pi chooses: Limited for TV modes.")

    _picture(page, registry, scheme, "after-picture", choose)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_white(page, registry, scheme):
    """Test pattern White: the paper-sheet line, on every Frame of the display."""
    from test_frame_picture_browser import PAPER, TEST_PATTERN, _group, _show

    def choose(page):
        _show(page, "White")
        expect(_group(page, TEST_PATTERN)).to_contain_text(PAPER)

    _picture(page, registry, scheme, "after-white", choose)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_black_level_full(page, registry, scheme):
    """Test pattern Black level with Signal range Full applied by the Pi: the reading line and
    "The Pi sends Full."."""
    from test_frame_picture_browser import READING, SIGNAL, TEST_PATTERN, _choice, _group, _show

    def choose(page):
        _show(page, "Black level")
        expect(_group(page, TEST_PATTERN)).to_contain_text(READING)
        _choice(page, SIGNAL, "Full").click()
        expect(_group(page, SIGNAL)).to_contain_text("The Pi sends Full.")

    _picture(page, registry, scheme, "after-black-level-full", choose)
