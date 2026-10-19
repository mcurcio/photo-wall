"""The Frame page's Hardware tab: the Frame's facts (frame box run, .claude/runs/frame-box/page.md,
bead fb-11).

The Frame's pixels, shape and physical size are worked out, never typed: its pixels are its Position's
box placed on its screen as it hangs, its shape their ratio, its physical size the box's share of the
Display's own size (its EDID's). The Frame section says them and links to the Position tab; it has no
field for pixels or a diagonal, and the console never calls the Frame profile route. Shows videos is the
Frame's own switch, saved at once.

Every display report travels the real path (`test_display_identity._report`: the link store's commit
and its Output report judge); bindings, turns and Position commits go through the Registry.

Mutation probe: place the box on the screen as built instead of as it hangs (no turn): the turned
Frame's facts test fails.
"""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from console_tasks import connect, open_frame
from operator_harness import operator_server
from playwright.sync_api import expect
from support.frames import frame_create
from support.position import save_position
from test_display_identity import MODE, SERIAL_DISPLAY, _bind, _generation, enroll_pi
from test_display_identity import _report as _recorded

from central.displays.model import Readiness
from contracts.models import Calibration, PositionBox
from contracts.node_output import OutputReport, encode_output_report

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

NO_SIZE = "Size on the wall: Not known (this display does not state its size)"


def _frame(registry, frame_id="lobby") -> str:
    registry.create_frame(frame_create(frame_id, x_mm=100, y_mm=100))
    return frame_id


def _sized(registry, serial: str, output_id: str, size_mm: tuple[int, int] | None) -> None:
    """The Pi's Output report stating the display's size, recorded off Playwright's running event loop."""
    report = OutputReport(output_id, True, SERIAL_DISPLAY, MODE, (), None, None, None, None, size_mm)
    with ThreadPoolExecutor(1) as pool:
        pool.submit(_recorded, registry, serial, output_id, SERIAL_DISPLAY,
                    data=encode_output_report(report)).result()


def _bound(registry, size_mm: tuple[int, int] | None, frame_id="lobby") -> str:
    """A Frame on a Pi's 1920 x 1080 Output whose display states `size_mm`, Position the whole screen."""
    pi = enroll_pi(registry, "pi-a")
    _bind(registry, _frame(registry, frame_id), pi, "HDMI-A-1")
    _sized(registry, "pi-a", "HDMI-A-1", size_mm)
    save_position(registry, frame_id)
    return frame_id


def _facts(inspector):
    return inspector.get_by_role("region", name="Frame", exact=True)


def _display(inspector):
    return inspector.get_by_role("region", name="Display", exact=True)


def _no_profile_route(page) -> list:
    """Every request the page makes to a Frame profile route (there must be none)."""
    calls: list = []
    page.on("request", lambda request: calls.append(request.url) if request.url.endswith("/profile") else None)
    return calls


def test_the_frame_reads_its_pixels_shape_and_size_from_its_box_and_display(page, registry):
    frame_id = _bound(registry, (950, 540))
    with operator_server(registry.db, registry.clock) as origin:
        calls = _no_profile_route(page)
        connect(page, origin, "wall")
        inspector = open_frame(page, frame_id, "hardware")
        facts = _facts(inspector)
        expect(facts).to_contain_text("1,920 × 1,080 pixels, 16:9")
        expect(facts).to_contain_text("95 × 54 cm")
        # Worked out, never typed: no field for pixels or a diagonal, no profile editor.
        expect(facts.get_by_role("spinbutton")).to_have_count(0)
        expect(facts.get_by_role("textbox")).to_have_count(0)
        expect(inspector.get_by_role("button", name="Edit Frame profile", exact=True)).to_have_count(0)
        expect(inspector).not_to_contain_text("Diagonal")
        link = facts.get_by_role("link", name="Change its size and place on the Position tab", exact=True)
        expect(link).to_have_attribute("href", f"#/wall/frames/{frame_id}/position")
        expect(_display(inspector)).to_contain_text("95 × 54 cm")
        expect(inspector).not_to_contain_text(" in across")
        assert not calls


def test_a_turned_frame_reads_its_pixels_and_size_as_it_hangs(page, registry):
    """A box half the width and a quarter of the height of a screen hung top to the right: 540 x 480
    pixels of the 1080 x 1920 screen as it hangs, and of a 950 x 540 mm display 27 x 23.75 cm."""
    frame_id = _bound(registry, (950, 540))
    registry.turn_frame(frame_id, 90, expected_generation=_generation(registry, frame_id))
    save_position(registry, frame_id, Calibration(fit=PositionBox(left=0.25, top=0.5, width=0.5, height=0.25)))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        facts = _facts(open_frame(page, frame_id, "hardware"))
        expect(facts).to_contain_text("540 × 480 pixels, 9:8")
        expect(facts).to_contain_text("27 × 24 cm")


def test_a_display_that_states_no_size_says_so_and_what_it_means(page, registry):
    frame_id = _bound(registry, None)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        inspector = open_frame(page, frame_id, "hardware")
        facts = _facts(inspector)
        expect(facts).to_contain_text("1,920 × 1,080 pixels, 16:9")
        expect(facts).to_contain_text(NO_SIZE)
        expect(facts).to_contain_text(
            "Until then, only even room light is drawn and this Frame counts as large for videos.")
        expect(_display(inspector)).to_contain_text("Does not state its size")


def test_an_unbound_frame_says_its_size_comes_from_its_screen(page, registry):
    frame_id = _frame(registry)
    with operator_server(registry.db, registry.clock) as origin:
        calls = _no_profile_route(page)
        connect(page, origin, "wall")
        facts = _facts(open_frame(page, frame_id, "hardware"))
        expect(facts).to_contain_text("Not known until this Frame is connected to a Pi")
        expect(facts).not_to_contain_text("pixels,")
        expect(facts.get_by_role("spinbutton")).to_have_count(0)
        assert not calls


def test_shows_videos_saves_at_once_and_keeps_a_ready_frame_ready(page, registry):
    """Shows videos is the Frame's own setting (frame box run, fb-5): one switch on the Hardware
    tab, saved the moment it is switched, with no generation, so a bound, ready Frame stays ready."""
    frame_id = _bound(registry, (950, 540))
    before = registry.inventory().frames[0]
    assert (before.readiness, before.video) == (Readiness.READY, True)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        inspector = open_frame(page, frame_id, "hardware")
        shows = _facts(inspector).get_by_role("switch", name="Shows videos", exact=True)
        expect(shows).to_be_checked()
        with page.expect_request(
            lambda request: request.url.endswith(f"/v1/operator/frames/{frame_id}/video")
            and request.method == "PUT"
        ) as captured:
            shows.click()
        assert captured.value.post_data_json == {"video": False}
        expect(shows).not_to_be_checked()
        expect(inspector.get_by_text("Saved", exact=True)).to_be_visible()
        after = registry.inventory().frames[0]
        assert (after.video, after.readiness, after.generation) == (False, Readiness.READY, before.generation)

        with page.expect_response(lambda response: response.url.endswith(f"/v1/operator/frames/{frame_id}/video")
                                  and response.request.method == "PUT"):
            inspector.get_by_role("button", name="Reset Shows videos (Default: On)", exact=True).click()
        expect(shows).to_be_checked()
        assert registry.inventory().frames[0].video is True
