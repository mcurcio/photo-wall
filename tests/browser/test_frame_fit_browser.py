"""The Position tab's Frame fit (render redesign S5, run ledger .claude/runs/render-planes/s5-page.md
B2): the picture is one upright rectangle of the Frame profile's shape on its screen as it hangs
(owner, 2026-10-17: "We can make the position pure crop and scale"); the screen's turn is the
Frame's own, set on the Hardware tab, never the Position's. No Fit choice, no rotation, no angle,
no trim.

The live session is test_console_frame_page_browser.py's fake Central (`FakeSessions`: Central's
trial rules, Save through the real Registry's Save CAS), so Save keeps the rectangle and leaves the
Frame profile, the one source of the Frame's shape, as it was, exactly as Central does.

Mutation probes: send the fit with the old form's keys (useDraft.js `updateHandles`) -> the
draft-shape case fails; offer Done only for a changed draft (live-preview-editor.tsx
`unconfirmed`) -> the check-again case fails; step by the Frame profile instead of 1920 x 1080
with no reported size (frame-position.tsx `FALLBACK_SIZE`) -> the no-size case fails.
"""

import os
import re

import pytest
from console_tasks import open_frame
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect
from test_console_frame_page_browser import (
    FRAME,
    HALF,
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
from contracts.models import FrameProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

# A 4:3 Frame: its profile is neither the screen's size nor its shape.
FOUR_THREE = FrameProfile(width_px=1600, height_px=1200, diagonal_inches=24)


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


def test_the_tab_edits_one_rectangle_with_no_fit_choice_rotation_or_trim(page, registry, sessions):
    """The draft is the rectangle and its base, nothing else: no corners, rotation, trim or brightness."""
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
        position.get_by_role("button", name="Exact values", exact=True).click()
        expect(position).not_to_contain_text(re.compile(r"trim|rotation|corner", re.I))
        expect(position.get_by_label(re.compile(r"^Trim"))).to_have_count(0)

        after = len(sessions.sent)
        _move(page)
        draft = _wait_for_draft(page, sessions, after, lambda draft: _left_x(draft) != SAVED_X)
        assert set(draft) == {"revision", "fit"}, draft
        assert draft["revision"] == 2
        assert set(draft["fit"]) == {"mode", "centre_x", "centre_y", "width"}, draft
        assert draft["fit"]["mode"] == "rectangle" and _left_x(draft) == SAVED_X + 10


def test_the_width_typed_in_pixels_sends_its_share_of_the_screen(page, registry, sessions):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        position = _position(open_frame(page, FRAME, "position"))
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        after = len(sessions.sent)
        position.get_by_label("Width in pixels", exact=True).fill("1000")  # 80 to 1080 px across: it stays on the screen
        draft = _wait_for_draft(page, sessions, after, lambda draft: draft["fit"]["width"] != 0.5)
        assert draft["fit"]["width"] == 1000 / 1920
        assert draft["fit"]["centre_x"] == (SAVED_X + HALF) / 1920
        expect(position.get_by_role("slider", name="Width", exact=True)).to_have_value("1000")


def test_a_saved_rectangle_keeps_the_frames_own_profile(page, registry, sessions):
    """Done saves the rectangle; the Hardware tab still shows the Frame's own 1920 x 1080 profile."""
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
        assert (fit.mode, fit.centre_x, fit.centre_y, fit.width) == ("rectangle", 0.5, 0.5, 0.5)
        profile = page.get_by_role("region", name="Frame profile", exact=True)
        open_frame(page, FRAME, "hardware")
        expect(profile).to_contain_text("Pixel width: 1920 px")
        expect(profile).to_contain_text("Pixel height: 1080 px")
        expect(page.get_by_role("group", name="Check the Display", exact=True)).to_have_count(0)


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


def test_with_no_reported_output_size_a_step_is_a_pixel_of_a_common_screen_not_the_profile(
        page, registry, sessions):
    """The Frame profile is the Frame's shape, never the screen's size: while the Pi reports no
    size, a step is a pixel of 1920 x 1080 and the tab says so (here its output last reported
    disconnected). Mutation probe: fall back to the profile (1600 x 1200 here) -> the 10 px step
    moves the centre 10/1600 of the width."""
    _seed(registry, profile=FOUR_THREE)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE outputs SET observation = jsonb_set(observation, '{connected}', 'false')")
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        position = _position(open_frame(page, FRAME, "position"))
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(position).to_contain_text("The Pi has not reported its display's size")
        saved = _saved(registry).fit.centre_x
        after = len(sessions.sent)
        _move(page)
        draft = _wait_for_draft(page, sessions, after, lambda draft: draft["fit"]["centre_x"] != saved)
        assert abs((draft["fit"]["centre_x"] - saved) * 1920 - 10) < 1e-6, draft
