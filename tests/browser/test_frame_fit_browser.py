"""The Position tab's Frame fit (roadmap 1c, slice K2; owner 2026-10-10 q1: "Switch to cover. It's a
more sensible default, and skewing transforms or rotations should be an explicit opt in"; q3: Trim
edges removed; the angle: "optional rotation angle", any angle with quarter-turn presets).

The live session is test_console_frame_page_browser.py's fake Central (`FakeSessions`: Central's
trial rules, Save through the real Registry's Save CAS), so Save keeps the fit at any angle and
leaves the Frame profile, the one source of the Frame's aspect and orientation, as it was, exactly
as Central does (owner 2026-10-11: ""landscape" and "portrait" are descriptions of the aspect
ratio. whereas the angle of rotation is a quality of the way frames are hung on the wall").

Mutation probes: read every saved fit as Corners (useDraft.js `fitOf`'s `mode`) -> the
opens-on-Cover case fails (its no-saved-position fallback is not reached here: Central serves every
Frame a Position, and the server-side default is tests/test_frame_fit_settings.py's); keep a crop
field in the draft -> the draft-shape and Trim-absent cases fail; offer Done only for a changed
draft (live-preview-editor.tsx `unconfirmed`) -> the check-again case fails.
"""

import os
import re

import pytest
from console_tasks import open_frame
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect
from test_console_frame_page_browser import (
    FRAME,
    PORTRAIT,
    PRESENTED,
    SAVED_X,
    FakeSessions,
    _bar,
    _move,
    _saved,
    _seed,
    _top_left_x,
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
    return page.get_by_role("region", name="Position", exact=True)


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


def test_a_frame_opens_on_cover_and_trim_edges_is_gone(page, registry, sessions):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        position = _position(open_frame(page, FRAME, "position"))
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(position.get_by_role("radio", name="Cover", exact=True)).to_be_checked()
        expect(position.get_by_role("radio", name="Corners", exact=True)).not_to_be_checked()
        expect(position.get_by_role("slider", name="Angle", exact=True)).to_be_visible()
        position.get_by_role("button", name="Exact values", exact=True).click()
        expect(position).not_to_contain_text(re.compile(r"trim", re.I))
        expect(position.get_by_label(re.compile(r"^Trim"))).to_have_count(0)


def test_choosing_corners_sends_a_corners_draft_and_back(page, registry, sessions):
    """The draft is the fit and its base, nothing else: no trim, no brightness."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        position = _position(open_frame(page, FRAME, "position"))
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        after = len(sessions.sent)
        position.get_by_role("radio", name="Corners", exact=True).click()
        draft = _wait_for_draft(page, sessions, after, lambda draft: draft["fit"]["mode"] == "corners")
        assert set(draft) == {"revision", "fit"}, draft
        assert draft["revision"] == 2
        assert draft["fit"]["rotation"] == 0
        assert round(draft["fit"]["corners"][0][0] * 1920) == SAVED_X
        expect(position).to_contain_text("Stretch the picture exactly onto the corners")
        expect(position.get_by_role("slider", name="Angle", exact=True)).to_have_count(0)  # quarter turns only

        after = len(sessions.sent)
        position.get_by_role("radio", name="Cover", exact=True).click()
        draft = _wait_for_draft(page, sessions, after, lambda draft: draft["fit"]["mode"] == "cover")
        assert set(draft) == {"revision", "fit"}, draft


def test_the_90_button_and_a_typed_angle_each_send_the_angle(page, registry, sessions):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        position = _position(open_frame(page, FRAME, "position"))
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        after = len(sessions.sent)
        position.get_by_role("radio", name="90°", exact=True).click()
        _wait_for_draft(page, sessions, after, lambda draft: draft["fit"]["rotation"] == 90)

        after = len(sessions.sent)
        position.get_by_label("Angle in degrees", exact=True).fill("7.5")
        draft = _wait_for_draft(page, sessions, after, lambda draft: draft["fit"]["rotation"] == 7.5)
        assert draft["fit"]["mode"] == "cover"
        expect(position.get_by_role("slider", name="Angle", exact=True)).to_have_value("7.5")


def test_a_60_degree_save_on_a_landscape_frame_keeps_its_landscape_profile(page, registry, sessions):
    """At 60 degrees the turned corners' bounding box is portrait, but the angle is how the Frame
    is hung, not its orientation: Done saves the angle, and the Hardware tab still shows the
    Frame's own 1920 x 1080 profile. Mutation probe: write the profile from the turned corners at
    Save, or refuse the angle -> this fails."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        position = _position(open_frame(page, FRAME, "position"))
        bar = _bar(page)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        position.get_by_label("Angle in degrees", exact=True).fill("60")
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        bar.get_by_role("button", name="Done", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="Saved at")).to_be_visible()
        expect(position.get_by_role("alert")).to_have_count(0)
        fit = _saved(registry).fit
        assert (fit.mode, fit.rotation) == ("cover", 60)
        profile = page.get_by_role("region", name="Frame profile", exact=True)
        open_frame(page, FRAME, "hardware")
        expect(profile).to_contain_text("Pixel width: 1920 px")
        expect(profile).to_contain_text("Pixel height: 1080 px")
        expect(page.get_by_role("group", name="Check the Display", exact=True)).to_have_count(0)


def test_a_frame_to_check_again_is_kept_by_done_with_no_change(page, registry, sessions):
    """After migration 075 every Frame's Position must be checked again (owner q1's accepted
    cost): its saved fit is what the operator confirms, so Done is offered with no change once the
    Pi presents it, and keeping it makes the Frame ready. A ready Frame with no change has nothing
    to keep."""
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
        assert _top_left_x(frame.calibration) == SAVED_X
        page.reload()
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(_bar(page).get_by_role("button", name="Done", exact=True)).to_be_disabled()
        expect(_bar(page)).to_contain_text("No changes to keep.")


def test_with_no_reported_output_size_a_step_is_a_pixel_of_a_common_screen_not_the_profile(
        page, registry, sessions):
    """The Frame profile is the Frame's shape, never the output's size: while the Pi reports no
    size, a step is a pixel of 1920 x 1080 and the tab says so (here its output last reported
    disconnected). Mutation probe: fall back to the profile (1080 x 1920 here) -> the 10 px step
    moves the corner 10/1080 of the width."""
    _seed(registry, profile=PORTRAIT, rotation=90)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE outputs SET observation = jsonb_set(observation, '{connected}', 'false')")
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        position = _position(open_frame(page, FRAME, "position"))
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(position).to_contain_text("The Pi has not reported this output's size")
        after = len(sessions.sent)
        _move(page)
        draft = _wait_for_draft(page, sessions, after, lambda draft: _top_left_x(draft) != SAVED_X)
        assert _top_left_x(draft) == SAVED_X + 10
