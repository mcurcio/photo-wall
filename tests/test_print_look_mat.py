"""The Frame's mat through Position's trial (print look, slice C3; owner, 2026-10-12: "the mat shadow
is definitely a Frame quality"; design R5, R6).

Real Registry and Runtime transactions, as tests/test_node_calibration.py runs them: a trial begins
at the saved mat, an edit carries the whole draft mat and its hash covers it, so the Player's
presented tag proves the mat; Save writes it to the Frame's binding with the calibration, never
lowering readiness and never changing the Frame output.
"""
from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb
from test_node_calibration import setup_trial

from central.displays.model import Readiness
from central.fleet.node_sessions import NodeControlError
from central.infra.display_store import frame_outputs_in
from contracts.models import Calibration, Light, Mat
from contracts.node_calibration import canonical, parse_trial

FRAME = "node-f0"
TOP_LIGHT = {"from_degrees": 0, "steep_degrees": 30, "softness_degrees": 10, "strength_tenths": 20,
             "colour_kelvin": 3000}
DRAFT_MAT = {"depth_mm": 2.0, "lights": [TOP_LIGHT]}


def _present(request, exchange, sampled: int):
    """The Node's next exchange is handed the trial; the one after reports it presented (its frame
    tag on the receipt). The candidate it was handed."""
    request = replace(request, request_id=uuid4(), sampled_boottime_ms=sampled)
    candidate = parse_trial(exchange(request).trial)
    exchange(replace(request, request_id=uuid4(), sampled_boottime_ms=sampled + 100, receipt=replace(
        request.receipt, sampled_boottime_ms=sampled + 100, buffer_id=f"weston-{sampled}",
        frame_tag=candidate.frame_tag)))
    return candidate


def _binding(trials, row):
    with trials.registry.db.transaction() as conn:
        return trials.display.installation.configuration_in(conn, row["player_id"], row["authority_epoch"])[
            "bindings"][0]


def _frame(registry):
    return next(frame for frame in registry.inventory().frames if frame.id == FRAME)


def test_a_mat_edited_in_the_trial_is_presented_and_saved_to_the_binding(registry):
    """R5, the tracer: begin, edit with the calibration and a mat (depth 2, one light from 12
    o'clock), the Player presents the trial's tag, Save: frames.mat holds it, the calibration
    revision rises by one, the binding carries it, the operator inventory shows it, and the Frame
    stays READY. Mutation probe: commit without writing frames.mat -> the binding's mat stays Mat()."""
    trials, request, exchange = setup_trial(registry)
    assert _frame(registry).readiness is Readiness.READY and _frame(registry).mat == Mat()
    row = trials.begin(FRAME)
    trial_id = UUID(row["trial_id"])
    revision = row["calibration_revision"]
    edited = trials.operate(FRAME, trial_id, operation="edit", expected_sequence=1,
                            calibration=row["calibration"], mat=DRAFT_MAT)
    assert edited["mat"] == DRAFT_MAT
    candidate = _present(request, exchange, 1500)
    assert candidate.lighting_json == canonical(DRAFT_MAT) and candidate.sequence == 2
    saved = trials.operate(FRAME, trial_id, operation="save", expected_sequence=2)
    assert saved["state"] == "saved" and saved["saved_mat"] == DRAFT_MAT
    assert saved["saved_calibration"]["revision"] == revision + 1

    expected = Mat(depth_mm=2.0, lights=(Light(**TOP_LIGHT),))
    [stored] = _sql(registry, "SELECT mat FROM frames WHERE id=%s", (FRAME,))
    assert Mat.model_validate(stored["mat"]) == expected
    assert _binding(trials, row).mat == expected
    assert _frame(registry).mat == expected and _frame(registry).readiness is Readiness.READY


def test_a_mat_only_edit_changes_the_candidate_so_save_waits_for_its_tag(registry):
    """The trial proves the mat: an edit that changes only the mat changes candidate_sha256 and
    clears the presentation, so Save waits for the new tag; the envelope the Node is handed carries
    the draft mat. Mutation probe: hash without the mat -> the sha is unchanged and Save passes on
    the old presentation."""
    trials, request, exchange = setup_trial(registry)
    row = trials.begin(FRAME)
    trial_id = UUID(row["trial_id"])
    _present(request, exchange, 1500)
    assert trials.operate(FRAME, trial_id, operation="status", expected_sequence=1)["presented_sequence"] == 1
    edited = trials.operate(FRAME, trial_id, operation="edit", expected_sequence=1,
                            calibration=row["calibration"], mat=DRAFT_MAT)
    assert edited["calibration"] == row["calibration"]
    assert edited["candidate_sha256"] != row["candidate_sha256"] and edited["presented_sequence"] is None
    with pytest.raises(NodeControlError, match="trial_latest_not_presented"):
        trials.operate(FRAME, trial_id, operation="save", expected_sequence=2)
    candidate = _present(request, exchange, 1700)
    assert (candidate.candidate_sha256, candidate.lighting_json) == (edited["candidate_sha256"], canonical(DRAFT_MAT))
    assert trials.operate(FRAME, trial_id, operation="save", expected_sequence=2)["state"] == "saved"


