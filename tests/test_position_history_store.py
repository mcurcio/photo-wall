"""A Frame's Position history through the real Registry, trial owner, store and routes
(configuration snapshots, slice S3; owner, 2026-10-13: scope "Everything per Display+Frame", use
"View and restore"; migration 096).

Every trial runs on tests/test_node_calibration.py's rig (`setup_trial`): a Node Pi with node-f0 and
node-f1 bound, both saved once by `save_position` at support.position's PORTRAIT_ON_WIDE (so each begins
with one kept-saved entry). The Node's exchanges are real `NodeDisplay.exchange` calls.
(1) Save appends kept-saved; (2) a turn, and an entry of another box; (3) a change made outside Photo Wall; (4) what
the exchanges hand the Node; (5) Restore; (6) Restore's refusals; (7) the page and time; (8) kept and
sent retention apart; (9) a deleted Frame's history; (10) one transaction.
"""
from __future__ import annotations

import itertools
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from support.position import LIGHTING, PORTRAIT_ON_WIDE, save_position
from test_node_calibration import LIGHTING_JSON, SHIFTED, SKEWED, begin, operate, setup_trial
from test_registry import ADMIN, frame

from central.app import create_app
from central.fleet.node_sessions import NodeControlConfig, NodeControlError
from central.fleet.position_history import (
    PositionEntryKind,
    PositionValues,
    TrialHanded,
    saved_record,
)
from central.infra import position_history_store
from central.infra.position_history_store import record_sent_in, write_position_in
from central.tuning_history import bounds as retention
from contracts.frame_lighting import FrameLighting
from contracts.models import Calibration, PositionBox
from contracts.node_calibration import parse_trial

AUTH = {"Authorization": "Bearer " + ADMIN}
DEEP = LIGHTING.model_copy(update={"mat_depth_mm": 3.0})


class Node:
    """The rig's Node Pi: each exchange a fresh request at a later boot time; `present` answers the
    trial it is handed as shown, so Save may keep it."""

    def __init__(self, registry):
        self.trials, self.request, self._exchange = setup_trial(registry)
        self._times = itertools.count(1500, 100)

    def exchange(self):
        self.request = replace(self.request, request_id=uuid4(), sampled_boottime_ms=next(self._times))
        return self._exchange(self.request)

    def present(self) -> None:
        candidate = parse_trial(self.exchange().trial)
        time = next(self._times)
        self.request = replace(self.request, request_id=uuid4(), sampled_boottime_ms=time, receipt=replace(
            self.request.receipt, sampled_boottime_ms=time, buffer_id=f"weston-{time}", frame_tag=candidate.frame_tag))
        self._exchange(self.request)

    def begin(self, frame_id: str = "node-f0") -> dict:
        return begin(self.trials, frame_id)

    def edit(self, trial: dict, fit, lighting: FrameLighting = LIGHTING) -> dict:
        draft = Calibration(revision=trial["calibration_revision"], fit=fit)
        return operate(self.trials, "node-f0", UUID(trial["trial_id"]), operation="edit",
                                   expected_sequence=trial["sequence"], calibration=draft.model_dump(mode="json"),
                                   lighting=lighting.model_dump(mode="json"))

    def save(self, trial: dict) -> dict:
        self.present()
        return operate(self.trials, "node-f0", UUID(trial["trial_id"]), operation="save",
                                   expected_sequence=trial["sequence"])

    def end(self, trial: dict) -> dict:
        return operate(self.trials, "node-f0", UUID(trial["trial_id"]), operation="end",
                                   expected_sequence=trial["sequence"])

    def restore(self, trial: dict, entry_id, expected: int | None = None) -> dict:
        return self.trials.restore("node-f0", UUID(trial["trial_id"]), entry_id=UUID(str(entry_id)),
                                   expected_sequence=trial["sequence"] if expected is None else expected)


def _client(registry) -> TestClient:
    return TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False,
                                 node_control=NodeControlConfig("node-test")))


def _history(registry, frame_id: str = "node-f0", **params) -> dict:
    with _client(registry) as client:
        answer = client.get(f"/v1/operator/frames/{frame_id}/position-history", headers=AUTH, params=params)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _kinds(registry, frame_id: str = "node-f0") -> list[str]:
    return [entry["kind"] for entry in _history(registry, frame_id)["entries"]]


