"""Display tuning on Central (decision 0024, design /display-tuning, slice C1): the Display's signal
range and the picture session's test pattern, end to end through the operator API and the documents
the worker's source projects (owner, 2026-10-13: "can we set the pi to output full range? Can we
make it configurable per display?"; "a button in Central that shows pure white on the frame").

Pure rules first (central/displays/picture.py), then the DB journeys, which reuse the picture
split's fake Pi (tests/test_display_picture_settings.py: `_picture_report` travels the real link
store and its judge). Each test names the acceptance item of the slice it proves and its mutation
probe.
"""
from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from test_display_identity import (
    AUTH,
    NO_SERIAL,
    SERIAL_DISPLAY,
    _bind,
    _frame,
    _report,
    _sql,
    enroll_pi,
)
from test_display_picture_settings import (
    _begin,
    _bound,
    _client,
    _display,
    _done,
    _picture,
    _picture_report,
    _revert,
    _software,
    _view,
)

from central.displays.picture import (
    DisplayPicture,
    PictureRefused,
    PictureSession,
    TestPatternState,
    copied,
    default_picture,
    edited_signal_range,
    edited_test_pattern,
    effective_picture,
    picture_digest,
    picture_document,
    presented,
    reverted,
    session_test_pattern,
    signal_answered,
    test_pattern_state,
)
from central.live_session import Lease
from contracts.node_picture import (
    NEUTRAL_SOFTWARE,
    TEST_PATTERN_SECONDS,
    PictureDocument,
    PictureReport,
    SignalRange,
    SignalReport,
    SignalResult,
    TestPatternAsk,
    encode_picture_document,
)
from contracts.tuning import TestPattern

FULL, LIMITED, AUTO = SignalRange.FULL, SignalRange.LIMITED, SignalRange.AUTO
WHITE = TestPattern.WHITE
STORED = DisplayPicture({}, NEUTRAL_SOFTWARE, AUTO)


def _session(draft: DisplayPicture = STORED, *, idle_until: float = 10.0,
             test_pattern: TestPatternAsk | None = None) -> PictureSession:
    return PictureSession(uuid4(), uuid4(), 1, draft, 0, {}, frozenset(), frozenset(), Lease(idle_until, None),
                          test_pattern)


def _answer(change: int, *, presented_change: int | None = None, signal: SignalReport | None = None,
            shown: TestPatternAsk | None = None) -> PictureReport:
    return PictureReport("HDMI-A-1", NO_SERIAL, None, change, {}, presented_change, signal, shown)


def _signal(registry, change: int, in_force: SignalRange = AUTO, result: SignalResult = SignalResult.APPLIED,
            **report) -> None:
    """The fake Pi's picture report answering `change`, presented, with its signal answer."""
    _picture_report(registry, for_change=change, presented=change, signal=SignalReport(change, in_force, result),
                    **report)


def _range(client, session: dict, signal_range: str, expected: int):
    return client.post(f"/v1/operator/picture-sessions/{session['session_id']}/signal-range-edits", headers=AUTH,
                       json={"signal_range": signal_range, "expected_sequence": expected})


def _pattern(client, session: dict, pattern: str | None, expected: int):
    return client.post(f"/v1/operator/picture-sessions/{session['session_id']}/test-pattern-edits", headers=AUTH,
                       json={"test_pattern": pattern, "expected_sequence": expected})


def _end_lease(registry) -> None:
    _sql(registry, "UPDATE picture_sessions SET idle_until = EXTRACT(EPOCH FROM clock_timestamp()) - 1")


# --- Pure rules ----------------------------------------------------------------------------------

def test_a_new_display_starts_at_auto_and_its_document_carries_no_pattern():
    """(1, pure) Mutation probe: default_picture() returns FULL -> fails."""
    picture = default_picture()
    assert picture.signal_range is AUTO
    document = json.loads(encode_picture_document(picture_document("HDMI-A-1", 1, picture, None)))
    assert (document["signal_range"], document["test_pattern"]) == ("auto", None)


