"""Screenshots of configuration snapshots (0025): the Frame page's Picture and Position tabs before
and after, the two console views the delivery changes (each gains its History).

Not a test (no `test_` prefix, so CI never collects it). Run it by path, against a built console
and the test database, as the browser tier runs (AGENTS.md Verify):

    PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q \
        tests/browser/capture_configuration_snapshots_evidence.py --browser chromium -k after

`-k before` runs on a checkout of main with this file copied there: the `before` cases import
nothing main lacks. PNGs go to $PHOTO_WALL_EVIDENCE_DIR (default
docs/evidence/configuration-snapshots), one per tab and colour scheme, each the whole page. Seeding
and every step reuse the browser tests' own helpers, so every state shown here is one those tests
prove.
"""

import os
from pathlib import Path

import pytest
from console_tasks import connect, open_frame
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OUT = Path(os.environ.get("PHOTO_WALL_EVIDENCE_DIR", "docs/evidence/configuration-snapshots"))
SCHEMES = ["dark", "light"]


def _shot(page, scheme, name):
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / f"{name}.{scheme}.png"), full_page=True, animations="disabled", caret="hide")


def _picture(page, registry, scheme, name, steps=None):
    from test_frame_picture_browser import PRESENTED, FakePi, _bar, _seen

    _seen(registry)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, "kitchen", "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        if steps:
            steps(page)
        _shot(page, scheme, name)


def _position(page, registry, scheme, name, sessions_class, steps=None):
    from test_console_frame_page_browser import FRAME, PRESENTED, _bar, _seed

    _seed(registry)
    sessions_class(page, registry)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)
    try:
        with operator_server(registry.db, registry.clock) as origin:
            sign_in(page, origin)
            open_frame(page, FRAME, "position")
            expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
            if steps:
                steps(page)
            _shot(page, scheme, name)
    finally:
        page.unroute_all(behavior="ignoreErrors")


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_picture(page, registry, scheme):
    """Base: the Picture tab as main left it (no History)."""
    _picture(page, registry, scheme, "before-picture")


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_position(page, registry, scheme):
    """Base: the Position tab as main left it (no History)."""
    from test_frame_mat_browser import MatSessions

    _position(page, registry, scheme, "before-position", MatSessions)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_picture(page, registry, scheme):
    """The Picture tab's History: one change kept by Done, then one tried and not kept, its Details open."""
    from test_frame_picture_browser import PHOTO_WALL, _press, _slider
    from test_picture_history_browser import KEPT, TRIED, _done, _rows

    def steps(page):
        _press(page, PHOTO_WALL, "Brightness", "ArrowRight", 3)
        _done(page)
        expect(_rows(page, KEPT)).to_have_count(1)
        expect(_slider(page, PHOTO_WALL, "Brightness")).to_be_enabled()  # the next session has begun
        _press(page, PHOTO_WALL, "Brightness", "ArrowRight", 5)
        expect(_rows(page, TRIED).first).to_contain_text("Brightness 108 %")
        _rows(page, KEPT).first.get_by_role("button", name="Details").click()

    _picture(page, registry, scheme, "after-picture", steps)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_position(page, registry, scheme):
    """The Position tab's History: the seeded kept Position, a moved corner kept by Done, then a
    deeper mat tried and not kept."""
    from test_console_frame_page_browser import _move
    from test_position_history_browser import KEPT, TRIED, HistorySessions, _depth, _done, _rows

    def steps(page):
        _move(page, 2)
        _done(page)
        expect(_rows(page, KEPT)).to_have_count(2)
        _depth(page).fill("6")
        expect(_rows(page, TRIED).first).to_contain_text("Mat depth 6.0 mm")

    _position(page, registry, scheme, "after-position", HistorySessions, steps)
