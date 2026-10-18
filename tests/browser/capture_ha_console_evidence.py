"""Screenshots of the Home Assistant integration's console views (PR #106): Fleet › Releases with a
version per Player and Install, Settings › Home Assistant with States last heard, and the House ›
Lights picker.

Not a test (no `test_` prefix, so CI never collects it). Run it by path, against a built console
and the test database, as the browser tier runs (AGENTS.md Verify):

    PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q \\
        tests/browser/capture_ha_console_evidence.py --browser chromium

PNGs go to $PHOTO_WALL_EVIDENCE_DIR (default docs/evidence/home-assistant-console), one per view
and colour scheme. Seeding reuses the browser tests' own fakes (test_releases_browser.py,
test_house_lights_browser.py), so every state shown here is one those tests prove.
"""

import os
from pathlib import Path

import pytest
from console_tasks import connect, go
from operator_harness import operator_server
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OUT = Path(os.environ.get("PHOTO_WALL_EVIDENCE_DIR", "docs/evidence/home-assistant-console"))
SCHEMES = ["dark", "light"]


def _shot(page, name, scheme):
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / f"{name}.{scheme}.png"), full_page=True, animations="disabled", caret="hide")


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_releases(page, registry, scheme):
    from test_releases_browser import PAUSED_AT, Releases

    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)
    with operator_server(registry.db, registry.clock) as origin:
        releases = Releases(page)
        releases.view["players"][2]["install"] = "downloading"
        connect(page, origin, "releases", paused_at=PAUSED_AT)
        expect(page.get_by_role("list", name="Players", exact=True)).to_be_visible()
        _shot(page, "after-releases", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_home_assistant(page, registry, scheme):
    from test_house_lights_browser import SPOT, WINDOW_METER, _states, put, signed_in

    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)
    with operator_server(registry.db, registry.clock) as origin:
        _states(page)
        signed_in(page, origin)
        put(page, origin, "/v1/operator/house/sensors/window-meter", WINDOW_METER)
        put(page, origin, "/v1/operator/house/lights/bookshelf-spot", SPOT)
        go(page, "home-assistant")
        expect(page.get_by_text("States last heard:")).to_be_visible()
        _shot(page, "after-home-assistant", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_lights_picker(page, registry, scheme):
    from test_house_lights_browser import _dialog, _states, signed_in

    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)
    with operator_server(registry.db, registry.clock) as origin:
        _states(page)
        signed_in(page, origin)
        go(page, "lights")
        page.get_by_role("button", name="Add a room light", exact=True).click()
        dialog = _dialog(page, "Add a room light")
        dialog.get_by_role("textbox", name="Name", exact=True).fill("Kitchen")
        dialog.get_by_role("radio", name="Reports itself", exact=True).click()
        expect(dialog.get_by_role("combobox", name="Smart light", exact=True)).to_be_visible()
        _shot(page, "after-lights-picker", scheme)
