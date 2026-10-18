"""The configuration snapshot: one Frame at one time, composed from its Position and picture
histories (configuration snapshots, slice S4; owner, 2026-10-13: "configuration snapshots";
design Q1: the agent matches a photo by Central's time, never the phone's).

(1) the tracer: a picture session edit and a Position trial edit on one Frame, read at three times;
(2) with no sent picture entry for the Frame, its seen Display's kept entry; (3) the time and Frame
refusals; (4) `model.snapshot_at`'s refusals, pure; (5) every `at` in UTC.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from support.position import LIGHTING
from test_fleet_attempts import DEVICE_ID
from test_node_calibration import SKEWED
from test_position_history_store import AUTH, Node, _client, _see_display, _sql

from central.db import DatabaseTransactionClock
from central.displays.picture import default_picture
from central.displays.picture_history import (
    PictureEntry,
    PictureEntryKind,
    PictureRecord,
    Port,
    copied_record,
)
from central.displays.views import SoftwareEdit
from central.fleet.position_history import (
    MadeFor,
    PositionEntry,
    PositionEntryKind,
    PositionRecord,
    PositionValues,
)
from central.infra.display_store import PgPictureSessions, output_documents
from central.infra.picture_history_store import append_kept_in
from central.tuning_history.model import snapshot_at
from contracts.models import RectangleFit

AT = datetime(2026, 10, 13, 21, 4, 7, tzinfo=UTC)


def _now(registry) -> datetime:
    return _sql(registry, "SELECT clock_timestamp() AS now")[0]["now"]


def _snapshot(registry, frame_id: str = "node-f0", at=None):
    params = {} if at is None else {"at": at if isinstance(at, str) else at.isoformat()}
    with _client(registry) as client:
        return client.get(f"/v1/operator/frames/{frame_id}/tuning-snapshot", headers=AUTH, params=params)


def _read(registry, at) -> dict:
    answer = _snapshot(registry, at=at)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    for value in (body["at"], *(body[part]["at"] for part in ("position", "picture") if body[part] is not None)):
        assert datetime.fromisoformat(value).utcoffset() == timedelta(0), value      # (5)
    return body


def _project(registry) -> None:
    """The worker's picture projection for the rig's Pi, at Central's time now."""
    with registry.db.transaction() as conn:
        output_documents(conn, DEVICE_ID, DatabaseTransactionClock().now_in(conn))


def test_the_tracer_a_picture_edit_and_a_position_edit_read_back_by_time(registry):
    """(1) Acceptance 1. Mutation probe P4-strict-at: entries strictly before `at` -> the read at the
    Position draft's own time misses it."""
    before_all = _now(registry)
    node = Node(registry)                                            # the rig's Save: a kept Position entry
    display = _see_display(registry)
    pictures = PgPictureSessions(registry.db)
    session = pictures.begin_picture("node-f0")
    pictures.edit_software(session.session_id, SoftwareEdit(setting="contrast", value=114, expected_sequence=1))
    _project(registry)
    between = _now(registry)
    node.edit(node.begin(), SKEWED)
    node.exchange()
    after_both = _now(registry)

    both = _read(registry, after_both)
    assert (both["frame"]["id"], both["position"]["kind"], both["picture"]["kind"]) == (
        "node-f0", "sent-draft", "sent-draft")
    assert both["picture"]["picture"]["software"]["contrast_percent"] == 114
    assert both["picture"]["display"]["id"] == str(display) and both["picture"]["frame"]["id"] == "node-f0"
    assert UUID(both["picture"]["session_id"]) == session.session_id
    assert both["position"]["fit"] == SKEWED.model_dump(mode="json")
    exactly = _read(registry, both["position"]["at"])
    assert exactly["position"]["entry_id"] == both["position"]["entry_id"]
    # The picture's sent entry read at its own time (probe P4-strict-at, picture's sent read).
    assert _read(registry, both["picture"]["at"])["picture"]["entry_id"] == both["picture"]["entry_id"]

    earlier = _read(registry, between)
    assert (earlier["position"]["kind"], earlier["picture"]["kind"]) == ("kept-saved", "sent-draft")
    nothing = _read(registry, before_all)
    assert (nothing["position"], nothing["picture"]) == (None, None)


