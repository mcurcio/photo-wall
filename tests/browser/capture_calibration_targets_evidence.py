"""Screenshots of calibration targets (0026): the Frame page's Picture tab before and after, the one
console view the delivery changes (the Test pattern becomes a Select with the two screen targets and
the paper target's print links; a target's try names its stage in the History).

Not a test (no `test_` prefix, so CI never collects it). Run it by path, against a built console
and the test database, as the browser tier runs (AGENTS.md Verify):

    PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q \
        tests/browser/capture_calibration_targets_evidence.py --browser chromium -k after

`-k before` runs on a checkout of main with this file copied there: the `before` case imports
nothing main lacks. PNGs go to $PHOTO_WALL_EVIDENCE_DIR (default docs/evidence/calibration-targets),
one per state and colour scheme, each the whole page. Seeding and every step reuse the browser
tests' own helpers, so every state shown here is one those tests prove.
"""

import os
from pathlib import Path

import pytest
from console_tasks import connect, open_frame
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OUT = Path(os.environ.get("PHOTO_WALL_EVIDENCE_DIR", "docs/evidence/calibration-targets"))
SCHEMES = ["dark", "light"]


def _shot(page, scheme, name):
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / f"{name}.{scheme}.png"), full_page=True, animations="disabled", caret="hide")


def _picture(page, registry, scheme, name, steps):
    from operator_harness import operator_server
    from test_frame_picture_browser import PRESENTED, FakePi, _bar, _seen

    _seen(registry)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, "kitchen", "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        steps(page)
        _shot(page, scheme, name)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_white(page, registry, scheme):
    """Base: main's Test pattern (four choices) with White shown."""
    from test_frame_picture_browser import PAPER, TEST_PATTERN, _choice, _group

    def steps(page):
        _choice(page, TEST_PATTERN, "White").click()
        expect(_group(page, TEST_PATTERN)).to_contain_text(PAPER)

    _picture(page, registry, scheme, "before-white", steps)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_target_pre_software(page, registry, scheme):
    """Target: pre-software shown, its line and the paper target's print links; the try's Details
    in the History name the stage."""
    from test_frame_picture_browser import SHOWN, TEST_PATTERN, _group, _show
    from test_picture_history_browser import TRIED, _rows

    def steps(page):
        _show(page, "Target: pre-software")
        expect(_group(page, TEST_PATTERN)).to_contain_text(SHOWN)
        expect(_group(page, TEST_PATTERN).get_by_role("link", name="Letter", exact=True)).to_be_visible()
        tried = _rows(page, TRIED).first
        expect(tried).to_be_visible()
        tried.get_by_role("button", name="Details").click()
        expect(tried).to_contain_text("Target: pre-software")

    _picture(page, registry, scheme, "after-target-pre-software", steps)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_target_post_software(page, registry, scheme):
    """Target: post-software shown, its line and the paper target's print links; the try's row in
    the History names the stage (the entry the reader reads), waited for as the pre-software one."""
    from test_frame_picture_browser import SHOWN, TEST_PATTERN, _group, _show
    from test_picture_history_browser import TRIED, _rows

    def steps(page):
        _show(page, "Target: post-software")
        expect(_group(page, TEST_PATTERN)).to_contain_text(SHOWN)
        expect(_group(page, TEST_PATTERN).get_by_role("link", name="A4", exact=True)).to_be_visible()
        tried = _rows(page, TRIED).first
        expect(tried).to_be_visible()
        tried.get_by_role("button", name="Details").click()
        expect(tried).to_contain_text("Target: post-software")

    _picture(page, registry, scheme, "after-target-post-software", steps)
