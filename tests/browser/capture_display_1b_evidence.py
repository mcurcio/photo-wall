"""Before/after screenshots of the Frame page's Hardware and Power tabs for PR 76 (roadmap 1b).

Not a test (no `test_` prefix, so CI never collects it). Run it by path, against a built console
and the test database, as the browser tier runs (AGENTS.md Verify):

    PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q \
        tests/browser/capture_display_1b_evidence.py --browser chromium -k after

`-k before` runs on a checkout of main (copy this file there): only the `before` case imports
nothing that main lacks. PNGs go to $PHOTO_WALL_EVIDENCE_DIR (default docs/evidence/display-1b),
one per page state and colour scheme. Seeding reuses the browser tests' own helpers
(test_frame_display_changed_browser.py, test_frame_power_browser.py), so every state reached
here is one those tests prove.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from console_tasks import connect, open_frame
from operator_harness import operator_server
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OUT = Path(os.environ.get("PHOTO_WALL_EVIDENCE_DIR", "docs/evidence/display-1b"))
SCHEMES = ["dark", "light"]


def _shot(page, name: str, scheme: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(300)
    page.screenshot(path=str(OUT / f"{name}.{scheme}.png"), full_page=True, animations="disabled", caret="hide")


def _off_thread(call, *args, **kwargs):
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(call, *args, **kwargs).result()


def _open(page, scheme):
    page.set_viewport_size({"width": 1280, "height": 900})
    page.emulate_media(color_scheme=scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_before_hardware(page, registry, scheme):
    """main: a Frame bound to a Pi's HDMI-A-1 (the Hardware tab as it was; main has no Power tab)."""
    from test_registry import enroll

    from central.registry import FrameCreate
    from contracts.models import FrameProfile

    identity, _key, _request = enroll(registry, count=1, device_id="device-display-1b-pi")
    registry.create_frame(FrameCreate(id="lobby", surface_id="wall", x_mm=100, y_mm=100, width_mm=500,
                                      height_mm=300,
                                      profile=FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)))
    registry.bind("lobby", identity["player_id"], "HDMI-A-1", expected_generation=0)
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, "lobby", "hardware")
        _shot(page, "before-hardware", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_hardware(page, registry, scheme):
    """The Display section, a no-serial display, and the display-changed card."""
    from test_display_identity import NO_SERIAL, SERIAL_DISPLAY, _bind, enroll_pi
    from test_frame_display_changed_browser import (
        CARD,
        OTHER_DISPLAY,
        _commit_position,
        _landscape,
        _report,
    )

    from central.registry import FrameCreate
    from contracts.models import FrameProfile

    pi = enroll_pi(registry, "pi-a")
    # The ready Frame's profile matches its display (1920 x 1080), as main's `before` Frame does.
    ready = "lobby"
    registry.create_frame(FrameCreate(id=ready, surface_id="wall", x_mm=100, y_mm=100, width_mm=500, height_mm=300,
                                      profile=FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)))
    changed, no_serial = (_landscape(registry, name) for name in ("hallway", "kitchen"))
    _bind(registry, ready, pi, "HDMI-A-1")
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    _commit_position(registry, ready)
    _bind(registry, changed, pi, "HDMI-A-2")
    _report(registry, "pi-a", "HDMI-A-2", SERIAL_DISPLAY)
    _commit_position(registry, changed)
    _report(registry, "pi-a", "HDMI-A-2", OTHER_DISPLAY)
    other = enroll_pi(registry, "pi-b")
    _bind(registry, no_serial, other, "HDMI-A-1")
    _report(registry, "pi-b", "HDMI-A-1", NO_SERIAL)

    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        frame = open_frame(page, ready, "hardware")
        expect(frame.get_by_role("region", name="Display", exact=True)).to_contain_text("CN0F1X7K")
        _shot(page, "after-hardware", scheme)

        frame = open_frame(page, changed, "hardware")
        expect(frame.get_by_role("group", name=CARD, exact=True)).to_contain_text("DELL U2720Q")
        _shot(page, "after-hardware-display-changed", scheme)

        frame = open_frame(page, no_serial, "hardware")
        expect(frame.get_by_role("region", name="Display", exact=True)).to_contain_text(
            "Recognised by make and model on this Frame")
        _shot(page, "after-hardware-no-serial", scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_after_power(page, registry, scheme):
    """The Power tab: ready, waiting, confirmed off, confirmed on, didn't answer, and unbound."""
    from test_display_identity import _frame
    from test_display_power_requests import _rows
    from test_frame_power_browser import WAITING, _change, _power, _reported, _seen_frame

    from contracts.node_output import InForce, PowerResult

    frame = _seen_frame(registry)
    unbound = _frame(registry, "hallway")
    _open(page, scheme)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        power = _power(open_frame(page, frame, "power"))
        expect(power.get_by_role("button", name="Test: turn off", exact=True)).to_be_visible()
        _shot(page, "after-power", scheme)

        power.get_by_role("button", name="Test: turn off", exact=True).click()
        status = power.get_by_role("status")
        expect(status).to_have_text(WAITING)
        _shot(page, "after-power-waiting", scheme)

        test_id = _rows(registry, "SELECT id FROM power_requests")[0]["id"]
        _reported(registry, _change(registry), PowerResult.CONFIRMED, InForce(str(test_id), 299))
        expect(status).to_have_text("Display confirmed off")
        _shot(page, "after-power-confirmed-off", scheme)

        power.get_by_role("button", name="Turn on", exact=True).click()
        expect(status).to_have_text(WAITING)
        _reported(registry, _change(registry), PowerResult.CONFIRMED, InForce("standing", None))
        expect(status).to_have_text("Display confirmed on")
        _shot(page, "after-power-confirmed-on", scheme)

        power.get_by_role("button", name="Test: turn off", exact=True).click()
        expect(status).to_have_text(WAITING)
        test_id = _rows(registry, "SELECT id FROM power_requests")[0]["id"]
        _reported(registry, _change(registry), PowerResult.DID_NOT_ANSWER, InForce(str(test_id), 299))
        expect(status).to_have_text("Display didn't answer")
        _shot(page, "after-power-did-not-answer", scheme)

        power = _power(open_frame(page, unbound, "power"))
        expect(power).to_contain_text("Choose which Pi and HDMI port feeds this Frame")
        _shot(page, "after-power-unbound", scheme)