def test_the_range_is_the_drafts_while_live_and_revert_and_the_lease_end_put_the_stored_one_back():
    """(2, pure) Mutation probe: reverted() takes session.draft.signal_range -> fails."""
    session = edited_signal_range(_session(), FULL, expected_sequence=1, now=6.0)
    assert (session.sequence, session.draft.signal_range, session.lease.idle_until > 10.0) == (2, FULL, True)
    assert effective_picture(STORED, session, 6.0).signal_range is FULL
    assert reverted(STORED, session).signal_range is AUTO
    assert effective_picture(STORED, session, 1e9).signal_range is AUTO       # the lease ran out


def test_any_change_of_range_or_pattern_raises_the_digest():
    """Design rule 1: each group, and the session's pattern, moves picture_change."""
    digests = {picture_digest(STORED, None), picture_digest(replace(STORED, signal_range=FULL), None),
               picture_digest(STORED, TestPatternAsk(WHITE, 2)), picture_digest(STORED, TestPatternAsk(WHITE, 3)),
               picture_digest(STORED, TestPatternAsk(TestPattern.GREY_RAMP, 2))}
    assert len(digests) == 5


def test_done_waits_for_any_signal_answer_to_the_change():
    """(3, pure) Mutation probes: A, drop signal_answered from presented -> the None and older
    cases are presented; B, require APPLIED -> the failed and unsupported cases are not."""
    document = PictureDocument("HDMI-A-1", 5, {}, NEUTRAL_SOFTWARE, FULL)
    draft = replace(STORED, signal_range=FULL)
    assert not presented(draft, document, _answer(5, presented_change=5))                     # no signal answer
    older = SignalReport(4, AUTO, SignalResult.APPLIED)
    assert not presented(draft, document, _answer(5, presented_change=5, signal=older))
    assert not signal_answered(document, _answer(5, signal=older))
    for result in SignalResult:
        answer = _answer(5, presented_change=5, signal=SignalReport(5, AUTO, result))
        assert signal_answered(document, answer) and presented(draft, document, answer), result
    assert not presented(STORED, document, _answer(5, presented_change=5, signal=SignalReport(5, FULL,
                                                                                            SignalResult.APPLIED)))


def test_the_pattern_is_the_sessions_ask_never_the_drafts_and_goes_with_the_lease():
    """(4, 5, pure) Mutation probes: project the pattern without checking lease.live -> the
    lease-end assertion fails; ask = 1 constant -> the re-ask has the same ask."""
    asked = edited_test_pattern(_session(), WHITE, expected_sequence=1, now=5.0)
    assert (asked.test_pattern, asked.draft) == (TestPatternAsk(WHITE, 2), STORED)
    assert session_test_pattern(asked, 6.0) == TestPatternAsk(WHITE, 2)
    assert session_test_pattern(asked, 1e9) is None and session_test_pattern(None, 6.0) is None
    off = edited_test_pattern(asked, None, expected_sequence=2, now=6.0)
    again = edited_test_pattern(off, WHITE, expected_sequence=3, now=7.0)
    assert (off.test_pattern, again.test_pattern) == (None, TestPatternAsk(WHITE, 4))
    same = edited_test_pattern(asked, WHITE, expected_sequence=2, now=6.0)
    assert same.test_pattern == TestPatternAsk(WHITE, 3) != asked.test_pattern