def _sql(registry, sql: str, params=()) -> list[dict]:
    with registry.db.transaction() as conn:
        cursor = conn.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


def _see_display(registry, frame_id: str = "node-f0") -> UUID:
    """A Display seen on the Frame's port and named by its Position, so the Frame stays ready."""
    display = uuid4()
    _sql(registry, "INSERT INTO displays(id, maker, product, name, serial, first_seen_at, matched_lux, panel_black) "
                   "VALUES (%s, 'SAM', 1, 'Samsung QN43Q8F', %s, 0, 1000.0, 0.0003)", (display, display.hex))
    _sql(registry, "INSERT INTO output_displays(player_id, output_id, connected, display_id, report, reported_at) "
                   "SELECT player_id, output_id, true, %s, '{}', 0 FROM bindings WHERE frame_id=%s "
                   "ON CONFLICT (player_id, output_id) DO UPDATE SET display_id=EXCLUDED.display_id",
         (display, frame_id))
    _sql(registry, "UPDATE frames SET position_display_id=%s WHERE id=%s", (display, frame_id))
    return display


def _entry(registry, *, frame_id: str = "node-f0", kind: str = "kept-saved", display: UUID | None = None,
           fit=SKEWED, turn: int | None = None) -> UUID:
    """An entry Photo Wall wrote at another time (another box, turn or Display), as stored; `turn`
    None: the Frame's turn now."""
    entry = uuid4()
    _sql(registry, "INSERT INTO position_entries(id, frame_id, number, kind, at, fit, lighting, turn, display_id) "
                   "SELECT %s, %s, COALESCE(max(number), 0) + 1, %s, clock_timestamp(), %s, %s, "
                   "COALESCE(%s, (SELECT turn FROM frames WHERE id=%s)), %s FROM position_entries WHERE frame_id=%s",
         (entry, frame_id, kind, fit.model_dump_json(), LIGHTING.model_dump_json(), turn, frame_id, display, frame_id))
    return entry


def _turn(registry, frame_id: str, turn: int) -> None:
    generation = _sql(registry, "SELECT generation FROM frames WHERE id=%s", (frame_id,))[0]["generation"]
    registry.turn_frame(frame_id, turn, expected_generation=generation)


def test_save_appends_a_kept_entry_made_for_the_turn_and_the_display_seen(registry):
    """(1) A new Frame's creation is its first kept entry, and the rig's first Save on it is one
    kept-saved entry, no unrecorded one. Mutation probe P3-no-append: no kept append in
    `write_position_in` -> no new kept-saved."""
    node = Node(registry)
    first = _history(registry)
    assert [(entry["kind"], entry["number"]) for entry in first["entries"]] == [("kept-saved", 2), ("kept-created", 1)]
    assert first["unrecorded_change"] is False and first["frame"]["id"] == "node-f0"
    display = _see_display(registry)
    trial = node.edit(node.begin(), SKEWED, DEEP)
    assert node.save(trial)["state"] == "saved"
    newest = _history(registry)["entries"][0]
    assert (newest["kind"], newest["session_id"], newest["restored_from"]) == ("kept-saved", trial["trial_id"], None)
    assert PositionBox.model_validate(newest["fit"]) == SKEWED and FrameLighting.model_validate(newest["lighting"]) == DEEP
    assert newest["made_for"] == {"turn": 0, "display_id": str(display)}
    assert newest["restore_refusal"] is None


def test_an_entry_kept_before_a_turn_is_refused_made_for_another_turn(registry):
    """(2) fb-13: a box is fractions of the screen as it hangs, so after the Frame's turn the entry
    kept before it is refused made_for_other_turn, and the turn's own entry is not."""
    Node(registry)
    before_turn = _history(registry)["entries"][0]
    assert before_turn["kind"] == "kept-saved"
    _turn(registry, "node-f0", 90)
    turned, kept = _history(registry)["entries"][:2]
    assert (turned["kind"], turned["made_for"]["turn"], turned["restore_refusal"]) == ("kept-turn-changed", 90, None)
    assert (kept["entry_id"], kept["restore_refusal"]) == (before_turn["entry_id"], "made_for_other_turn")


