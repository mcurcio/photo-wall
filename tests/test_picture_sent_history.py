"""What Central sent each port, as picture history (configuration snapshots, slice S2; owner,
2026-10-13: "how can we improve the tuning flow so that you can read the values as we go?"; design
rule 1: sent entries are written by the picture projection, `output_documents`, each time a port's
picture_change rises).

Every projection is the worker's document source (tests/test_display_picture_settings.py
`_documents`, `_picture`) or `output_documents` at a chosen Central time; every console write goes
through the operator API; every Pi answer travels the link store and its report judge.
(1) a live session's edits are sent-draft entries; (2) the test pattern rides them; (3) Revert sends
the wall back, Done with a pattern on sends the kept values; (4) a lapse is a back entry on time;
(5) one entry per picture change; (6) no Display, no entry; (7) Copy; (8) the route interleaves kept
and sent entries; (9) retention per port.
"""
from __future__ import annotations

from datetime import UTC, datetime
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
    SERIAL,
    _begin,
    _bound,
    _client,
    _documents,
    _done,
    _hardware,
    _picture,
    _picture_report,
    _software,
)
from test_picture_history_store import _display_id, _history, _present

from central.content_catalog.catalog import device_id_for_serial
from central.db import DatabaseTransactionClock
from central.displays.picture import default_picture
from central.displays.picture_history import PictureEntryKind, PictureRecord, Port
from central.infra.display_store import _until_next_deadline, output_documents
from central.infra.picture_history_store import append_sent_in
from central.tuning_history import model as retention
from contracts.node_picture import HardwareResult, WriteResult


def _sent_rows(registry, output_id: str = "HDMI-A-1") -> list[dict]:
    return _sql(registry, "SELECT * FROM picture_sent_entries WHERE output_id=%s ORDER BY picture_change",
                (output_id,))


def _kinds(registry) -> list[tuple]:
    return [(row["picture_change"], row["kind"], None if row["session_id"] is None else str(row["session_id"]),
             row["test_pattern"]) for row in _sent_rows(registry)]


def _pattern(client, session: dict, pattern: str | None, expected: int):
    answer = client.post(f"/v1/operator/picture-sessions/{session['session_id']}/test-pattern-edits", headers=AUTH,
                         json={"test_pattern": pattern, "expected_sequence": expected})
    assert answer.status_code == 200, answer.text


def test_each_edit_of_a_live_session_is_a_sent_draft_entry(registry):
    """(1) Acceptance 1. Mutation probe P2-kind-from-stored (a draft only when it differs from the
    stored values) -> the edit back to 100 reads as kept."""
    player = _bound(registry)
    display_id = _display_id(registry, player)
    with _client(registry) as client:
        first = _picture(registry)                                   # the Display newly seen: its kept values
        session = _begin(client)
        assert _picture(registry) == first                           # beginning sends nothing new
        _software(client, session, "brightness", 90, 1)
        assert _picture(registry).picture_change == 2
        _software(client, session, "brightness", 100, 2)            # back to the stored value, still a draft
        assert _picture(registry).picture_change == 3
    sid = session["session_id"]
    assert _kinds(registry) == [(1, "sent-kept", None, None), (2, "sent-draft", sid, None),
                                (3, "sent-draft", sid, None)]
    rows = _sent_rows(registry)
    assert {(row["display_id"], row["frame_id"], row["player_id"]) for row in rows} == {(display_id, "kitchen", player)}
    assert [row["picture"]["software_brightness_percent"] for row in rows] == [100, 90, 100]


def test_the_test_pattern_rides_sent_drafts_and_done_sends_the_kept_values(registry):
    """(2) Acceptance 2, and 3's Done: a pattern on, then off, then on again and Done: the pattern is
    a draft's only; Done with a pattern on changes the document, and that entry is sent-kept.
    Mutation probe P2-no-pattern: drop the test pattern -> None where white is expected."""
    _bound(registry)
    with _client(registry) as client:
        _picture(registry)
        session = _begin(client)
        _pattern(client, session, "white", 1)
        _picture(registry)
        _pattern(client, session, None, 2)
        _picture(registry)
        _pattern(client, session, "grey-ramp", 3)
        _present(registry)                                          # projects, then the Pi presents it
        assert _done(client, session).status_code == 204
        _picture(registry)
    sid = session["session_id"]
    assert _kinds(registry) == [(1, "sent-kept", None, None), (2, "sent-draft", sid, "white"),
                                (3, "sent-draft", sid, None), (4, "sent-draft", sid, "grey-ramp"),
                                (5, "sent-kept", None, None)]


def test_revert_sends_the_wall_back_with_the_displays_own_value(registry):
    """(3) Acceptance 3: Revert of a draft that differs from the kept values: the next projection
    is sent-back, naming the draft's session, with the values sent (the kept ones, and brightness put
    back at the Pi's first before). Mutation probe P2-always-kept -> sent-kept."""
    _bound(registry)
    with _client(registry) as client:
        _picture(registry)
        session = _begin(client)
        _hardware(client, session, "10", 46, 1)
        sent = _picture(registry)
        _picture_report(registry, for_change=sent.picture_change, presented=sent.picture_change,
                        results={"10": HardwareResult(WriteResult.TAKEN, 60, 46)})
        assert client.delete(f"/v1/operator/picture-sessions/{session['session_id']}", headers=AUTH).status_code == 204
        back = _picture(registry)
    assert dict(back.hardware) == {"10": 60}
    assert _kinds(registry)[-1] == (3, "sent-back", session["session_id"], None)
    last = _sent_rows(registry)[-1]
    assert (last["picture"]["hardware_picture"], last["picture"]["software_brightness_percent"]) == ({"10": 60}, 100)


