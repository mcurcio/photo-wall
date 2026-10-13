"""Screenshots of configuration snapshots: the Frame page's Picture tab before and after its History.

Not a test (no `test_` prefix, so CI never collects it). Run it by path, against a built console
and the test database, as the browser tier runs (AGENTS.md Verify):

    PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q \
        tests/browser/capture_config_snapshots_evidence.py --browser chromium -k after

`-k before` runs on a checkout of main with this file copied there: the `before` case imports
nothing main lacks. PNGs go to $PHOTO_WALL_EVIDENCE_DIR (default docs/evidence/config-snapshots), one
per state and colour scheme. Seeding reuses the browser tests' own helpers
(test_frame_picture_browser.py, test_picture_history_browser.py), so every state shown here is one
those tests prove.
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

OUT = Path(os.environ.get("PHOTO_WALL_EVIDENCE_DIR", "docs/evidence/config-snapshots"))
SCHEMES = ["dark", "light"]


def _shoot(locator, name, scheme):
    OUT.mkdir(parents=True, exist_ok=True)
    locator.page.wait_for_timeout(400)
    locator.screenshot(path=str(OUT / f"{name}.{scheme}.png"), animations="disabled", caret="hide")


def _picture(page, registry, scheme, act=None):
    """The Picture tab once the Pi presents; `act(page)` then returns what else to shoot."""
    from test_frame_picture_browser import FRAME, PRESENTED, FakePi, _bar, _seen

    _seen(registry)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        shots = act(page) if act else {"tab": "before-picture-tab"}
        _shoot(page.get_by_role("tabpanel", name="Picture"), shots.pop("tab"), scheme)
        for name, locator in shots.items():
            _shoot(locator, name, scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_picture(page, registry, scheme):
    """Base: the Picture tab as main left it (no History)."""
    _picture(page, registry, scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_picture_history(page, registry, scheme):
    """A change kept by Done, then one tried and not kept: the tab with its History, and the History
    with a kept row's Details open."""
    from test_frame_picture_browser import PHOTO_WALL, _press, _slider
    from test_picture_history_browser import KEPT, TRIED, _done, _history, _rows

    def act(page):
        _press(page, PHOTO_WALL, "Brightness", "ArrowRight", 3)
        _done(page)
        expect(_rows(page, KEPT)).to_have_count(1)
        expect(_slider(page, PHOTO_WALL, "Brightness")).to_be_enabled()  # the next session has begun
        _press(page, PHOTO_WALL, "Brightness", "ArrowRight", 5)
        expect(_rows(page, TRIED).first).to_contain_text("Brightness 108 %")
        _rows(page, KEPT).first.get_by_role("button", name="Details").click()
        return {"tab": "after-picture-tab", "after-picture-history": _history(page)}

    _picture(page, registry, scheme, act)