def test_an_old_entry_of_another_box_lists_and_restores_its_box_is_its_shape(registry):
    """(2) fb-13: a box is its own shape, so an old entry of another box (a stored
    kept-profile-changed one, still listed) made for this turn restores as it is. Mutation probe:
    keep the deleted profile comparison as an always-true refusal -> the other box is refused."""
    node = Node(registry)
    trial = node.begin()
    tall = PositionBox(left=0.3, top=0.0, width=0.4, height=1.0)
    old = _entry(registry, kind="kept-profile-changed", fit=tall)
    listed = _history(registry)["entries"][0]
    assert (listed["entry_id"], listed["kind"], listed["restore_refusal"]) == (str(old), "kept-profile-changed", None)
    restored = node.restore(trial, old)
    assert restored["calibration"]["fit"] == tall.model_dump(mode="json") and restored["restored_from"] == str(old)


def test_a_change_made_outside_photo_wall_is_recorded_before_the_next_save(registry):
    """(3) Design rule 3. Mutation probes: P3-no-unrecorded -> no kept-unrecorded;
    P3-default-unrecorded fails the first test's single entry."""
    node = Node(registry)
    _sql(registry, "UPDATE frames SET lighting=%s WHERE id='node-f0'", (DEEP.model_dump_json(),))
    assert _history(registry)["unrecorded_change"] is True
    node.save(node.edit(node.begin(), SHIFTED))
    history = _history(registry)
    saved, unrecorded = history["entries"][:2]
    assert (saved["kind"], unrecorded["kind"], history["unrecorded_change"]) == ("kept-saved", "kept-unrecorded", False)
    assert FrameLighting.model_validate(unrecorded["lighting"]) == DEEP and unrecorded["session_id"] is None
    assert PositionBox.model_validate(unrecorded["fit"]) == PORTRAIT_ON_WIDE.fit  # the rig's saved fit
    assert unrecorded["made_for"] == {"turn": 0, "display_id": None}


def test_a_new_frame_records_its_creation_so_no_change_reads_as_made_outside(registry):
    """A Frame created while the house has no fixed room light starts with no light; its creation is
    its first kept entry, so its first kept write records no change made outside Photo Wall.
    Mutation probe: create_frame without its KEPT_CREATED entry and the old fixed default back in
    `unrecorded_record` -> a kept-unrecorded entry appears."""
    from central.infra.frame_lighting_store import PgHouseLock
    from central.infra.room_light_store import PgRoomLights
    PgRoomLights(registry.db, PgHouseLock()).remove_room_light("room")
    frame(registry, "spare")
    assert _history(registry, "spare")["unrecorded_change"] is False
    _turn(registry, "spare", 90)
    entries = _history(registry, "spare")["entries"]
    assert [entry["kind"] for entry in entries] == ["kept-turn-changed", "kept-created"]
    assert FrameLighting.model_validate(entries[1]["lighting"]).lights == ()


def test_the_kept_write_takes_only_a_kept_kind_and_its_own_values(registry):
    """The one kept Position write refuses what is not a Save (a kept-profile-changed record too:
    nothing writes it since fb-13) and values that are not the calibration it stores; the Frame is
    untouched."""
    Node(registry)
    before = _sql(registry, "SELECT calibration, lighting, generation FROM frames WHERE id='node-f0'")
    saved = saved_record(PositionValues(SKEWED, DEEP), session_id=None, restored_from=None)
    for record, calibration, code in (
            (replace(saved, kind=PositionEntryKind.SENT_DRAFT), Calibration(fit=SKEWED), "position_history_kind"),
            (replace(saved, kind=PositionEntryKind.KEPT_PROFILE_CHANGED), Calibration(fit=SKEWED),
             "position_history_kind"),
            (saved, Calibration(fit=SHIFTED), "position_history_values")):
        with registry.db.transaction() as conn, pytest.raises(ValueError, match=code):
            write_position_in(conn, "node-f0", record, calibration=calibration)
    assert _sql(registry, "SELECT calibration, lighting, generation FROM frames WHERE id='node-f0'") == before