def test_a_lapsed_session_is_a_back_entry_on_time(registry):
    """(4) Acceptance 4: the projection at Central's time past the lease's end sends the wall back,
    and the source wakes no later than that end. Mutation probe P2-lease-ignored: any present
    session is live -> sent-draft."""
    _bound(registry)
    device_id = device_id_for_serial(SERIAL)
    with _client(registry) as client:
        _picture(registry)
        session = _begin(client)
        _software(client, session, "brightness", 90, 1)
        _picture(registry)
    [lease] = _sql(registry, "SELECT idle_until FROM picture_sessions")
    with registry.db.transaction() as conn:
        now = DatabaseTransactionClock().now_in(conn)
        assert _until_next_deadline(conn, device_id, now) <= lease["idle_until"] - now
        output_documents(conn, device_id, lease["idle_until"] + 1)
    assert _kinds(registry)[-1] == (3, "sent-back", session["session_id"], None)
    assert _sent_rows(registry)[-1]["picture"]["software_brightness_percent"] == 100


def test_one_entry_per_picture_change(registry):
    """(5) Acceptance 5: a projection whose digest did not change appends nothing, and an append for
    a change the port already has is no second entry. Mutation probe P2-no-conflict-clause: drop
    ON CONFLICT DO NOTHING -> unique violation."""
    player = _bound(registry)
    display_id = _display_id(registry, player)
    _picture(registry)
    _picture(registry)
    assert len(_sent_rows(registry)) == 1
    record = PictureRecord(PictureEntryKind.SENT_KEPT, default_picture(), None, None, None)
    with registry.db.transaction() as conn:
        append_sent_in(conn, Port(player, "HDMI-A-1"), 1, display_id=display_id, frame_id="kitchen", record=record)
        with pytest.raises(ValueError, match="picture_history_kind"):
            append_sent_in(conn, Port(player, "HDMI-A-1"), 2, display_id=display_id, frame_id="kitchen",
                           record=PictureRecord(PictureEntryKind.KEPT_DONE, default_picture(), uuid4(), None, None))
    assert len(_sent_rows(registry)) == 1


def test_an_output_with_no_display_seen_appends_nothing(registry):
    """(6) Acceptance 6: the Pi's second port has no Display seen; the projection still answers it.
    Mutation probe P2-no-guard: drop the `_no_display` guard -> the append fails the projection."""
    _bound(registry)
    _output, picture = _documents(registry, output_id="HDMI-A-2")
    assert picture.picture_change == 1
    assert _sent_rows(registry, "HDMI-A-2") == []


def test_copy_onto_a_seen_display_is_sent_as_its_kept_values(registry):
    """(7) Acceptance 7. Mutation probe P2-copy-unseen: no entry for a change made with no session ->
    nothing after the Copy."""
    pi_a, pi_b = enroll_pi(registry, "pi-a"), enroll_pi(registry, "pi-b")
    _bind(registry, _frame(registry, "a"), pi_a, "HDMI-A-1")
    _bind(registry, _frame(registry, "b"), pi_b, "HDMI-A-1")
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    _report(registry, "pi-b", "HDMI-A-1", NO_SERIAL)
    _sql(registry, "UPDATE displays SET software_brightness_percent=130 WHERE serial IS NOT NULL")
    _picture(registry, "pi-b")
    with _client(registry) as client:
        assert client.post("/v1/operator/frames/a/picture-copies", headers=AUTH,
                           json={"to_frames": ["b"]}).json()[0]["outcome"] == "copied"
    _picture(registry, "pi-b")
    rows = _sql(registry, "SELECT * FROM picture_sent_entries WHERE player_id=%s ORDER BY picture_change", (pi_b,))
    assert [(row["picture_change"], row["kind"], row["frame_id"], row["picture"]["software_brightness_percent"])
            for row in rows] == [(1, "sent-kept", "b", 100), (2, "sent-kept", "b", 130)]


def test_the_history_interleaves_kept_and_sent_entries_newest_first(registry):
    """(8) Acceptance 8. Mutation probe P2-kept-only: read picture_kept_entries alone -> no sent
    entries listed."""
    player = _bound(registry)
    [frame] = _sql(registry, "SELECT name FROM frames WHERE id='kitchen'")
    with _client(registry) as client:
        _picture(registry)
        session = _begin(client)
        _software(client, session, "contrast", 130, 1)
        _present(registry)
        assert _done(client, session).status_code == 204
        history = _history(client, _display_id(registry, player))
    entries = history["entries"]
    assert [(entry["kind"], entry["number"]) for entry in entries] == [
        ("kept-done", 1), ("sent-draft", 2), ("sent-kept", 1)]
    assert [entry["frame"] for entry in entries] == [None, {"id": "kitchen", "name": frame["name"]},
                                                     {"id": "kitchen", "name": frame["name"]}]
    times = [datetime.fromisoformat(entry["at"]) for entry in entries]
    assert times == sorted(times, reverse=True) and all(time.tzinfo is not None for time in times)
    assert all(time.utcoffset() == UTC.utcoffset(None) for time in times)


def test_a_port_keeps_its_newest_sent_entries(registry, monkeypatch):
    """(9) Acceptance 9. Mutation probe P2-no-trim: no DELETE -> six entries."""
    monkeypatch.setattr(retention, "SENT_ENTRIES", 3)
    _bound(registry)
    with _client(registry) as client:
        _picture(registry)
        session = _begin(client)
        for sequence, brightness in enumerate((91, 92, 93, 94, 95), start=1):
            _software(client, session, "brightness", brightness, sequence)
            _picture(registry)
    assert [(row["picture_change"], row["picture"]["software_brightness_percent"]) for row in _sent_rows(registry)] == [
        (4, 93), (5, 94), (6, 95)]
