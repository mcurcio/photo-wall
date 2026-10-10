"""The Frame page's Hardware tab shows its Display and, when another display appears on its HDMI port,
the display-changed card (roadmap 1b, slice K1; design-language §8 row G5; console design §5, §6).

Every report travels the real path (`test_display_identity._report`: the link store's commit and its
Output report judge); bindings and Position commits go through the Registry. The Position tab's own
Done flow is proven in test_console_frame_page_browser.py, so here the commit it makes is made by the
Registry directly, after the card has sent the operator there.

Mutation probes: show the card on `position-needed` too -> the unchanged-Frame case fails; save the
Frame's old size instead of the new display's preferred mode -> the confirm assertion fails.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from console_tasks import connect, current_hash, open_frame
from operator_harness import operator_server
from playwright.sync_api import expect
from test_display_identity import NO_SERIAL, SERIAL_DISPLAY, _bind, _frame, enroll_pi
from test_display_identity import _report as _recorded

from central.displays.model import Readiness
from central.registry import FrameCreate
from contracts.models import Calibration, FrameProfile
from contracts.node_output import DisplayIdentity

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

OTHER_DISPLAY = DisplayIdentity("DEL", 41200, "DELL U2720Q", "CN0F1X8Z")
CARD = "Display changed"


def _landscape(registry, frame_id: str) -> str:
    """A landscape Frame declared at 1280 x 720; the reported displays prefer 1920 x 1080."""
    registry.create_frame(FrameCreate(id=frame_id, surface_id="wall", x_mm=100, y_mm=100, width_mm=500,
                                      height_mm=300,
                                      profile=FrameProfile(width_px=1280, height_px=720, diagonal_inches=24)))
    return frame_id


def _report(*args, **kwargs) -> None:
    """`test_display_identity._report` off Playwright's thread, whose event loop is running."""
    with ThreadPoolExecutor(1) as pool:
        pool.submit(_recorded, *args, **kwargs).result()


def _row(registry, frame_id: str):
    return next(frame for frame in registry.inventory().frames if frame.id == frame_id)


def _commit_position(registry, frame_id: str) -> None:
    """The Position tab's Done (`Registry.calibrate` commit)."""
    frame = _row(registry, frame_id)
    registry.calibrate(frame_id, "commit", frame.calibration.revision, Calibration(),
                       expected_generation=frame.generation)


def _display(frame):
    return frame.get_by_role("region", name="Display", exact=True)


def test_a_changed_display_is_cleared_by_confirming_the_profile_then_checking_position(page, registry):
    pi, frame_id = enroll_pi(registry, "pi-a"), _landscape(registry, "lobby")
    _bind(registry, frame_id, pi, "HDMI-A-1")
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    _commit_position(registry, frame_id)
    _report(registry, "pi-a", "HDMI-A-1", OTHER_DISPLAY)     # another display on its port
    assert _row(registry, frame_id).readiness is Readiness.DISPLAY_CHANGED

    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        frame = open_frame(page, frame_id, "hardware")
        card = frame.get_by_role("group", name=CARD, exact=True)
        expect(card).to_contain_text("DELL U2720Q")
        expect(card.get_by_role("group", name="Step 1: Frame profile", exact=True)).to_contain_text("1920 × 1080")
        expect(card.get_by_role("group", name="Step 2: Position", exact=True)).to_be_visible()
        expect(card).not_to_contain_text(re.compile(r"Panel|Actuator|Display Host|calibration", re.IGNORECASE))

        with page.expect_request(lambda request: request.url.endswith(f"/v1/operator/frames/{frame_id}/profile")
                                 and request.method == "PUT") as captured:
            card.get_by_role("button", name="Confirm Frame profile", exact=True).click()
        # Pre-filled from the new display's preferred mode; the rest of the profile is kept.
        assert captured.value.post_data_json["profile"] == {
            "width_px": 1920, "height_px": 1080, "diagonal_inches": 24, "video": True}
        expect(_display(frame).get_by_role("status")).to_contain_text("Frame profile set to 1920 × 1080")
        assert _row(registry, frame_id).profile.width_px == 1920
        assert _row(registry, frame_id).readiness is Readiness.DISPLAY_CHANGED   # Position still to check

        card.get_by_role("button", name="Re-check Position", exact=True).click()
        expect(page.get_by_role("tablist", name="Frame settings", exact=True).get_by_role(
            "tab", name="Position", exact=True)).to_have_attribute("aria-selected", "true")
        assert current_hash(page).endswith(f"/frames/{frame_id}/position")

        _commit_position(registry, frame_id)
        assert _row(registry, frame_id).readiness is Readiness.READY

        frame = open_frame(page, frame_id, "hardware")
        expect(_display(frame)).to_contain_text("CN0F1X8Z")
        expect(frame.get_by_role("group", name=CARD, exact=True)).to_have_count(0)


def test_an_unchanged_frame_shows_its_display_and_no_card(page, registry):
    pi = enroll_pi(registry, "pi-a")
    ready, unchecked = _landscape(registry, "ready-frame"), _landscape(registry, "unchecked-frame")
    _bind(registry, ready, pi, "HDMI-A-1")
    _bind(registry, unchecked, pi, "HDMI-A-2")
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    _report(registry, "pi-a", "HDMI-A-2", OTHER_DISPLAY)
    _commit_position(registry, ready)
    assert _row(registry, ready).readiness is Readiness.READY
    assert _row(registry, unchecked).readiness is Readiness.POSITION_NEEDED

    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        frame = open_frame(page, ready, "hardware")
        display = _display(frame)
        expect(display).to_contain_text("DELL U2720Q")
        expect(display).to_contain_text("CN0F1X7K")
        expect(display).to_contain_text("1920 × 1080")
        expect(frame.get_by_role("group", name=CARD, exact=True)).to_have_count(0)

        frame = open_frame(page, unchecked, "hardware")
        expect(_display(frame)).to_contain_text("CN0F1X8Z")
        expect(frame.get_by_role("group", name=CARD, exact=True)).to_have_count(0)


def test_a_display_with_no_serial_is_recognised_by_make_and_model_on_this_frame(page, registry):
    pi, frame_id = enroll_pi(registry, "pi-a"), _frame(registry, "kitchen")
    _bind(registry, frame_id, pi, "HDMI-A-1")
    _report(registry, "pi-a", "HDMI-A-1", NO_SERIAL)

    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        display = _display(open_frame(page, frame_id, "hardware"))
        expect(display).to_contain_text("MNN")
        expect(display).to_contain_text("Recognised by make and model on this Frame")
