"""The Position tab's History (configuration snapshots K2; owner, 2026-10-13: "configuration
snapshots" that keep "a history of what has been done in the past", scope "Everything per
Display+Frame", used to "View and restore"): the Frame's kept and tried positions, mats and lights
with their times, and Restore into the live draft (he still presses Done).

As in test_frame_mat_browser.py, a real session needs a Node presenting it, so a fake Central
answers the session routes with Central's rules (`MatSessions`); this one adds what Central's trial
owner does for the history (central/fleet/node_calibration.py): every exchange records what it hands
the Pi through the real store (`position_history_store.record_sent_in`, as `deliver_in` does), and a
Restore is answered with Central's own rules (`position_entry_in`, `restore_refusal`,
`restored_draft` at the trial's base revision). Save goes through the real Registry, which keeps
its entry; the History is read from the real route.

Mutation probes (each fails the named case):
- K2-draft-not-replaced: keep the old draft after an accepted Restore (liveAdjustment.js `restore`)
  -> the tracer's corner and depth stay as tried.
- K2-refusal-ignored: offer Restore whatever `restore_refusal` says (position-history.tsx
  `restoreState`) -> the other-profile case.
- K2-entry-revision: take the answer as the draft's origin (`adopt`) instead of keeping the draft's
  own -> the restored draft is no change, Done is never offered: the tracer and the overtaken case.
- K2-always-enabled: `restoreState` offers Restore with no live session -> the no-session case.
- K2-no-refresh: drop PositionHistory's re-read after Done (the `savedAt` effect) -> the tracer's
  read right after Done is missing.
- K2-ungrouped: no session runs (HistoryList `runsOf` one row each) -> the run case's earlier try
  is listed before its run is opened.
"""

import os
import re
import time
from uuid import UUID

import pytest
from console_tasks import open_frame
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect
from test_console_frame_page_browser import FRAME, OVERTAKEN, PRESENTED, SAVED_X, _bar, _move, _seed
from test_display_identity import _sql
from test_frame_mat_browser import MatSessions, _mat, _saved_mat

from central.fleet.position_history import (
    PositionValues,
    TrialHanded,
    position_values,
    restore_refusal,
    restored_draft,
)
from central.infra.position_history_store import (
    frame_made_for_in,
    position_entry_in,
    record_sent_in,
)
from contracts.models import Calibration, Mat

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

KEPT = "Kept (Done)"
TRIED = "Tried, not kept"


class HistorySessions(MatSessions):
    """MatSessions that record what each exchange hands the Pi and answer Restore as Central does."""

    def _values(self):
        return PositionValues(Calibration.model_validate(self.row["calibration"]).fit, Mat.model_validate(self.row["mat"]))

    def _answer(self, route):
        if route.request.url.endswith("/restores"):
            self._restore(route)
        else:
            super()._answer(route)
        self._exchange()

    def _exchange(self):
        """The Pi's exchange: the trial it is handed (or the kept values), recorded when it changed."""
        saved = next(frame for frame in self.registry.inventory().frames if frame.id == FRAME)
        active = self.row is not None and self.row["state"] == "active"
        restored = self.row.get("restored_from") if active else None
        trial = TrialHanded(self._values(), UUID(self.row["trial_id"]),
                            None if restored is None else UUID(restored)) if active else None
        with self.registry.db.transaction() as conn:
            record_sent_in(conn, FRAME, trial=trial, saved=position_values(saved.calibration, saved.mat))

    def _restore(self, route):
        body = route.request.post_data_json
        self.sent.append("restore")
        self.bodies.append(body)
        if self.row is None or self.row["state"] != "active":
            route.fulfill(status=410, json={"error": "trial_ended"})
            return
        with self.registry.db.transaction() as conn:
            entry = position_entry_in(conn, UUID(body["entry_id"]))
            made_for = frame_made_for_in(conn, FRAME)
        if entry is None:
            route.fulfill(status=404, json={"error": "unknown_entry"})
            return
        refusal = restore_refusal(entry, frame_id=FRAME, profile_px=made_for.profile_px,
                                  seen_display_id=made_for.display_id)
        if refusal is not None:
            route.fulfill(status=409, json={"error": refusal.value})
            return
        if body["expected_sequence"] != self.row["sequence"]:
            route.fulfill(status=409, json={"error": "trial_sequence_conflict"})
            return
        calibration, mat = restored_draft(entry, base_revision=self.row["calibration_revision"])
        self._draft_mat = mat.model_dump(mode="json")
        self.row.update(sequence=self.row["sequence"] + 1, calibration=calibration.model_dump(mode="json"),
                        restored_from=str(entry.entry_id))
        self._candidate()
        route.fulfill(json=dict(self.row, touched_at=1000.0, hard_expires_at=1000.0 + self.window))


