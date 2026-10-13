"""The sent picture entry the calibration target's QR names (decision 0026, design rule 2: "The photo
names its entry"): GET /v1/operator/players/{player_id}/outputs/{output_id}/picture-changes/{n}.

(1) a target's sent entry reads back by its port and picture change, equal to the same entry in the
Display's picture history; (2) the key is exact: two Pis on the same output id with the same change
each answer their own, and every other key is 404 unknown_entry; (3) the route is admin only.
"""
from __future__ import annotations

from dataclasses import replace

from test_fleet_attempts import DEVICE_ID
from test_position_history_store import AUTH, Node, _client, _see_display, _sql

from central.db import DatabaseTransactionClock
from central.displays.picture import default_picture
from central.displays.picture_history import PictureEntryKind, PictureRecord, Port
from central.displays.views import PictureEntryView, TestPatternEdit
from central.infra.display_store import PgPictureSessions, output_documents
from central.infra.picture_history_store import append_sent_in
from contracts.tuning import TestPattern

OTHER_PI = "pi-other"


def _project(registry) -> None:
    """The worker's picture projection for the rig's Pi, at Central's time now."""
    with registry.db.transaction() as conn:
        output_documents(conn, DEVICE_ID, DatabaseTransactionClock().now_in(conn))


def _entry(client, player_id: str, output_id: str, change: str | int, headers=AUTH):
    return client.get(f"/v1/operator/players/{player_id}/outputs/{output_id}/picture-changes/{change}",
                      headers=headers)


def _target_sent(registry):
    """A live session on the rig's Frame asks the raw screen target; the projection sends it.
    Answers the Display, the port and its picture change."""
    Node(registry)
    display = _see_display(registry)
    pictures = PgPictureSessions(registry.db)
    session = pictures.begin_picture("node-f0")
    pictures.edit_test_pattern(session.session_id, TestPatternEdit(
        test_pattern=TestPattern.TARGET_PRE_SOFTWARE, expected_sequence=session.sequence))
    _project(registry)
    [row] = _sql(registry, "SELECT player_id, output_id, picture_change FROM picture_sent_entries "
                           "WHERE test_pattern='target-pre-software'")
    return display, Port(row["player_id"], row["output_id"]), row["picture_change"]


def test_a_targets_sent_entry_reads_back_by_its_port_and_change(registry):
    """(1) Acceptance 1. Mutation probe K1-number: select by number and Display instead of the port
    key -> a second port's entry on the same Display answers."""
    display, port, change = _target_sent(registry)
    with _client(registry) as client:
        answer = _entry(client, port.player_id, port.output_id, change)
        assert answer.status_code == 200, answer.text
        body = answer.json()
        history = client.get(f"/v1/operator/displays/{display}/picture-history", headers=AUTH).json()
    assert (body["kind"], body["number"], body["test_pattern"]) == ("sent-draft", change, "target-pre-software")
    assert (body["display"]["id"], body["frame"]["id"]) == (str(display), "node-f0")
    [same] = [entry for entry in history["entries"] if entry["entry_id"] == body["entry_id"]]
    assert PictureEntryView.model_validate(body).model_dump() == PictureEntryView.model_validate(same).model_dump()


def test_the_key_is_exact(registry):
    """(2) Acceptance 2: another Pi on the same output id with the same change answers its own entry.
    Mutation probe K1-player: ignore player_id -> one of the two answers the other's."""
    display, port, change = _target_sent(registry)
    _sql(registry, "INSERT INTO players(id, public_key, token_hash, registered_at, last_seen, device_id) "
                   "VALUES (%s, 'other-public-key', 'other-token-hash', 900, 900, %s)", (OTHER_PI, OTHER_PI))
    other_picture = replace(default_picture(), software=replace(default_picture().software, gamma_tenths=26))
    with registry.db.transaction() as conn:
        append_sent_in(conn, Port(OTHER_PI, port.output_id), change, display_id=display, frame_id=None,
                       record=PictureRecord(PictureEntryKind.SENT_BACK, other_picture, None, None, TestPattern.WHITE))
    with _client(registry) as client:
        mine = _entry(client, port.player_id, port.output_id, change).json()
        theirs = _entry(client, OTHER_PI, port.output_id, change).json()
        assert (mine["kind"], mine["test_pattern"], mine["frame"]["id"]) == (
            "sent-draft", "target-pre-software", "node-f0")
        assert (theirs["kind"], theirs["test_pattern"], theirs["frame"]) == ("sent-back", "white", None)
        assert theirs["picture"]["software"]["gamma_tenths"] == 26 and mine["entry_id"] != theirs["entry_id"]
        unknown = [(port.player_id, port.output_id, change + 1), ("nobody", port.output_id, change),
                   (port.player_id, "HDMI-A-9", change),
                   *((port.player_id, port.output_id, text) for text in ("0", "-1", "abc", "01", "+1", " 1",
                                                                          "1.0", str(2**63)))]
        for player_id, output_id, key in unknown:
            answer = _entry(client, player_id, output_id, key)
            assert (answer.status_code, answer.json()) == (404, {"error": "unknown_entry"}), (player_id, output_id, key)


def test_the_entry_is_read_by_the_operator_only(registry):
    """(3) Acceptance 3: no token -> 401 (the guard itself is test_operator_scope's)."""
    _display, port, change = _target_sent(registry)
    with _client(registry) as client:
        assert _entry(client, port.player_id, port.output_id, change, headers={}).status_code == 401
