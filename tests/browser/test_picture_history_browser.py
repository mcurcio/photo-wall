"""The Picture tab's History (configuration snapshots K1; owner, 2026-10-13: "configuration snapshots"
that keep "a history of what has been done in the past", used to "View and restore"): every kept and
not-kept change of the display's picture with its time, and Restore into the live editor (he still
presses Done).

Every write goes through the operator API the tab calls, on the real Registry, display store and
history store; the Pi is test_frame_picture_browser.py's `FakePi`, which reads the projected picture
document and reports for its change, so a sent entry and Done come from Central's own projection and
status, never from the console alone.

Mutation probes (each fails the named case):
- K1-no-refresh: drop PictureHistory's re-read after Done (the `savedAt` effect) -> the tracer's read
  right after Done is missing.
- K1-restore-ignored: keep the old draft after an accepted Restore (pictureSession.js `restore` returns
  before replacing it) -> the tracer's slider keeps the tried value; folding no read into the draft
  -> Done is never offered after the Restore.
- K1-always-enabled: `restoreState` always enabled -> the other-window case.
- K1-silent-skip: `restoreNotes` says nothing -> the skipped-setting case.
- K1-no-notice: no `unrecorded_change` Note -> the unrecorded case.
"""

import os
import re
import time

import pytest
from console_tasks import connect, open_frame
from operator_harness import operator_server
from playwright.sync_api import expect
from test_frame_picture_browser import (
    DISPLAY,
    PHOTO_WALL,
    PRESENTED,
    FakePi,
    _bar,
    _press,
    _seen,
    _slider,
    _store,
)

from contracts.node_picture import HardwareRange, HardwareSetting, RangeKind

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

FRAME = "kitchen"
KEPT = "Kept (Done)"
TRIED = "Tried, not kept"
TIME = re.compile(r"^\d\d:\d\d:\d\d ")
# The display offers contrast alone: brightness (10) is no longer offered.
CONTRAST_ONLY = (HardwareSetting("12", HardwareRange(RangeKind.LEVEL, 100, ()), 50),)


def _history(page):
    return page.get_by_role("region", name="History", exact=True)


def _rows(page, what):
    """The History's rows of one kind, newest first; each row is named by its time and kind."""
    return _history(page).get_by_role("listitem").filter(has=page.get_by_text(what, exact=True))


def _done(page):
    """Done once the Pi presents the latest change; then the tab says Saved."""
    bar = _bar(page)
    expect(bar.get_by_role("status")).to_have_text(PRESENTED)
    done = bar.get_by_role("button", name="Done", exact=True)
    expect(done).to_be_enabled()
    done.click()
    expect(page.get_by_role("region", name="Picture", exact=True).get_by_role("status")
           .filter(has_text="Saved at")).to_be_visible()


def _history_reads(page):
    """The monotonic times of every History read the page begins (`reads`) and of every Done
    answered (`done`)."""
    seen = {"reads": [], "done": []}
    page.on("request", lambda request: seen["reads"].append(time.monotonic())
            if "/picture-history" in request.url and request.method == "GET" else None)
    page.on("response", lambda response: seen["done"].append(time.monotonic())
            if response.url.endswith("/done") and response.request.method == "POST" else None)
    return seen


def _values(row):
    """A row with its Details open: every value it records."""
    row.get_by_role("button", name="Details").click()
    return row


def _kept_brightness(page, steps=1):
    """One Photo Wall brightness change kept by Done: 100 % -> 100 + steps."""
    _press(page, PHOTO_WALL, "Brightness", "ArrowRight", steps)
    _done(page)
    expect(_rows(page, KEPT)).to_have_count(1)


def test_a_kept_and_a_tried_change_are_listed_and_restore_puts_the_kept_one_back_until_done(page, registry):
    """The tracer: Done -> a Kept row with its time, read again at once; another change -> a Tried,
    not kept row; Restore the kept row -> the slider shows its value and Done is offered once the Pi
    presents; Done -> a second Kept row."""
    _seen(registry)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(_history(page)).to_be_visible()

        seen = _history_reads(page)
        _press(page, PHOTO_WALL, "Brightness", "ArrowRight", 3)
        _done(page)
        kept = _rows(page, KEPT)
        expect(kept).to_have_count(1)
        expect(kept.first).to_have_attribute("aria-label", re.compile(TIME.pattern + re.escape(KEPT)))
        expect(_values(kept.first)).to_contain_text(re.compile(r"Brightness\s*103 %"))
        # Read again as Done is answered, not at the next poll (CADENCE_MS 2000).
        [done] = seen["done"]
        assert any(done <= at <= done + 0.25 for at in seen["reads"]), (done, seen["reads"])

        expect(_slider(page, PHOTO_WALL, "Brightness")).to_be_enabled()  # the next session has begun
        brightness = _press(page, PHOTO_WALL, "Brightness", "ArrowRight", 5)
        expect(brightness).to_have_value("108")
        tried = _rows(page, TRIED)
        expect(tried.first).to_contain_text("Brightness 108 %")
        expect(tried.first).to_have_attribute("data-kept", "false")

        kept.first.get_by_role("button", name="Restore", exact=True).click()
        expect(_slider(page, PHOTO_WALL, "Brightness")).to_have_value("103")
        _done(page)
        expect(_rows(page, KEPT)).to_have_count(2)
        expect(_values(_rows(page, KEPT).first)).to_contain_text(re.compile(r"Brightness\s*103 %"))


def test_restore_waits_with_its_reason_while_another_window_holds_the_editor(page, registry):
    _seen(registry)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        _kept_brightness(page)
        restore = _rows(page, KEPT).first.get_by_role("button", name="Restore", exact=True)
        expect(restore).to_be_enabled()

        other = page.context.new_page()
        connect(other, origin, "wall")
        open_frame(other, FRAME, "picture")
        held = _rows(other, KEPT).first.get_by_role("button", name="Restore", exact=True)
        expect(held).to_be_disabled()
        expect(held).to_have_accessible_description(re.compile("Someone else is adjusting this display's picture"))
        other.close()


def test_a_restore_names_the_display_settings_this_display_no_longer_offers(page, registry):
    _seen(registry)
    with FakePi(registry) as pi, operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        _press(page, DISPLAY, "Brightness", "ArrowRight", 2)
        _done(page)
        expect(_rows(page, KEPT)).to_have_count(1)

        pi.offered = CONTRAST_ONLY
        expect(_slider(page, DISPLAY, "Brightness")).to_have_count(0)  # no longer offered
        _rows(page, KEPT).first.get_by_role("button", name="Restore", exact=True).click()
        expect(_history(page)).to_contain_text("Not restored: Brightness (this display does not offer them now).")


def test_values_changed_outside_photo_wall_are_said_until_the_next_done(page, registry):
    _seen(registry)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        _kept_brightness(page)
        notice = "This display's values changed outside Photo Wall since its last kept change."
        expect(_history(page)).not_to_contain_text(notice)

        _store(registry, software_contrast_percent=90)
        expect(_history(page)).to_contain_text(notice)