@pytest.fixture
def sessions(page, registry):
    yield HistorySessions(page, registry)
    page.unroute_all(behavior="ignoreErrors")


def _history(page):
    return page.get_by_role("region", name="History", exact=True)


def _rows(page, what):
    """The History's rows of one kind, newest first."""
    return _history(page).get_by_role("listitem").filter(has=page.get_by_text(what, exact=True))


def _history_reads(page):
    """The monotonic times of every History read the page begins (`reads`) and of every Done
    (a trial's save) answered (`done`)."""
    seen = {"reads": [], "done": []}

    def answered(response):
        request = response.request
        if "/calibration-trials" in request.url and request.method == "POST":
            try:
                body = request.post_data_json or {}
            except ValueError:
                body = {}
            if body.get("operation") == "save":
                seen["done"].append(time.monotonic())

    page.on("request", lambda request: seen["reads"].append(time.monotonic())
            if "/position-history" in request.url and request.method == "GET" else None)
    page.on("response", answered)
    return seen


def _done(page):
    bar = _bar(page)
    expect(bar.get_by_role("status")).to_have_text(PRESENTED)
    done = bar.get_by_role("button", name="Done", exact=True)
    expect(done).to_be_enabled()
    done.click()
    expect(page.get_by_role("status").filter(has_text="Saved at")).to_be_visible()


def _depth(page):
    return _mat(page).get_by_role("slider", name="Depth", exact=True)


def _top_left(calibration):
    return round(calibration["fit"]["corners"][0][0] * 1920)


