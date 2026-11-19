"""Screenshots of the Pi's Reboot section: the reboot dialog and the reboot history after a send.

Not a test (no `test_` prefix, so CI never collects it). Run it by path, against a built console and
the test database, as the browser tier runs (AGENTS.md Verify):

    PHOTO_WALL_BROWSER_TESTS=1 PHOTO_WALL_EVIDENCE_PHASE=after .venv/bin/python scripts/test_local.py -q \\
        tests/browser/capture_reboot_evidence.py --browser chromium

PNGs go to $PHOTO_WALL_EVIDENCE_DIR (default docs/evidence/reboot-console), one per view, phase and
colour scheme. The flow drives the console only (open the dialog, send, read the history), so the same
script captures a console built from the base branch (PHASE=before) and from this one. The seeding is
test_player_page_browser.py's.
"""

import os
from pathlib import Path

import pytest
from console_tasks import connect, open_pi
from playwright.sync_api import expect
from support.frames import frame_create
from test_player_page_browser import NAME, Box, _node_server, _reboot

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OUT = Path(os.environ.get("PHOTO_WALL_EVIDENCE_DIR", "docs/evidence/reboot-console"))
PHASE = os.environ.get("PHOTO_WALL_EVIDENCE_PHASE", "after")
SCHEMES = ["dark", "light"]


def _shot(page, target, name, scheme):
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(600)
    target.screenshot(path=str(OUT / f"{PHASE}-{name}.{scheme}.png"), animations="disabled", caret="hide")


@pytest.mark.parametrize("scheme", SCHEMES)
def test_reboot_dialog_and_history(page, registry, scheme):
    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)
    box = Box(registry)
    registry.create_frame(frame_create("lobby", x_mm=100, y_mm=100), by="console")
    registry.bind("lobby", box.player_id, "HDMI-A-1", expected_generation=0, by="console")
    with _node_server(registry) as origin:
        connect(page, origin)
        open_pi(page, NAME)
        _reboot(page).get_by_role("button", name="Reboot Player", exact=True).click()
        dialog = page.get_by_role("dialog", name=f"Reboot {NAME}?", exact=True)
        expect(dialog).to_contain_text("Frame lobby: no live Run")
        dialog.get_by_text("Request", exact=True).click()   # unfold the request's details
        _shot(page, page, "reboot-dialog", scheme)
        dialog.get_by_role("button", name="Reboot Player", exact=True).click()
        expect(dialog).to_have_count(0)
        history = _reboot(page).get_by_role("list", name="Reboot history", exact=True)
        expect(history).to_be_visible()
        history.get_by_text("Request …", exact=False).first.click()   # unfold the entry's details
        _shot(page, _reboot(page), "reboot-history", scheme)