def test_the_tab_says_waiting_shown_or_ended():
    """(6) Mutation probe: ENDED without the presented check -> the WAITING case fails."""
    ask = TestPatternAsk(WHITE, 4)
    document = PictureDocument("HDMI-A-1", 7, {}, NEUTRAL_SOFTWARE, AUTO, ask)
    assert test_pattern_state(ask, document, None) is TestPatternState.WAITING
    assert test_pattern_state(ask, document, _answer(7, presented_change=6)) is TestPatternState.WAITING
    assert test_pattern_state(ask, document, _answer(6, presented_change=6)) is TestPatternState.WAITING
    assert test_pattern_state(ask, document, _answer(7, presented_change=7, shown=ask)) is TestPatternState.SHOWN
    assert test_pattern_state(ask, document, _answer(7, presented_change=7)) is TestPatternState.ENDED
    older = TestPatternAsk(WHITE, 2)
    assert test_pattern_state(ask, document, _answer(7, presented_change=6, shown=older)) is TestPatternState.WAITING
    assert test_pattern_state(ask, replace(document, test_pattern=older),
                              _answer(7, presented_change=7)) is TestPatternState.WAITING   # another ask's document


def test_copy_keeps_the_targets_signal_range():
    """(7, pure) Mutation probe: copied() uses source.signal_range -> fails."""
    source = DisplayPicture({}, replace(NEUTRAL_SOFTWARE, saturation_percent=80), FULL)
    result, _skipped = copied(source, DisplayPicture({}, NEUTRAL_SOFTWARE, LIMITED), source_offered=None,
                              target_offered=None)
    assert (result.signal_range, result.software) == (LIMITED, source.software)


def test_the_edits_refuse_like_the_other_edits():
    """(8, pure) Mutation probe: skip the sequence check in edited_signal_range -> the stale case passes."""
    session = _session()
    for edit in (lambda: edited_signal_range(session, FULL, expected_sequence=2, now=5.0),
                 lambda: edited_test_pattern(session, WHITE, expected_sequence=2, now=5.0)):
        with pytest.raises(PictureRefused) as stale:
            edit()
        assert (stale.value.code, stale.value.status) == ("stale_sequence", 409)
    for edit in (lambda: edited_signal_range(session, FULL, expected_sequence=1, now=11.0),
                 lambda: edited_test_pattern(session, WHITE, expected_sequence=1, now=11.0)):
        with pytest.raises(PictureRefused) as ended:
            edit()
        assert (ended.value.code, ended.value.status) == ("session_ended", 410)
    for edit in (lambda: edited_signal_range(session, "studio", expected_sequence=1, now=5.0),
                 lambda: edited_test_pattern(session, "bars", expected_sequence=1, now=5.0)):
        with pytest.raises(PictureRefused) as wrong:
            edit()
        assert (wrong.value.code, wrong.value.status) == ("out_of_range", 422)


# --- Journeys through the operator API and the projected documents (DB) ---------------------------

def test_a_new_display_reads_auto_and_its_document_carries_auto_and_no_pattern(registry):
    """(1) Mutation probe: default_picture() returns FULL -> the Output with no Display reads full;
    the displays column's default is 094's (tests/test_migration_094.py)."""
    player = _bound(registry)
    assert _picture(registry, "pi-picture", "HDMI-A-2").signal_range is AUTO     # no Display seen there
    document = _picture(registry)
    assert (document.signal_range, document.test_pattern) == (AUTO, None)
    assert _display(registry, player)["signal_range"] == "auto"
    with _client(registry) as client:
        view = _view(client)
    assert (view["signal_range"], view["signal"], view["test_pattern"]) == ("auto", None, None)
    assert view["test_pattern_seconds"] == TEST_PATTERN_SECONDS