def test_a_tried_and_a_kept_position_are_listed_and_restore_puts_the_kept_one_back_until_done(
        page, registry, sessions):
    """The tracer: move a corner -> Tried, not kept; Done -> Kept (Done); deepen the mat -> Tried;
    Restore an older kept row (the seeded Position: restoring the newest kept one is no change, so
    Done would have nothing to keep) -> the corner and depth return to its values and the Pi is
    handed them; Done -> a new Kept (Done) row."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "position")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(_rows(page, KEPT)).to_have_count(1)  # the seeded Position

        seen = _history_reads(page)
        field = _move(page, 2)
        expect(field).to_have_value(str(SAVED_X + 20))
        # A row says what differs from the row below it: here the moved corner alone.
        expect(_rows(page, TRIED).first).to_contain_text("Top-left")
        expect(_rows(page, TRIED).first).not_to_contain_text("Mat depth")
        _done(page)
        expect(_rows(page, KEPT)).to_have_count(2)
        # Read again as Done is answered, not at the next poll (CADENCE_MS 2000).
        [done] = seen["done"]
        assert any(done <= at <= done + 0.25 for at in seen["reads"]), (done, seen["reads"])

        _depth(page).fill("6")
        expect(_mat(page)).to_contain_text("6.0 mm")
        expect(_rows(page, TRIED).first).to_contain_text("Mat depth 6.0 mm")
        expect(_rows(page, TRIED).first).not_to_contain_text("Top-left")

        _rows(page, KEPT).last.get_by_role("button", name="Restore", exact=True).click()
        expect(_depth(page)).to_have_value("0")
        expect(field).to_have_value(str(SAVED_X))
        page.wait_for_timeout(600)  # the loop's next exchanges: the restored draft stays what the Pi is handed
        assert (_top_left(sessions.row["calibration"]), sessions.row["mat"]["depth_mm"]) == (SAVED_X, 0)
        _done(page)
        expect(_rows(page, KEPT)).to_have_count(3)
        saved = next(frame for frame in registry.inventory().frames if frame.id == FRAME)
        assert (round(saved.calibration.fit.corners[0][0] * 1920), saved.mat.depth_mm) == (SAVED_X, 0)


def test_a_position_made_for_another_screen_size_cannot_be_restored_and_says_why(page, registry, sessions):
    _seed(registry)
    # The seeded Position as one made before the Frame's screen size changed.
    _sql(registry, "UPDATE position_entries SET profile_width_px=3840, profile_height_px=2160 WHERE frame_id=%s",
         (FRAME,))
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "position")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        restore = _rows(page, KEPT).first.get_by_role("button", name="Restore", exact=True)
        expect(restore).to_be_disabled()
        expect(restore).to_have_accessible_description(re.compile(r"Made for another screen size \(3,840 × 2,160 px\)"))


def test_a_restored_draft_keeps_its_base_so_a_save_made_elsewhere_still_stops_it(page, registry, sessions):
    """Another window saves meanwhile (revision 2 -> 3): the restored draft is still the one made from
    revision 2, so Done is refused with the overtaken words, never saved over the other save."""
    from test_console_frame_page_browser import ELSEWHERE_X, _save_elsewhere

    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "position")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        _move(page, 3)
        expect(_rows(page, TRIED)).to_have_count(1)
        _save_elsewhere(registry)

        _depth(page).fill("4")
        expect(_rows(page, TRIED).first).to_contain_text("Mat depth 4.0 mm")
        _rows(page, TRIED).last.get_by_role("button", name="Restore", exact=True).click()
        expect(_depth(page)).to_have_value("0")
        bar = _bar(page)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        bar.get_by_role("button", name="Done", exact=True).click()
        expect(page.get_by_role("alert")).to_contain_text(OVERTAKEN)
        assert round(_saved_mat(registry).depth_mm) == 0
        saved = next(frame for frame in registry.inventory().frames if frame.id == FRAME).calibration
        assert round(saved.fit.corners[0][0] * 1920) == ELSEWHERE_X


def test_restore_waits_for_the_editor_and_says_so(page, registry):
    """No live session (Central's own trial routes, which no Pi presents here): every Restore is
    offered only while the editor runs, and says so."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "position")
        restore = _rows(page, KEPT).first.get_by_role("button", name="Restore", exact=True)
        expect(restore).to_be_disabled()
        expect(restore).to_have_accessible_description("Open while the editor runs")


def test_one_sessions_tries_show_as_one_run_whose_earlier_tries_open_on_request(page, registry, sessions):
    """Rows not kept from one session follow one another as one run (design Q3, "grouped by
    session"): its newest try shows, the earlier ones open on request and can be restored."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "position")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        _move(page, 3)
        expect(_rows(page, TRIED)).to_have_count(1)
        _depth(page).fill("4")
        expect(_rows(page, TRIED).first).to_contain_text("Mat depth 4.0 mm")
        expect(_rows(page, TRIED)).to_have_count(1)
        earlier = _history(page).get_by_role("button", name="Show 1 earlier try from this session")
        expect(earlier).to_have_attribute("aria-expanded", "false")
        earlier.click()
        expect(_rows(page, TRIED)).to_have_count(2)
        expect(_history(page).get_by_role("button", name="Hide 1 earlier try from this session")).to_have_attribute(
            "aria-expanded", "true")
        _rows(page, TRIED).last.get_by_role("button", name="Restore", exact=True).click()
        expect(_depth(page)).to_have_value("0")
