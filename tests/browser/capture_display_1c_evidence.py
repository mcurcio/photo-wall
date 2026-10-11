"""Before/after screenshots of the Frame page's Position and Picture tabs for PR 79 (roadmap 1c).

Not a test (no `test_` prefix, so CI never collects it). Run it by path, against a built console
and the test database, as the browser tier runs (AGENTS.md Verify):

    PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q \
        tests/browser/capture_display_1c_evidence.py --browser chromium -k after

`-k before` runs on a checkout of main (copy this file there): every import sits inside its case,
and the `before` cases use only helpers main has. PNGs go to $PHOTO_WALL_EVIDENCE_DIR (default
docs/evidence/display-1c), one per page state and colour scheme. Seeding reuses the browser tests'
own helpers (test_console_frame_page_browser.py's fake Central for Position,
test_frame_picture_browser.py's fake Pi for Picture), so every state reached here is one those
tests prove.
"""

import os
from pathlib import Path

import pytest
from console_tasks import open_frame
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OUT = Path(os.environ.get("PHOTO_WALL_EVIDENCE_DIR", "docs/evidence/display-1c"))
SCHEMES = ["dark", "light"]


def _shot(page, name: str, scheme: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(300)
    page.screenshot(path=str(OUT / f"{name}.{scheme}.png"), full_page=True, animations="disabled", caret="hide")


def _open(page, scheme):
    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)


def _region(page, name):
    return page.get_by_role("region", name=name, exact=True)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_position_and_picture(page, registry, scheme):
    """main: the Position tab's corners (and its Exact values, trims included) and the Picture
    tab's one brightness slider, both on the shared live adjustment."""
    from test_console_frame_page_browser import FRAME, PRESENTED, FakeSessions, _bar, _seed

    _seed(registry)
    FakeSessions(page, registry)
    _open(page, scheme)
    try:
        with operator_server(registry.db, registry.clock) as origin:
            sign_in(page, origin)
            open_frame(page, FRAME, "position")
            expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
            _shot(page, "before-position", scheme)
            page.get_by_role("button", name="Exact values", exact=True).click()
            _shot(page, "before-position-exact", scheme)

            open_frame(page, FRAME, "picture")
            expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
            _shot(page, "before-picture", scheme)
    finally:
        page.unroute_all(behavior="ignoreErrors")


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_position(page, registry, scheme):
    """Cover with a 5 degree angle (the default fit), its Exact values (no trims), then Corners
    chosen as the opt-in."""
    from test_console_frame_page_browser import FRAME, PRESENTED, FakeSessions, _bar, _seed

    _seed(registry)
    FakeSessions(page, registry)
    _open(page, scheme)
    try:
        with operator_server(registry.db, registry.clock) as origin:
            sign_in(page, origin)
            position = _region(open_frame(page, FRAME, "position"), "Position")
            bar = _bar(page)
            expect(bar.get_by_role("status")).to_have_text(PRESENTED)
            expect(position.get_by_role("radio", name="Cover", exact=True)).to_be_checked()
            position.get_by_label("Angle in degrees", exact=True).fill("5")
            expect(position.get_by_role("slider", name="Angle", exact=True)).to_have_value("5")
            expect(bar.get_by_role("status")).to_have_text(PRESENTED)
            _shot(page, "after-position-cover", scheme)
            position.get_by_role("button", name="Exact values", exact=True).click()
            _shot(page, "after-position-cover-exact", scheme)

            position.get_by_role("radio", name="Corners", exact=True).click()
            expect(position).to_contain_text("Stretch the picture exactly onto the corners")
            expect(bar.get_by_role("status")).to_have_text(PRESENTED)
            _shot(page, "after-position-corners", scheme)
    finally:
        page.unroute_all(behavior="ignoreErrors")


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_picture(page, registry, scheme):
    """The four sliders: brightness on the display, contrast a Photo Wall picture adjustment; then
    the Copy dialog with two other Frames chosen."""
    from test_frame_picture_browser import FRAME, PRESENTED, FakePi, _bar, _row, _seen, _store

    player = _seen(registry)
    _seen(registry, "hall", output_id="HDMI-A-2", player=player)
    _seen(registry, "stairs", serial="pi-stairs")
    _store(registry, brightness=70, contrast=45, colour_temperature_kelvin=5500, gamma_tenths=22)
    _open(page, scheme)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        picture = _region(open_frame(page, FRAME, "picture"), "Picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(_row(page, "Brightness")).to_contain_text("On the display · 70 %")
        expect(_row(page, "Contrast")).to_contain_text("Photo Wall picture adjustment · 45 %")
        _shot(page, "after-picture", scheme)

        picture.get_by_role("button", name="Copy to other Frames", exact=True).click()
        picture.get_by_role("switch", name="Frame hall", exact=True).click()
        picture.get_by_role("switch", name="Frame stairs", exact=True).click()
        expect(picture.get_by_role("button", name="Copy to 2 Frames", exact=True)).to_be_enabled()
        _shot(page, "after-picture-copy", scheme)