def test_what_each_exchange_hands_the_node_is_recorded_once(registry):
    """(4) Acceptance 4. Mutation probes: P3-every-exchange (append unconditionally) -> the repeated
    exchange appends; P3-new-session-on-renew (session_id = trial_id on renew) -> the renewed draft
    names another session."""
    node = Node(registry)
    trial = node.begin()
    node.exchange()                                                  # the draft is the saved Position
    assert _kinds(registry) == ["kept-saved", "kept-created"]
    trial = node.edit(trial, SKEWED)
    node.exchange()
    node.exchange()
    assert _kinds(registry) == ["sent-draft", "kept-saved", "kept-created"]
    draft = Calibration(revision=trial["calibration_revision"], fit=SHIFTED).model_dump(mode="json")
    renewed = operate(node.trials, "node-f0", UUID(trial["trial_id"]), operation="renew",
                      expected_sequence=trial["sequence"], calibration=draft, lighting=LIGHTING_JSON)
    node.exchange()
    node.end(renewed)
    node.exchange()
    entries = _history(registry)["entries"]
    assert [(entry["kind"], entry["session_id"]) for entry in entries] == [
        ("sent-back", trial["trial_id"]), ("sent-draft", trial["trial_id"]), ("sent-draft", trial["trial_id"]),
        ("kept-saved", None), ("kept-created", None)]
    assert PositionBox.model_validate(entries[0]["fit"]) == PORTRAIT_ON_WIDE.fit   # the saved values back

    lapsed = node.edit(node.begin(), SHIFTED)
    node.exchange()
    registry.clock.advance(6)                                         # past the idle deadline
    node.exchange()
    assert [(entry["kind"], entry["session_id"]) for entry in _history(registry)["entries"][:2]] == [
        ("sent-back", lapsed["trial_id"]), ("sent-draft", lapsed["trial_id"])]

    node.save(node.edit(node.begin(), SKEWED))
    node.exchange()
    assert _kinds(registry)[:2] == ["kept-saved", "sent-draft"]       # after Save, nothing goes back


def test_restore_loads_an_entry_at_the_trials_base_and_save_names_it(registry):
    """(5) Acceptance 5. Mutation probe P3-entry-revision: the draft at the entry's own revision
    (Calibration's default) -> the answer's revision is not the base and Save is refused."""
    node = Node(registry)
    trial = node.edit(node.begin(), SKEWED, DEEP)
    node.exchange()
    skewed = _history(registry)["entries"][0]                         # the draft handed to the Node
    assert skewed["kind"] == "sent-draft"
    trial = node.edit(trial, SHIFTED)
    restored = node.restore(trial, skewed["entry_id"])
    assert restored["sequence"] == trial["sequence"] + 1
    assert restored["calibration"] == Calibration(revision=trial["calibration_revision"], fit=SKEWED).model_dump(
        mode="json")
    assert FrameLighting.model_validate(restored["lighting"]) == DEEP and restored["restored_from"] == skewed["entry_id"]
    assert restored["candidate_sha256"] != trial["candidate_sha256"]
    assert (restored["presented_sequence"], restored["presented_sha256"]) == (None, None)
    with _client(registry) as client:
        answer = client.post(f"/v1/operator/frames/node-f0/calibration-trials/{trial['trial_id']}/restores",
                             headers=AUTH, json={"entry_id": skewed["entry_id"], "expected_sequence": 1})
        assert (answer.status_code, answer.json()["error"]) == (409, "trial_sequence_conflict")
    assert node.save(restored)["state"] == "saved"
    newest = _history(registry)["entries"][0]
    assert (newest["kind"], newest["restored_from"], newest["session_id"]) == (
        "kept-saved", skewed["entry_id"], trial["trial_id"])


def test_an_edit_after_restore_keeps_what_it_was_restored_from(registry):
    """(5) The frozen rule "operate edit keeps restored_from". Mutation probe
    P3-edit-drops-restored-from: an edit clears it -> Save names no entry."""
    node = Node(registry)
    trial = node.edit(node.begin(), SKEWED)
    node.exchange()
    skewed = _history(registry)["entries"][0]
    edited = node.edit(node.restore(trial, skewed["entry_id"]), SHIFTED)
    assert edited["restored_from"] == skewed["entry_id"]
    node.save(edited)
    newest = _history(registry)["entries"][0]
    assert (newest["kind"], newest["restored_from"]) == ("kept-saved", skewed["entry_id"])
    assert newest["fit"] == SHIFTED.model_dump(mode="json")


