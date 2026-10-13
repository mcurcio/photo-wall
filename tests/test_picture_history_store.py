"""A Display's picture history through the real store and the operator routes (configuration
snapshots, slice S1; owner, 2026-10-13: "configuration snapshots" that keep "a history of what has
been done in the past"; "View and restore"; migration 096).

Every console write goes through the operator API the Picture tab calls; every Pi answer travels
the link store and its report judge (tests/test_display_picture_settings.py `_picture_report`).
(1) Done appends one kept-done entry and the route lists it; (2) Copy appends kept-copied on each
copied target only; (3) a change made outside Photo Wall is recorded before the next kept write;
(4) a kept entry names its LUT's recipe; (5) Restore loads an entry into the draft and Done names
it; (6) Restore's refusals in order; (7) what Restore skips; (9) the page and Central's time;
(10) retention; (11) one transaction; (12) every `at` is UTC.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

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
    _hardware,
    _picture,
    _picture_report,
    _software,
    _view,
)

from central.displays.lut import LUT_RECIPE_ID
from central.displays.picture import DisplayPicture
from central.displays.picture_history import PictureEntryKind, PictureRecord
from central.infra import display_store
from central.infra.picture_history_store import append_kept_in
from central.tuning_history import model as retention
from contracts.lut import LutRef
from contracts.node_picture import NEUTRAL_SOFTWARE, SignalRange
from contracts.tuning import TestPattern

LISTED, UNLISTED = "c" * 64, "d" * 64
OLD_RECIPE = "lut33-old-recipe"


def _present(registry) -> None:
    """The Pi answers and presents the latest picture document."""
    latest = _picture(registry)
    _picture_report(registry, for_change=latest.picture_change, presented=latest.picture_change)


def _keep(client, registry, session: dict) -> None:
    _present(registry)
    answer = _done(client, session)
    assert answer.status_code == 204, answer.text


def _display_id(registry, player: str) -> UUID:
    return _display(registry, player)["id"]


def _history(client, display_id: UUID, **params) -> dict:
    answer = client.get(f"/v1/operator/displays/{display_id}/picture-history", headers=AUTH, params=params)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _kept(history: dict) -> list[dict]:
    """The kept entries of a history answer, in its order (the projection's sent entries are
    tests/test_picture_sent_history.py's)."""
    return [entry for entry in history["entries"] if entry["kind"].startswith("kept-")]


def _restore(client, session: dict, entry_id, expected: int):
    return client.post(f"/v1/operator/picture-sessions/{session['session_id']}/restores", headers=AUTH,
                       json={"entry_id": str(entry_id), "expected_sequence": expected})


def _now(registry) -> datetime:
    return _sql(registry, "SELECT clock_timestamp() AS now")[0]["now"]


def _kept_rows(registry) -> list[dict]:
    return _sql(registry, "SELECT * FROM picture_kept_entries ORDER BY display_id, number")


def _append(registry, display_id: UUID, picture: DisplayPicture) -> UUID:
    """An entry written through the store as Copy writes one (for entries Photo Wall could no longer
    write today: a code since withdrawn, an older recipe)."""
    with registry.db.transaction() as conn:
        return append_kept_in(conn, display_id, PictureRecord(PictureEntryKind.KEPT_COPIED, picture, None, None,
                                                              None)).entry_id


def _list_lut(registry, sha: str, name: str) -> None:
    _sql(registry, "INSERT INTO luts(profile_sha256, name, profile, added_at) VALUES (%s, %s, %s, 1)",
         (sha, name, b"\0" * 128))


def _assert_utc(body: dict) -> None:
    """Acceptance 12: every `at` an answer carries is ISO 8601 with a zero UTC offset."""
    for entry in body["entries"]:
        assert datetime.fromisoformat(entry["at"]).utcoffset() == timedelta(0), entry["at"]


def test_done_appends_one_kept_entry_and_the_display_lists_it(registry):
    """(1) Acceptance 1, and 3's second half (a Display at its defaults with no entries gets no
    unrecorded one). Mutation probe P1-no-append: drop `append_kept_in` from write_picture -> no row."""
    player = _bound(registry)
    display_id = _display_id(registry, player)
    with _client(registry) as client:
        session = _begin(client)
        _hardware(client, session, "10", 46, 1)
        _software(client, session, "brightness", 114, 2)
        _present(registry)
        before = _now(registry)
        assert _done(client, session).status_code == 204
        after = _now(registry)
        history = _history(client, display_id)
    [row] = _kept_rows(registry)
    assert (row["kind"], row["number"], str(row["session_id"]), row["restored_from"]) == (
        "kept-done", 1, session["session_id"], None)
    assert before <= row["at"] <= after
    assert (row["picture"]["hardware_picture"], row["picture"]["software_brightness_percent"],
            row["picture"]["signal_range"], row["lut_recipe_id"]) == ({"10": 46}, 114, "auto", None)
    name = _display(registry, player)["name"]
    assert history["display"] == {"id": str(display_id), "name": name} and history["unrecorded_change"] is False
    [entry] = _kept(history)
    assert (entry["entry_id"], entry["kind"], entry["number"], entry["frame"], entry["test_pattern"]) == (
        str(row["id"]), "kept-done", 1, None, None)
    assert entry["display"] == {"id": str(display_id), "name": name}
    assert entry["picture"]["hardware"] == {"10": 46} and entry["picture"]["software"]["brightness_percent"] == 114
    _assert_utc(history)


def test_copy_appends_a_kept_entry_on_each_copied_target_only(registry):
    """(2) Acceptance 2. Mutation probe P1-copy-all: append for every outcome -> rows for the
    same-display, no-display and session-open targets."""
    pi_a, pi_b = enroll_pi(registry, "pi-a"), enroll_pi(registry, "pi-b")
    _bind(registry, _frame(registry, "a"), pi_a, "HDMI-A-1")
    _bind(registry, _frame(registry, "b"), pi_b, "HDMI-A-1")
    _bind(registry, _frame(registry, "e"), pi_b, "HDMI-A-2")
    _frame(registry, "d")                                                  # never bound: no Display
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    _report(registry, "pi-b", "HDMI-A-1", NO_SERIAL)
    _report(registry, "pi-b", "HDMI-A-2", replace(SERIAL_DISPLAY, serial="CN0F1X7Z"))   # a third Display
    _sql(registry, "UPDATE displays SET software_brightness_percent=130 WHERE serial='CN0F1X7K'")
    with _client(registry) as client:
        _begin(client, "e")                                                # e's Display: session open
        copied = client.post("/v1/operator/frames/a/picture-copies", headers=AUTH,
                             json={"to_frames": ["b", "a", "d", "e"]})
        assert [outcome["outcome"] for outcome in copied.json()] == [
            "copied", "same-display", "no-display", "session-open"]
    [row] = _kept_rows(registry)
    assert (row["display_id"], row["kind"], row["number"], row["session_id"]) == (
        _display_id(registry, pi_b), "kept-copied", 1, None)
    assert row["picture"]["software_brightness_percent"] == 130


def test_a_change_made_outside_photo_wall_is_recorded_before_the_next_kept_write(registry):
    """(3) Design rule 3. Mutation probes: P1-no-unrecorded (skip it) -> one entry;
    P1-default-unrecorded (no default exception) fails the first test's single row."""
    player = _bound(registry)
    display_id = _display_id(registry, player)
    _sql(registry, "UPDATE displays SET software_brightness_percent=120")
    with _client(registry) as client:
        flagged = _history(client, display_id)
        assert (flagged["unrecorded_change"], flagged["entries"]) == (True, [])
        session = _begin(client)
        _software(client, session, "contrast", 130, 1)
        _keep(client, registry, session)
        history = _history(client, display_id)
    assert history["unrecorded_change"] is False
    done, unrecorded = _kept(history)
    assert [(done["kind"], done["number"]), (unrecorded["kind"], unrecorded["number"])] == [
        ("kept-done", 2), ("kept-unrecorded", 1)]
    assert (unrecorded["picture"]["software"]["brightness_percent"], unrecorded["picture"]["software"]["contrast_percent"],
            unrecorded["session_id"]) == (120, 100, None)
    assert (done["picture"]["software"]["brightness_percent"], done["picture"]["software"]["contrast_percent"]) == (
        120, 130)


def test_a_kept_entry_names_its_luts_recipe(registry):
    """(4) Acceptance 4: the LUT's recipe is stored beside the entry; the list names the LUT as the
    LUTs list does today."""
    player = _bound(registry)
    _list_lut(registry, LISTED, "Hahnemuhle Photo Rag")
    with _client(registry) as client:
        session = _begin(client)
        assert client.post(f"/v1/operator/picture-sessions/{session['session_id']}/lut-edits", headers=AUTH,
                           json={"profile_sha256": LISTED, "expected_sequence": 1}).status_code == 200
        _keep(client, registry, session)
        [entry] = _kept(_history(client, _display_id(registry, player)))
    [row] = _kept_rows(registry)
    assert (row["lut_recipe_id"], row["picture"]["software_lut_profile"]) == (LUT_RECIPE_ID, LISTED)
    assert entry["picture"]["lut_recipe_id"] == LUT_RECIPE_ID
    assert entry["picture"]["software"]["lut"] == {"profile_sha256": LISTED, "name": "Hahnemuhle Photo Rag"}


def test_restore_loads_an_entry_into_the_draft_and_done_names_it(registry):
    """(5) Acceptance 5. Mutation probes: P1-draft-unwritten (the session not written) -> the picture
    stays at 80; P1-no-restored-from -> the last kept entry names nothing."""
    player = _bound(registry)
    display_id = _display_id(registry, player)
    with _client(registry) as client:
        first = _begin(client)
        _hardware(client, first, "10", 46, 1)
        _software(client, first, "brightness", 114, 2)
        _keep(client, registry, first)
        [kept] = _kept(_history(client, display_id))
        session = _begin(client)
        _software(client, session, "brightness", 80, 1)
        assert _picture(registry).software.brightness_percent == 80
        answer = _restore(client, session, kept["entry_id"], 2)
        assert answer.status_code == 200, answer.text
        assert answer.json() == {"sequence": 3, "hardware_skipped": [], "lut_skipped": False,
                                 "lut_recipe_changed": False}
        view = _view(client)
        assert view["session"]["sequence"] == 3 and view["software"]["brightness_percent"] == 114
        assert next(row for row in view["hardware"] if row["code"] == "10")["value"] == 46
        document = _picture(registry)
        assert (dict(document.hardware), document.software.brightness_percent) == ({"10": 46}, 114)
        _keep(client, registry, session)
        newest = _kept(_history(client, display_id))[0]
    assert (newest["kind"], newest["restored_from"], newest["session_id"]) == (
        "kept-done", kept["entry_id"], session["session_id"])
    assert _display(registry, player)["software_brightness_percent"] == 114


def test_restore_refusals_in_order(registry):
    """(6) Acceptance 6: unknown_session, unknown_entry, session_ended, stale_sequence, other_display.
    Mutation probe P1-any-display: drop the display check -> another Display's entry is taken."""
    player = _bound(registry)
    other = enroll_pi(registry, "pi-other")
    _bind(registry, _frame(registry, "hall"), other, "HDMI-A-1")
    _report(registry, "pi-other", "HDMI-A-1", SERIAL_DISPLAY)
    own = _append(registry, _display_id(registry, player), DisplayPicture({}, NEUTRAL_SOFTWARE, SignalRange.FULL))
    foreign = _append(registry, _display_id(registry, other), DisplayPicture({}, NEUTRAL_SOFTWARE, SignalRange.FULL))
    with _client(registry) as client:
        unknown = client.post(f"/v1/operator/picture-sessions/{uuid4()}/restores", headers=AUTH,
                              json={"entry_id": str(own), "expected_sequence": 1})
        assert (unknown.status_code, unknown.json()) == (404, {"error": "unknown_session"})
        session = _begin(client)
        refusals = [(_restore(client, session, uuid4(), 1), 404, "unknown_entry"),
                    (_restore(client, session, foreign, 2), 409, "stale_sequence"),
                    (_restore(client, session, foreign, 1), 409, "other_display")]
        _sql(registry, "UPDATE picture_sessions SET idle_until = EXTRACT(EPOCH FROM clock_timestamp()) - 1")
        refusals += [(_restore(client, session, uuid4(), 9), 404, "unknown_entry"),
                     (_restore(client, session, foreign, 9), 410, "session_ended")]
        for answer, status, code in refusals:
            assert (answer.status_code, answer.json()) == (status, {"error": code})
    assert _sql(registry, "SELECT restored_from FROM picture_sessions") == [{"restored_from": None}]


def test_restore_skips_what_is_no_longer_offered_or_listed_and_keeps_the_test_pattern(registry):
    """(7) Acceptance 7. Mutation probes: P1-no-skip (keep every code) -> "16" reaches the document;
    P1-lut-recipe-kept -> the draft's LUT names the old recipe."""
    player = _bound(registry)
    display_id = _display_id(registry, player)
    _list_lut(registry, LISTED, "Listed paper")
    withdrawn = _append(registry, display_id, DisplayPicture(            # "16" is not offered (OFFERED)
        {"10": 46, "16": 3}, replace(NEUTRAL_SOFTWARE, lut=LutRef(UNLISTED, OLD_RECIPE), brightness_percent=90),
        SignalRange.AUTO))
    older = _append(registry, display_id, DisplayPicture(
        {}, replace(NEUTRAL_SOFTWARE, lut=LutRef(LISTED, OLD_RECIPE)), SignalRange.AUTO))
    with _client(registry) as client:
        session = _begin(client)
        assert client.post(f"/v1/operator/picture-sessions/{session['session_id']}/test-pattern-edits", headers=AUTH,
                           json={"test_pattern": "white", "expected_sequence": 1}).status_code == 200
        skipped = _restore(client, session, withdrawn, 2)
        assert skipped.json() == {"sequence": 3, "hardware_skipped": ["16"], "lut_skipped": True,
                                  "lut_recipe_changed": False}
        document = _picture(registry)
        assert (dict(document.hardware), document.software.brightness_percent, document.software.lut) == (
            {"10": 46}, 90, None)                                          # the draft's own LUT: none
        assert document.test_pattern is not None and document.test_pattern.pattern is TestPattern.WHITE
        rendered = _restore(client, session, older, 3)
        assert rendered.json() == {"sequence": 4, "hardware_skipped": [], "lut_skipped": False,
                                   "lut_recipe_changed": True}
        document = _picture(registry)
        assert document.software.lut == LutRef(LISTED, LUT_RECIPE_ID)
        assert document.test_pattern is not None and document.test_pattern.pattern is TestPattern.WHITE


def test_a_page_is_bounded_and_times_carry_their_zone(registry):
    """(9) Acceptance 9. Mutation probe P1-naive-time: accept a naive time as UTC -> 200."""
    player = _bound(registry)
    display_id = _display_id(registry, player)
    for brightness in (90, 95, 105):
        _append(registry, display_id, DisplayPicture({}, replace(NEUTRAL_SOFTWARE, brightness_percent=brightness),
                                                     SignalRange.AUTO))
    with _client(registry) as client:
        newest = _history(client, display_id, limit=2)["entries"]
        assert [entry["number"] for entry in newest] == [3, 2]
        older = _history(client, display_id, before=newest[1]["at"])
        assert [entry["number"] for entry in older["entries"]] == [1]
        _assert_utc(older)
        for params, status, code in (({"before": "2026-10-13T21:04:07"}, 422, "bad_time"),
                                     ({"limit": 0}, 422, "bad_range"), ({"limit": 501}, 422, "bad_range")):
            answer = client.get(f"/v1/operator/displays/{display_id}/picture-history", headers=AUTH, params=params)
            assert (answer.status_code, answer.json()) == (status, {"error": code})
        unknown = client.get(f"/v1/operator/displays/{uuid4()}/picture-history", headers=AUTH)
        assert (unknown.status_code, unknown.json()) == (404, {"error": "unknown_display"})


def test_a_display_keeps_its_newest_kept_entries(registry, monkeypatch):
    """(10) Acceptance 10. Mutation probe P1-no-trim: no DELETE -> five entries."""
    monkeypatch.setattr(retention, "KEPT_ENTRIES", 3)
    player = _bound(registry)
    with _client(registry) as client:
        for brightness in (101, 102, 103, 104, 105):
            session = _begin(client)
            _software(client, session, "brightness", brightness, 1)
            _keep(client, registry, session)
        entries = _kept(_history(client, _display_id(registry, player)))
    assert [(entry["number"], entry["picture"]["software"]["brightness_percent"]) for entry in entries] == [
        (5, 105), (4, 104), (3, 103)]


def test_a_failed_append_fails_the_done(registry, monkeypatch):
    """(11) Acceptance 11: one transaction. Mutation probe P1-swallow: catch the error in
    write_picture -> the Done is taken and the Display stores 130."""
    player = _bound(registry)

    def refuse(conn, display_id, record):
        raise RuntimeError("history write failed")

    with _client(registry) as client:
        session = _begin(client)
        _software(client, session, "contrast", 130, 1)
        _present(registry)
        monkeypatch.setattr(display_store, "append_kept_in", refuse)
        with pytest.raises(RuntimeError, match="history write failed"):
            _done(client, session)
    assert _display(registry, player)["software_contrast_percent"] == 100
    assert [str(row["id"]) for row in _sql(registry, "SELECT id FROM picture_sessions")] == [session["session_id"]]
    assert _kept_rows(registry) == []