def test_full_is_live_kept_by_done_and_put_back_by_revert_and_the_lease_end(registry):
    """(2) Q1, R5, R7. Mutation probe: reverted() takes session.draft.signal_range -> Revert leaves
    Full on the document."""
    player = _bound(registry)
    before = _picture(registry)
    with _client(registry) as client:
        session = _begin(client)
        assert _range(client, session, "full", 1).json() == {"sequence": 2}
        full = _picture(registry)
        assert (full.signal_range, full.picture_change > before.picture_change) == (FULL, True)
        assert _display(registry, player)["signal_range"] == "auto"            # live, not kept yet
        _signal(registry, full.picture_change, FULL)
        assert _done(client, session).status_code == 204
        assert _display(registry, player)["signal_range"] == "full"
        assert _picture(registry).signal_range is FULL

        reverting = _begin(client)
        _range(client, reverting, "limited", 1)
        assert _picture(registry).signal_range is LIMITED
        _revert(client, reverting)
        assert _picture(registry).signal_range is FULL

        leaving = _begin(client)
        _range(client, leaving, "auto", 1)
        assert _picture(registry).signal_range is AUTO
        _end_lease(registry)                                                   # leaving the tab
        assert _picture(registry).signal_range is FULL
    assert _display(registry, player)["signal_range"] == "full"


def test_done_waits_for_the_signal_answer_to_the_latest_change(registry):
    """(3) Mutation probes: A, drop signal_answered from presented -> the first Done is taken; B,
    require APPLIED -> the failed answer never lets Done keep the range."""
    player = _bound(registry)
    with _client(registry) as client:
        session = _begin(client)
        before = _picture(registry)
        _signal(registry, before.picture_change)                              # answers the previous change
        _range(client, session, "full", 1)
        _software(client, session, "brightness", 90, 2)
        document = _picture(registry)
        _picture_report(registry, for_change=document.picture_change, presented=document.picture_change, signal=None)
        assert _view(client)["status"] == "waiting"
        early = _done(client, session)
        assert (early.status_code, early.json()) == (409, {"error": "not_presented_yet"})
        _picture_report(registry, for_change=document.picture_change, presented=document.picture_change,
                        signal=SignalReport(before.picture_change, AUTO, SignalResult.APPLIED))
        assert _done(client, session).status_code == 409                       # an older change's answer
        _signal(registry, document.picture_change, AUTO, SignalResult.FAILED)
        view = _view(client)
        assert view["status"] == "shown"
        assert view["signal"] == {"in_force": "auto", "result": "failed", "answered": True}
        assert _done(client, session).status_code == 204
    assert _display(registry, player)["signal_range"] == "full"


def test_the_test_pattern_is_the_sessions_and_done_revert_and_the_lease_end_take_it_off(registry):
    """(4) Mutation probe: project the pattern without checking lease.live -> the lease-end
    document still carries White."""
    player = _bound(registry)
    with _client(registry) as client:
        for end in ("done", "revert", "lease"):
            session = _begin(client)
            asked = _pattern(client, session, "white", 1).json()
            document = _picture(registry)
            assert document.test_pattern == TestPatternAsk(WHITE, asked["sequence"])
            [row] = _sql(registry, "SELECT draft, test_pattern, test_pattern_ask FROM picture_sessions")
            assert "test_pattern" not in row["draft"] and (row["test_pattern"], row["test_pattern_ask"]) == (
                "white", asked["sequence"])
            if end == "done":
                _signal(registry, document.picture_change)
                assert _done(client, session).status_code == 204
            elif end == "revert":
                _revert(client, session)
            else:
                _end_lease(registry)
            off = _picture(registry)
            assert off.test_pattern is None and off.picture_change > document.picture_change, end
    assert "test_pattern" not in _display(registry, player)


def test_asking_again_after_the_pi_ended_it_gives_a_new_ask_and_change(registry):
    """(5, 6) The Pi took White off at its bound: the tab says ended; asking again restarts it.
    Mutation probe: ask = 1 constant -> the second document equals the first and its change stays."""
    _bound(registry)
    with _client(registry) as client:
        session = _begin(client)
        _pattern(client, session, "white", 1)
        first = _picture(registry)
        assert _view(client)["test_pattern"] == {"pattern": "white", "state": "waiting"}
        _signal(registry, first.picture_change, test_pattern_shown=first.test_pattern)
        assert _view(client)["test_pattern"] == {"pattern": "white", "state": "shown"}
        _signal(registry, first.picture_change)                               # off at its bound
        assert _view(client)["test_pattern"] == {"pattern": "white", "state": "ended"}
        assert _pattern(client, session, "white", 2).json() == {"sequence": 3}
        second = _picture(registry)
        assert second.test_pattern == TestPatternAsk(WHITE, 3) != first.test_pattern
        assert second.picture_change > first.picture_change
        assert _view(client)["test_pattern"] == {"pattern": "white", "state": "waiting"}
        assert _pattern(client, session, None, 3).json() == {"sequence": 4}
        assert (_view(client)["test_pattern"], _picture(registry).test_pattern) == (None, None)