def test_the_restore_route_answers_the_draft_at_the_trials_base(registry):
    """(5) The HTTP route the Position tab calls, on success: 200 with the entry's fit at the
    trial's base revision, its lighting, the next sequence and the entry it came from. Mutation probes
    P3-route-success-empty and P3-route-unvalidated-sequence fail here."""
    node = Node(registry)
    trial = node.edit(node.begin(), SKEWED, DEEP)
    node.exchange()
    skewed = _history(registry)["entries"][0]
    trial = node.edit(trial, SHIFTED)
    with _client(registry) as client:
        answer = client.post(f"/v1/operator/frames/node-f0/calibration-trials/{trial['trial_id']}/restores",
                             headers=AUTH, json={"entry_id": skewed["entry_id"],
                                                 "expected_sequence": trial["sequence"]})
        invalid = client.post(f"/v1/operator/frames/node-f0/calibration-trials/{trial['trial_id']}/restores",
                              headers=AUTH, json={"entry_id": skewed["entry_id"], "expected_sequence": 0})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["calibration"] == Calibration(revision=trial["calibration_revision"], fit=SKEWED).model_dump(
        mode="json")
    assert FrameLighting.model_validate(body["lighting"]) == DEEP
    assert (body["sequence"], body["restored_from"], body["trial_id"]) == (
        trial["sequence"] + 1, skewed["entry_id"], trial["trial_id"])
    assert (invalid.status_code, invalid.json()["error"]) == (422, "node_trial_operation_invalid")


def test_restore_refusals_in_order(registry):
    """(6) Acceptance 6. Mutation probe P3-no-refusal: skip `restore_refusal` -> the other Frame's
    and the other Display's entries are taken."""
    node = Node(registry)
    seen = _see_display(registry)
    trial = node.begin()
    own = _history(registry)["entries"][0]["entry_id"]
    other_frame = _history(registry, "node-f1")["entries"][0]["entry_id"]
    _sql(registry, "INSERT INTO displays(id, maker, product, name, serial, first_seen_at, matched_lux, panel_black) "
                   "VALUES (%s, 'DEL', 2, 'Dell', 'other', 0, 1000.0, 0.0003)", (dell := uuid4(),))
    other_turn = _entry(registry, turn=180)               # made for the screen hung upside down
    other_display = _entry(registry, display=dell)
    with pytest.raises(NodeControlError) as invalid:
        node.restore(trial, own, expected=0)
    refusals = [(invalid.value.code, invalid.value.status)]
    for trial_id, entry, expected in ((uuid4(), own, 1), (UUID(trial["trial_id"]), uuid4(), 1),
                                      (UUID(trial["trial_id"]), other_frame, 1),
                                      (UUID(trial["trial_id"]), other_turn, 1),
                                      (UUID(trial["trial_id"]), other_display, 1),
                                      (UUID(trial["trial_id"]), own, 7)):
        with pytest.raises(NodeControlError) as refused:
            node.trials.restore("node-f0", trial_id, entry_id=UUID(str(entry)), expected_sequence=expected)
        refusals.append((refused.value.code, refused.value.status))
    assert refusals == [("trial_sequence_invalid", 422), ("trial_unknown", 404), ("unknown_entry", 404),
                        ("other_frame", 409), ("made_for_other_turn", 409), ("made_for_other_display", 409),
                        ("trial_sequence_conflict", 409)]
    assert _history(registry)["entries"][0]["restore_refusal"] == "made_for_other_display"
    assert seen != dell
    node.end(trial)
    with pytest.raises(NodeControlError) as ended:
        node.restore(trial, own)
    assert (ended.value.code, ended.value.status) == ("trial_ended", 410)


def test_a_page_is_bounded_and_times_carry_their_zone(registry):
    """(7) Acceptance 7. Mutation probe P3-naive-time: a naive time read as UTC -> 200."""
    Node(registry)
    save_position(registry, "node-f0", Calibration(fit=SKEWED))
    newest = _history(registry, limit=1)["entries"]
    assert [entry["number"] for entry in newest] == [3]
    assert [entry["number"] for entry in _history(registry, before=newest[0]["at"])["entries"]] == [2, 1]
    with _client(registry) as client:
        for params, status, code in (({"before": "2026-10-13T21:04:07"}, 422, "bad_time"),
                                     ({"limit": 0}, 422, "bad_range"), ({"limit": 501}, 422, "bad_range")):
            answer = client.get("/v1/operator/frames/node-f0/position-history", headers=AUTH, params=params)
            assert (answer.status_code, answer.json()) == (status, {"error": code})
        unknown = client.get("/v1/operator/frames/nope/position-history", headers=AUTH)
        assert (unknown.status_code, unknown.json()) == (404, {"error": "unknown_frame"})