def test_a_trial_begins_at_the_saved_mat(registry):
    """A begin without a mat starts at frames.mat, and the Node is handed it. Mutation probe: start
    at Mat() -> the draft is the empty mat."""
    trials, request, exchange = setup_trial(registry)
    _sql(registry, "UPDATE frames SET mat=%s WHERE id=%s", (Jsonb(DRAFT_MAT), FRAME))
    row = trials.begin(FRAME)
    assert row["mat"] == DRAFT_MAT
    assert parse_trial(exchange(replace(request, request_id=uuid4(), sampled_boottime_ms=1500)).trial).lighting_json == (
        canonical(DRAFT_MAT))
    trials.operate(FRAME, UUID(row["trial_id"]), operation="end", expected_sequence=1)
    other = {"depth_mm": 0.5, "lights": []}
    assert trials.begin(FRAME, mat=other)["mat"] == other


@pytest.mark.parametrize("operation", ["edit", "renew"])
def test_an_edit_without_a_mat_or_with_one_the_contract_refuses_is_refused(registry, operation):
    """Refusals: no mat -> trial_mat_required; 5 lights, depth 20.5, kelvin 2750 ->
    trial_mat_invalid; nothing is written. Mutation probe: default a missing mat to Mat() -> the
    edit is taken."""
    trials, _request, _exchange = setup_trial(registry)
    row = trials.begin(FRAME)
    trial_id = UUID(row["trial_id"])
    with pytest.raises(NodeControlError, match="trial_mat_required") as refused:
        trials.operate(FRAME, trial_id, operation=operation, expected_sequence=1, calibration=row["calibration"])
    assert refused.value.status == 422
    for bad in ({"depth_mm": 2.0, "lights": [TOP_LIGHT] * 5}, {"depth_mm": 20.5, "lights": []},
                {"depth_mm": 2.0, "lights": [dict(TOP_LIGHT, colour_kelvin=2750)]}):
        with pytest.raises(NodeControlError, match="trial_mat_invalid") as refused:
            trials.operate(FRAME, trial_id, operation=operation, expected_sequence=1,
                           calibration=row["calibration"], mat=bad)
        assert refused.value.status == 422
    with pytest.raises(NodeControlError, match="trial_mat_invalid"):
        trials.begin(FRAME, mat={"depth_mm": 20.5, "lights": []})
    assert _sql(registry, "SELECT trial_id::text, state, sequence, mat FROM node_calibration_trials") == [
        {"trial_id": row["trial_id"], "state": "active", "sequence": 1, "mat": {"depth_mm": 0.0, "lights": []}}]


def test_a_mat_only_save_never_changes_the_frame_output(registry):
    """Never re-plans: the Frame outputs the Planner reads (`frame_outputs_in`, its one input from
    the Frame's Position) are equal before and after a mat-only Save. Mutation probe: route the mat
    through calibration.fit -> the output changes."""
    trials, request, exchange = setup_trial(registry)
    with registry.db.transaction() as conn:
        before = frame_outputs_in(conn)
    row = trials.begin(FRAME)
    trial_id = UUID(row["trial_id"])
    trials.operate(FRAME, trial_id, operation="edit", expected_sequence=1, calibration=row["calibration"],
                   mat=DRAFT_MAT)
    _present(request, exchange, 1500)
    saved = trials.operate(FRAME, trial_id, operation="save", expected_sequence=2)
    assert Calibration.model_validate(saved["saved_calibration"]).fit == Calibration.model_validate(
        row["calibration"]).fit
    with registry.db.transaction() as conn:
        assert frame_outputs_in(conn) == before


def test_the_operator_routes_carry_the_mat(registry):
    """Through the real app: begin takes `mat`, edit takes it beside the calibration, any other key
    is refused; a missing mat on edit answers 422 trial_mat_required."""
    from fastapi.testclient import TestClient
    from test_registry import ADMIN

    from central.app import create_app
    from central.fleet.node_sessions import NodeControlConfig

    setup_trial(registry)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False,
                     node_control=NodeControlConfig("node-test"))
    path = f"/v1/operator/frames/{FRAME}/calibration-trials"
    headers = {"Authorization": "Bearer " + ADMIN}
    with TestClient(app) as client:
        assert client.post(path, headers=headers, json={"shadow": DRAFT_MAT}).status_code == 422
        begun = client.post(path, headers=headers, json={"mat": DRAFT_MAT})
        assert begun.status_code == 200, begun.text
        assert begun.json()["mat"] == DRAFT_MAT
        trial = f"{path}/{begun.json()['trial_id']}"
        missing = client.post(trial, headers=headers, json={
            "operation": "edit", "expected_sequence": 1, "calibration": begun.json()["calibration"]})
        assert (missing.status_code, missing.json()["error"]) == (422, "trial_mat_required")
        edited = client.post(trial, headers=headers, json={
            "operation": "edit", "expected_sequence": 1, "calibration": begun.json()["calibration"],
            "mat": {"depth_mm": 1.5, "lights": []}})
        assert edited.status_code == 200, edited.text
        assert edited.json()["mat"] == {"depth_mm": 1.5, "lights": []}


def _sql(registry, sql: str, params=()) -> list[dict]:
    with registry.db.transaction() as conn:
        cursor = conn.execute(sql, params)
        return cursor.fetchall() if cursor.description else []
