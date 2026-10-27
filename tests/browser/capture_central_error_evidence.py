"""Screenshots of Central's error in the console: Needs attention's room lights in doubt, House ›
Lights' "Can't read since …" and "Link closed since …", and the Frame page's "Central's error".

Not a test (no `test_` prefix, so CI never collects it). Run it by path, against a built console and
the test database, as the browser tier runs (AGENTS.md Verify):

    PHOTO_WALL_BROWSER_TESTS=1 PHOTO_WALL_EVIDENCE_PHASE=after .venv/bin/python scripts/test_local.py -q \\
        tests/browser/capture_central_error_evidence.py --browser chromium

PNGs go to $PHOTO_WALL_EVIDENCE_DIR (default docs/evidence/central-error-console), one per view, phase
and colour scheme. PHOTO_WALL_EVIDENCE_PHASE=before serves the house's lights as Central served them
before this delivery (no doubt: tonight's light just "Off"), for a console built from the base branch.
The seeding and served shapes are test_central_error_console_browser.py's, so every state shown here
is one those tests prove.
"""

import os
from pathlib import Path

import pytest
from console_tasks import connect, open_frame
from operator_harness import operator_server
from playwright.sync_api import expect
from test_central_error_console_browser import FRAME, _serve, lights_view
from test_console_frame_page_browser import _seed

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OUT = Path(os.environ.get("PHOTO_WALL_EVIDENCE_DIR", "docs/evidence/central-error-console"))
PHASE = os.environ.get("PHOTO_WALL_EVIDENCE_PHASE", "after")
SCHEMES = ["dark", "light"]


def _shot(page, name, scheme):
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(600)
    page.screenshot(path=str(OUT / f"{PHASE}-{name}.{scheme}.png"), full_page=True, animations="disabled", caret="hide")


def _open(page, registry, scheme):
    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)
    _seed(registry)
    if PHASE == "before":
        _serve(page, lights=lights_view(doubts=False, shape="cant_tell_since"))
    else:
        _serve(page)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_needs_attention(page, registry, scheme):
    _open(page, registry, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "attention")
        expect(page.get_by_role("heading", name="Needs attention", level=1)).to_be_visible()
        _shot(page, "needs-attention", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_house_lights(page, registry, scheme):
    _open(page, registry, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "lights")
        expect(page.get_by_role("table", name="In the house", exact=True)).to_be_visible()
        _shot(page, "house-lights", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_frame_overview(page, registry, scheme):
    _open(page, registry, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        open_frame(page, FRAME, "overview")
        _shot(page, "frame-overview", scheme)