def test_kept_and_sent_entries_are_trimmed_apart(registry, monkeypatch):
    """(8) Acceptance 8. Mutation probe P3-one-trim (one limit for both) -> two entries in all."""
    monkeypatch.setattr(retention, "KEPT_ENTRIES", 2)
    monkeypatch.setattr(retention, "SENT_ENTRIES", 2)
    Node(registry)
    for width in (0.2, 0.22, 0.24):
        save_position(registry, "node-f0", Calibration(fit=PositionBox(width=width, height=0.5)))
    session = uuid4()
    with registry.db.transaction() as conn:
        for depth in (1.0, 2.0, 3.0):
            record_sent_in(conn, "node-f0", trial=TrialHanded(PositionValues(SKEWED, LIGHTING.model_copy(update={"mat_depth_mm": depth})), session,
                                                              None), saved=PositionValues(SKEWED, LIGHTING))
    entries = _history(registry)["entries"]
    assert [(entry["kind"], entry["number"]) for entry in entries] == [
        ("sent-draft", 8), ("sent-draft", 7), ("kept-saved", 5), ("kept-saved", 4)]


def test_a_deleted_frame_takes_its_history(registry):
    """(9) Acceptance 9 (the schema's cascade)."""
    frame(registry, "spare")
    _turn(registry, "spare", 90)
    assert len(_sql(registry, "SELECT id FROM position_entries WHERE frame_id='spare'")) == 2
    with _client(registry) as client:
        assert client.delete("/v1/operator/frames/spare", headers=AUTH).status_code == 200
    assert _sql(registry, "SELECT id FROM position_entries WHERE frame_id='spare'") == []


def test_a_failed_append_fails_the_save(registry, monkeypatch):
    """(10) Acceptance 10: one transaction. Mutation probe P3-swallow: catch the error in Save ->
    the Save is taken."""
    Node(registry)
    before = _sql(registry, "SELECT calibration, lighting FROM frames WHERE id='node-f0'")

    def refuse(conn, frame_id, record):
        raise RuntimeError("history write failed")

    monkeypatch.setattr(position_history_store, "_append_in", refuse)
    with pytest.raises(RuntimeError, match="history write failed"):
        save_position(registry, "node-f0", Calibration(fit=SKEWED), lighting=DEEP)
    assert _sql(registry, "SELECT calibration, lighting FROM frames WHERE id='node-f0'") == before


def test_restore_refuses_an_entry_whose_lighting_names_a_room_light_gone_since(registry):
    """An entry keeps the lighting it recorded, and a room light it names may be removed later (a
    history entry does not hold it): Restore checks the entry's lighting as an edit's, so Save never
    keeps a light that adds nothing. Mutation probe: no `fitted_within` in `restore` -> the gone light
    is loaded into the draft."""
    from central.frame_lighting.model import FrameLightingRefused
    from central.infra.frame_lighting_store import PgHouseLock
    from central.infra.room_light_store import PgRoomLights
    from central.room_light.model import FixedLight
    from contracts.frame_lighting import AT_FULL, WashLight
    from contracts.room_light import FixedColour

    lights = PgRoomLights(registry.db, PgHouseLock())
    lights.save_room_light("spot", FixedLight(name="Spot", colour=FixedColour(kelvin=2700)))
    spotlit = LIGHTING.model_copy(update={"lights": (WashLight(light="spot", lux=500.0, reference=AT_FULL),)})
    node = Node(registry)
    trial = node.edit(node.begin(), SKEWED, spotlit)
    node.exchange()
    tried = _history(registry)["entries"][0]
    assert tried["kind"] == "sent-draft"
    node.end(trial)
    lights.remove_room_light("spot")
    with pytest.raises(FrameLightingRefused) as refused:
        node.restore(node.begin(), tried["entry_id"])
    assert (refused.value.code, refused.value.status) == ("unknown_room_light", 422)