def test_with_no_sent_entry_for_the_frame_the_seen_displays_kept_entry_answers(registry):
    """(2) Acceptance 2: the Frame's Pi never projected a picture. Mutation probe P4-no-fallback:
    the sent entry or None -> no picture."""
    Node(registry)
    display = _see_display(registry)
    kept = replace(default_picture(), software=replace(default_picture().software, gamma_tenths=24))
    with registry.db.transaction() as conn:
        entry = append_kept_in(conn, display, copied_record(kept))
    body = _read(registry, _now(registry))
    assert (body["picture"]["entry_id"], body["picture"]["kind"], body["picture"]["frame"]) == (
        str(entry.entry_id), "kept-copied", None)
    assert body["picture"]["picture"]["software"]["gamma_tenths"] == 24
    # Read at the kept entry's own time (probe P4-strict-at, picture's kept read).
    assert _read(registry, entry.at)["picture"]["entry_id"] == str(entry.entry_id)


def test_a_snapshot_needs_a_time_with_its_zone_and_a_known_frame(registry):
    """(3) Acceptance 3. Mutation probe P4-naive: a naive `at` read as UTC -> 200."""
    Node(registry)
    for at in (None, "2026-10-13T21:04:07", "soon"):
        answer = _snapshot(registry, at=at)
        assert (answer.status_code, answer.json()) == (422, {"error": "bad_time"})
    unknown = _snapshot(registry, "nope", at="2026-10-13T21:04:07Z")
    assert (unknown.status_code, unknown.json()) == (404, {"error": "unknown_frame"})


def _position(*, frame_id: str = "kitchen", at: datetime = AT) -> PositionEntry:
    return PositionEntry(uuid4(), frame_id, 1, at, MadeFor((1080, 1920), None),
                         PositionRecord(PositionEntryKind.KEPT_SAVED, PositionValues(RectangleFit(), LIGHTING), None, None))


def _picture(kind: PictureEntryKind, *, frame_id: str | None = "kitchen", at: datetime = AT) -> PictureEntry:
    sent = kind.value.startswith("sent-")
    return PictureEntry(uuid4(), uuid4(), 1, at, Port("pi", "HDMI-A-1") if sent else None,
                        frame_id if sent else None, PictureRecord(kind, default_picture(), None, None, None))


def test_snapshot_at_composes_the_sent_picture_over_the_kept_one():
    position, sent = _position(), _picture(PictureEntryKind.SENT_DRAFT)
    kept = _picture(PictureEntryKind.KEPT_DONE)
    assert snapshot_at("kitchen", AT, position=position, picture_sent=sent, picture_kept=kept).picture is sent
    assert snapshot_at("kitchen", AT, position=None, picture_sent=None, picture_kept=kept).picture is kept
    empty = snapshot_at("kitchen", AT, position=None, picture_sent=None, picture_kept=None)
    assert (empty.frame_id, empty.at, empty.position, empty.picture) == ("kitchen", AT, None, None)


def test_snapshot_at_refuses_an_entry_that_does_not_belong():
    """(4) Acceptance 4. Mutation probe P4-wrong-frame-accepted: skip the Frame checks -> another
    Frame's entries compose."""
    later = AT + timedelta(microseconds=1)
    cases = [
        {"position": _position(at=later)},
        {"position": _position(frame_id="hall")},
        {"picture_sent": _picture(PictureEntryKind.SENT_DRAFT, at=later)},
        {"picture_sent": _picture(PictureEntryKind.SENT_BACK, frame_id="hall")},
        {"picture_sent": _picture(PictureEntryKind.KEPT_DONE)},
        {"picture_kept": _picture(PictureEntryKind.SENT_KEPT)},
        {"picture_kept": _picture(PictureEntryKind.KEPT_UNRECORDED, at=later)},
    ]
    for case in cases:
        given = {"position": None, "picture_sent": None, "picture_kept": None, **case}
        with pytest.raises(ValueError, match="snapshot_entry"):
            snapshot_at("kitchen", AT, **given)