def test_copy_never_carries_the_signal_range(registry):
    """(7) Copy follows each TV: the target keeps its own range; Photo Wall's values copy as before.
    Mutation probe: copied() uses source.signal_range -> the target reads full."""
    pi_a, pi_b = enroll_pi(registry, "pi-a"), enroll_pi(registry, "pi-b")
    _bind(registry, _frame(registry, "a"), pi_a, "HDMI-A-1")
    _bind(registry, _frame(registry, "b"), pi_b, "HDMI-A-1")
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    _report(registry, "pi-b", "HDMI-A-1", NO_SERIAL)
    _sql(registry, "UPDATE displays SET signal_range='full', software_saturation_percent=80 WHERE serial IS NOT NULL")
    _sql(registry, "UPDATE displays SET signal_range='limited' WHERE serial IS NULL")
    with _client(registry) as client:
        copied_answer = client.post("/v1/operator/frames/a/picture-copies", headers=AUTH, json={"to_frames": ["b"]})
        assert copied_answer.json() == [{"frame_id": "b", "outcome": "copied", "hardware_skipped": []}]
    target = _display(registry, pi_b)
    assert (target["signal_range"], target["software_saturation_percent"]) == ("limited", 80)


def test_the_edits_refuse_through_the_routes(registry):
    """(8) Mutation probe: skip the sequence check in edited_signal_range -> the stale range edit is taken."""
    _bound(registry)
    with _client(registry) as client:
        session = _begin(client)
        gone = {"session_id": str(uuid4())}
        for edit in (_range(client, gone, "full", 1), _pattern(client, gone, "white", 1)):
            assert (edit.status_code, edit.json()) == (404, {"error": "unknown_session"})
        for edit in (_range(client, session, "full", 2), _pattern(client, session, "white", 2)):
            assert (edit.status_code, edit.json()) == (409, {"error": "stale_sequence"})
        for edit in (_range(client, session, "studio", 1), _pattern(client, session, "bars", 1)):
            assert edit.status_code == 422
        _end_lease(registry)
        for edit in (_range(client, session, "full", 1), _pattern(client, session, "white", 1)):
            assert (edit.status_code, edit.json()) == (410, {"error": "session_ended"})


def test_the_view_shows_the_drafts_range_the_signal_and_the_pattern(registry):
    """(10) Mutation probe: signal_range from the stored row during a live session -> the view
    reads auto while the draft is Full."""
    _bound(registry)
    with _client(registry) as client:
        session = _begin(client)
        _range(client, session, "full", 1)
        _pattern(client, session, "black-level", 2)
        document = _picture(registry)
        _signal(registry, document.picture_change, LIMITED, test_pattern_shown=document.test_pattern)
        view = _view(client)
        assert (view["signal_range"], view["signal"], view["test_pattern"], view["test_pattern_seconds"]) == (
            "full", {"in_force": "limited", "result": "applied", "answered": True},
            {"pattern": "black-level", "state": "shown"}, TEST_PATTERN_SECONDS)
        _software(client, session, "brightness", 90, 3)                      # a newer change, unanswered
        assert _picture(registry).picture_change > document.picture_change
        assert _view(client)["signal"] == {"in_force": "limited", "result": "applied", "answered": False}
        _revert(client, session)
        view = _view(client)
    assert (view["signal_range"], view["test_pattern"]) == ("auto", None)
