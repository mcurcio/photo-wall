"""The Frame page's turn control (render redesign S5, bead B5): the Hardware tab's "How the screen
hangs" sets the Frame's turn through the real Registry (`PUT /v1/operator/frames/{id}/turn`), the
moment it is chosen. A turn makes the Frame's Position one to check (readiness position-needed), the
Position tab draws the screen as it now hangs, and the Frame section's size on screen is the box on
the screen as it hangs (domain/frame-turn.tsx, domain/frame-facts.tsx).
The Position tab's live session needs a Pi's Display Host, so there the Frame page tests' fake
Central answers the session routes (test_console_frame_page_browser.py `FakeSessions`).
"""

import os
import re

import pytest
from console_tasks import open_frame
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect
from support.frames import frame_create
from support.position import PORTRAIT_ON_WIDE, save_position
from test_console_frame_page_browser import FakeSessions
from test_registry import enroll

from central.displays.model import Readiness

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

FRAME = "hall-frame"
OUTPUT = "HDMI-A-1"  # enroll() reports it connected at 1920 x 1080
DEVICE = "device-frame-turn-pi"
CHANGED = "The picture turns now. Check its position on the Position tab, then press Done there."
SAVED = re.compile(rf"^Saved at \d\d:\d\d:\d\d\. {re.escape(CHANGED)}$")
CONFLICT = "This Frame changed while you chose. Choose again."


def _seed(registry):
    """A portrait Frame bound to a 1920 x 1080 Output, hung upright, its Position saved (ready)."""
    identity, _key, _request = enroll(registry, count=1, device_id=DEVICE)
    registry.create_frame(frame_create(FRAME, x_mm=100, y_mm=100))
    registry.bind(FRAME, identity["player_id"], OUTPUT, expected_generation=0)
    save_position(registry, FRAME, PORTRAIT_ON_WIDE)


def _stored(registry):
    return next(frame for frame in registry.inventory().frames if frame.id == FRAME)


def _section(frame):
    return frame.get_by_role("region", name="How the screen hangs", exact=True)


def _choice(section, words):
    return section.get_by_role("radiogroup", name="Which way is the top of the screen?").get_by_role(
        "radio", name=words, exact=True)


def _facts(frame):
    return frame.get_by_role("region", name="Frame", exact=True)


def test_choosing_top_to_the_right_turns_the_frame_and_the_position_tab_follows(page, registry):
    """Mutation probe: send the Frame's current turn instead of the chosen one -> Central answers
    "not changed", the turn stays 0 and this test fails."""
    _seed(registry)
    assert (_stored(registry).turn, _stored(registry).readiness) == (0, Readiness.READY)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        FakeSessions(page, registry)
        frame = open_frame(page, FRAME, "hardware")
        section = _section(frame)
        expect(section).to_contain_text(
            "Which way the display's top edge points as it hangs on the wall: the edge at the top when the display "
            "stands on its feet (where its logo reads the right way up). Photo Wall turns the picture to match.")
        expect(_choice(section, "Upright")).to_be_checked()
        # Upright, the 9:16 box is 576 x 1,024 pixels of the 1920 x 1080 screen.
        expect(_facts(frame)).to_contain_text("576 × 1,024 pixels, 9:16")

        _choice(section, "Top to the right").click()
        expect(section.get_by_role("status")).to_have_text(SAVED)
        stored = _stored(registry)
        assert (stored.turn, stored.readiness) == (90, Readiness.POSITION_NEEDED)
        expect(_choice(section, "Top to the right")).to_be_checked()
        # Hung top to the right, the screen is 1080 x 1920: the box keeps its shares, another shape.
        expect(_facts(frame)).to_contain_text("324 × 1,820 pixels")

        section.get_by_role("link", name="Open the Position tab", exact=True).click()
        expect(page).to_have_url(re.compile(rf"#/wall/frames/{FRAME}/position$"))
        expect(page.get_by_text("This screen hangs with its top to the right.")).to_be_visible()
        editor = page.get_by_role("group", name="Picture position", exact=True)
        box = editor.bounding_box()
        assert box["height"] > box["width"], box
        # The box keeps its shares of the screen: on the screen as it now hangs it is another shape.
        expect(page.get_by_role("group", name="This Frame", exact=True)).to_contain_text("324 × 1,820 pixels")
        expect(page.get_by_label("Left", exact=True)).to_have_value("378")
        # History says the turn in words with the position it kept, in the editor's pixels, and why the
        # rows made upright cannot be restored, with the way back to the Hardware tab.
        history = page.get_by_role("region", name="History", exact=True)
        turned = history.get_by_role("listitem", name=re.compile(r"Kept \(screen turned\)$"))
        expect(turned).to_contain_text("Screen turned: it hangs with its top to the right. Position kept: Left and top")
        expect(turned).to_contain_text("Left and top 378 across, 50 down · Size 324 × 1,820 pixels")
        upright = history.get_by_role("listitem", name=re.compile(r"Created$"))
        expect(upright.get_by_role("button", name="Restore", exact=True)).to_have_accessible_description(
            re.compile(r"^Made when the screen hung upright\. Turn the screen back on the Hardware tab to restore it\."))
        page.unroute_all(behavior="ignoreErrors")


def test_a_choice_against_a_stale_generation_is_refused_and_changes_nothing(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "hardware")
        section = _section(frame)
        expect(_choice(section, "Upright")).to_be_checked()
        # The page keeps reading what it read before the other write: the generation it sends is stale.
        stale = page.evaluate("fetch('/v1/operator/snapshot').then((answer) => answer.text())")
        page.route("**/v1/operator/snapshot", lambda route: route.fulfill(
            status=200, content_type="application/json", body=stale))
        before = _stored(registry)
        registry.turn_frame(FRAME, 180, expected_generation=before.generation)  # another window

        _choice(section, "Top to the right").click()
        problem = section.get_by_role("alert")
        expect(problem).to_contain_text(CONFLICT)
        after = _stored(registry)
        assert (after.turn, after.generation) == (180, before.generation + 1)
        expect(section.get_by_text(CHANGED)).to_have_count(0)

        page.unroute("**/v1/operator/snapshot")
        expect(_choice(section, "Upside down")).to_be_checked(timeout=15_000)


def test_an_unbound_frame_shows_the_control_and_takes_a_turn(page, registry):
    registry.create_frame(frame_create(FRAME, x_mm=100, y_mm=100))
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "hardware")
        section = _section(frame)
        _choice(section, "Top to the left").click()
        expect(section.get_by_role("status")).to_have_text(SAVED)
        assert _stored(registry).turn == 270


def test_no_answer_asks_to_choose_again(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "hardware")
        section = _section(frame)
        expect(_choice(section, "Upright")).to_be_checked()
        page.route("**/v1/operator/frames/*/turn", lambda route: route.abort())
        _choice(section, "Upside down").click()
        expect(section.get_by_role("alert")).to_contain_text("Photo Wall's server did not answer. Choose again.")
        assert _stored(registry).turn == 0
