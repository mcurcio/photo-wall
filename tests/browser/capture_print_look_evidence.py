"""Screenshots of the print look (0023): the Frame page's Picture and Position tabs before and
after, and Settings › LUTs after (a new page, so no before).

Not a test (no `test_` prefix, so CI never collects it). Run it by path, against a built console
and the test database, as the browser tier runs (AGENTS.md Verify):

    PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q \
        tests/browser/capture_print_look_evidence.py --browser chromium -k after

`-k before` runs on a checkout of the base branch (feat/picture-hardware-and-software, PR 94) with
this file copied there: the `before` cases import nothing that branch lacks. PNGs go to
$PHOTO_WALL_EVIDENCE_DIR (default docs/evidence/print-look), one per tab state and colour scheme,
each the tab's own region (Settings › LUTs: the whole view). Seeding reuses the browser tests' own helpers
(test_frame_picture_browser.py, test_console_frame_page_browser.py, test_frame_mat_browser.py),
so every state shown here is one those tests prove.
"""

import os
from pathlib import Path

import pytest
from console_tasks import connect, go, open_frame
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OUT = Path(os.environ.get("PHOTO_WALL_EVIDENCE_DIR", "docs/evidence/print-look"))
SCHEMES = ["dark", "light"]


def _open(page, scheme):
    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)


def _shot(page, region: str, name: str, scheme: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(400)
    page.get_by_role("region", name=region, exact=True).screenshot(
        path=str(OUT / f"{name}.{scheme}.png"), animations="disabled", caret="hide")


def _picture(page, registry, scheme, name, choose=None):
    from test_frame_picture_browser import FRAME, PRESENTED, FakePi, _bar, _seen

    _seen(registry)
    _open(page, scheme)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        if choose:
            choose(page)
            expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        _shot(page, "Picture", name, scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_picture(page, registry, scheme):
    """Base: the Photo Wall (software) section as PR 94 left it."""
    _picture(page, registry, scheme, "before-picture")


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_picture(page, registry, scheme):
    """The print sliders and a LUT chosen (its amount showing), presented by the Pi."""
    from test_frame_picture_browser import PHOTO_WALL, _group, _slider
    from test_print_look_luts import _as_printer, _monitor

    from central.infra.lut_store import PgLuts

    paper, _ = PgLuts(registry.db, registry.clock).add_lut(_as_printer(_monitor()))

    def choose(page):
        _group(page, PHOTO_WALL).get_by_role("combobox", name="LUT", exact=True).select_option(
            value=paper.profile_sha256)
        expect(_slider(page, PHOTO_WALL, "LUT amount")).to_be_visible()
        _slider(page, PHOTO_WALL, "Saturation").fill("90")
        _slider(page, PHOTO_WALL, "Black floor").fill("2")

    _picture(page, registry, scheme, "after-picture", choose)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_position(page, registry, scheme):
    """Base: the Position tab as PR 94 left it (no mat)."""
    from test_console_frame_page_browser import FRAME, PRESENTED, FakeSessions, _bar, _seed

    _seed(registry)
    FakeSessions(page, registry)
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "position")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        _shot(page, "Position", "before-position", scheme)
    page.unroute_all(behavior="ignoreErrors")


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_position(page, registry, scheme):
    """The Frame's mat: 2.5 mm deep, a cool light from 3 o'clock and a warm one from the top."""
    from test_console_frame_page_browser import FRAME, PRESENTED, _bar, _seed
    from test_frame_mat_browser import MatSessions, _light, _mat

    _seed(registry)
    MatSessions(page, registry)
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "position")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        mat = _mat(page)
        mat.get_by_role("slider", name="Depth", exact=True).fill("2.5")
        add = mat.get_by_role("button", name="Add a light", exact=True)
        add.click()
        _light(page, 1).get_by_role("slider", name="Comes from", exact=True).fill("90")
        _light(page, 1).get_by_role("slider", name="Colour", exact=True).fill("7500")
        add.click()
        _light(page, 2).get_by_role("slider", name="Colour", exact=True).fill("2700")
        expect(_light(page, 2)).to_be_visible()
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        _shot(page, "Position", "after-position", scheme)
    page.unroute_all(behavior="ignoreErrors")


def _luts_shot(page, name: str, scheme: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / f"{name}.{scheme}.png"), animations="disabled", caret="hide")


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_luts_empty(page, registry, scheme):
    """Settings › LUTs before any paper profile is added (new page: no before)."""
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "luts")
        expect(page.get_by_text("Add a paper profile from your paper's maker.", exact=True)).to_be_visible()
        _luts_shot(page, "after-luts-empty", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_luts(page, registry, scheme):
    """Settings › LUTs with one paper's LUT, chosen on a Display's Picture tab and kept with Done,
    so the list names the Display that uses it (new page: no before)."""
    from test_frame_picture_browser import (
        FRAME,
        PHOTO_WALL,
        PRESENTED,
        FakePi,
        _bar,
        _group,
        _off_thread,
        _seen,
    )
    from test_print_look_luts import _as_printer, _monitor

    from central.infra.lut_store import PgLuts
    from contracts.time import ManualClock

    _seen(registry)
    added = ManualClock(1_791_797_400)  # 12 Oct 2026, so Added shows a real day, not the rig's 1970
    paper, _ = PgLuts(registry.db, added).add_lut(_as_printer(_monitor()))
    _open(page, scheme)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        _group(page, PHOTO_WALL).get_by_role("combobox", name="LUT", exact=True).select_option(
            value=paper.profile_sha256)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        bar.get_by_role("button", name="Done", exact=True).click()
        expect(page.get_by_role("region", name="Picture", exact=True).get_by_role("status").filter(
            has_text="Saved at")).to_be_visible()
        [display] = _off_thread(lambda: PgLuts(registry.db, registry.clock).list_luts()).luts[0].used_by
        go(page, "luts")
        expect(page.get_by_role("table", name="On the list", exact=True)).to_contain_text(display)
        _luts_shot(page, "after-luts", scheme)
