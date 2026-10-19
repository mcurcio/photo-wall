"""The Position tab's Frame fit (render redesign S5, run ledger .claude/runs/render-planes/s5-page.md
B2; frame box run, .claude/runs/frame-box/page.md fb-6, fb-7): the picture is one box on its screen
as it hangs, its left, top, width and height set independently (owner, 2026-10-19: "I need to be
able to set width and height independently, along with the position of that box on the display");
the screen's turn is the Frame's own, set on the Hardware tab, never the Position's. No Fit choice,
no rotation, no angle, no trim, and no Frame profile: the box is the Frame's shape.

The live session is test_console_frame_page_browser.py's fake Central (`FakeSessions`: Central's
trial rules, Save through the real Registry's Save CAS), so Save keeps the box and leaves the
Frame profile as it was, exactly as Central does.

Mutation probes: send the fit with other keys (useDraft.js `fitOf`) -> the draft-shape case fails;
offer Done only for a changed draft (live-preview-editor.tsx `unconfirmed`) -> the check-again case
fails; step by the Frame profile instead of the 16:9 stand-in with no known screen size
(frame-position.tsx `STAND_IN`) -> the no-size case fails.
"""

import os
import re

import pytest
from console_tasks import open_frame
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect
from test_console_frame_page_browser import (
    FRAME,
    PRESENTED,
    SAVED_X,
    FakeSessions,
    _bar,
    _left_x,
    _move,
    _saved,
    _seed,
)

from central.displays.model import Readiness

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

@pytest.fixture
def sessions(page, registry):
    yield FakeSessions(page, registry)
    page.unroute_all(behavior="ignoreErrors")


def _position(page):
    return page.get_by_role("region", name="Where the picture sits on the screen", exact=True)


def _sent(fake, after):
    """The Position drafts the page sent (edit or renew) after the first `after` requests."""
    return [body["calibration"] for operation, body in zip(fake.sent[after:], fake.bodies[after:])
            if operation in ("edit", "renew")]


def _wait_for_draft(page, fake, after, check):
    """Waits until a draft the page sent after `after` requests passes `check`; returns it."""
    for _ in range(50):
        drafts = [draft for draft in _sent(fake, after) if check(draft)]
        if drafts:
            return drafts[-1]
        page.wait_for_timeout(100)
    raise AssertionError(_sent(fake, after))


def test_the_tab_edits_one_box_with_no_fit_choice_rotation_or_trim(page, registry, sessions):
    """The draft is the box and its base, nothing else: no corners, rotation, trim or brightness."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        position = _position(open_frame(page, FRAME, "position"))
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(position.get_by_role("radio", name="Cover", exact=True)).to_have_count(0)
        expect(position.get_by_role("radio", name="Corners", exact=True)).to_have_count(0)
        expect(position.get_by_role("slider", name="Angle", exact=True)).to_have_count(0)
        expect(position.get_by_role("radio", name="90°", exact=True)).to_have_count(0)
        expect(position).to_contain_text("This screen hangs upright.")
        expect(position).not_to_contain_text(re.compile(r"trim|rotation|profile", re.I))
        expect(position.get_by_label(re.compile(r"^Trim"))).to_have_count(0)

        after = len(sessions.sent)
        _move(page)
        draft = _wait_for_draft(page, sessions, after, lambda draft: _left_x(draft) != SAVED_X)
        assert set(draft) == {"revision", "fit"}, draft
        assert draft["revision"] == 2
        assert set(draft["fit"]) == {"mode", "left", "top", "width", "height"}, draft
        assert draft["fit"]["mode"] == "box" and _left_x(draft) == SAVED_X + 10


def test_the_width_typed_in_pixels_sends_its_share_of_the_screen_and_keeps_the_rest(page, registry, sessions):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        position = _position(open_frame(page, FRAME, "position"))
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        after = len(sessions.sent)
        position.get_by_label("Width", exact=True).fill("1000")  # 100 to 1100 px across: it stays on the screen
        draft = _wait_for_draft(page, sessions, after, lambda draft: draft["fit"]["width"] != 0.5)
        assert draft["fit"] == {"mode": "box", "left": SAVED_X / 1920, "top": 0.25, "width": 1000 / 1920,
                                "height": 0.5}, draft


def test_a_saved_box_is_the_frames_size_on_the_hardware_tab(page, registry, sessions):
    """Done saves the box; the Hardware tab's Frame section says its pixels: half the 1920 x 1080
    screen each way."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        position = _position(open_frame(page, FRAME, "position"))
        bar = _bar(page)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        position.get_by_role("button", name="Centre", exact=True).click()
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        bar.get_by_role("button", name="Done", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="Saved at")).to_be_visible()
        expect(position.get_by_role("alert")).to_have_count(0)
        fit = _saved(registry).fit
        assert (fit.mode, fit.left, fit.top, fit.width, fit.height) == ("box", 0.25, 0.25, 0.5, 0.5)
        facts = page.get_by_role("region", name="Frame", exact=True)
        open_frame(page, FRAME, "hardware")
        expect(facts).to_contain_text("960 × 540 pixels, 16:9")


def test_a_frame_to_check_again_is_kept_by_done_with_no_change(page, registry, sessions):
    """A Frame whose Position must be checked again (migration 102, a turn change): its saved
    rectangle is what the operator confirms, so Done is offered with no change once the Pi presents
    it, and keeping it makes the Frame ready. A ready Frame with no change has nothing to keep."""
    _seed(registry)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE frames SET position_generation=NULL")
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "position")
        bar = _bar(page)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        expect(bar).to_contain_text("press Done to keep this position")
        done = bar.get_by_role("button", name="Done", exact=True)
        expect(done).to_be_enabled()
        done.click()
        expect(page.get_by_role("status").filter(has_text="Saved at")).to_be_visible()
        frame = next(frame for frame in registry.inventory().frames if frame.id == FRAME)
        assert frame.readiness is Readiness.READY
        assert _left_x(frame.calibration) == SAVED_X
        page.reload()
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(_bar(page).get_by_role("button", name="Done", exact=True)).to_be_disabled()
        expect(_bar(page)).to_contain_text("No changes to keep.")


def test_with_no_known_screen_size_the_drawing_stands_in_at_16_9_never_the_profile(page, registry, sessions):
    """While neither the Pi's report nor a Display seen on the Output gives a screen size (Central's
    `screen_px` is None), the drawing is a 16:9 stand-in, a note says the numbers are shares of the
    screen, no pixels or shape are claimed and a step is 1/1,920 of its width. Mutation probe: stand
    in by 1600 x 1200 -> the 10 px step moves the left edge 10/1600 of the width -> RED."""
    _seed(registry)
    with registry.db.transaction() as conn:
        conn.execute("""UPDATE outputs SET observation = observation || '{"width_px": 0, "height_px": 0}'""")
    assert next(frame for frame in registry.inventory().frames if frame.id == FRAME).screen_px is None
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        position = _position(open_frame(page, FRAME, "position"))
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(position).to_contain_text("the drawing stands in at 16:9")
        expect(position).to_contain_text("shares of the screen, not its pixels")
        expect(position.get_by_role("group", name="This Frame", exact=True)).not_to_contain_text("pixels,")
        drawn = position.get_by_role("group", name="Picture position", exact=True).bounding_box()
        assert drawn["width"] > drawn["height"], drawn
        saved = _saved(registry).fit.left
        after = len(sessions.sent)
        _move(page)
        draft = _wait_for_draft(page, sessions, after, lambda draft: draft["fit"]["left"] != saved)
        assert abs((draft["fit"]["left"] - saved) * 1920 - 10) < 1e-6, draft
