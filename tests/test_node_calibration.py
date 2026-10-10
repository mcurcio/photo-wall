"""Trial persistence/Save uses real Registry/Runtime transactions; pixels are separate evidence."""

from dataclasses import replace
from uuid import uuid4

import pytest
from test_node_runtime_reconciliation import rig

from central.fleet.node_calibration import NodeCalibration
from central.fleet.node_display import NodeDisplay
from central.fleet.node_sessions import NodeControlError
from contracts.models import Calibration
from contracts.node_calibration import candidate_hash, canonical, parse_trial
from contracts.node_display import (
    DisplayExchange,
    DisplayReceipt,
    Surface,
    encode_display_exchange,
    parse_display_decision,
)
from player.calibration_trial import TrialState
from player.rendering import OutputComposition


def setup_trial(registry):
    coordinator, _, _, sessions, claims, grants, facts, _, _ = rig(registry)
    fact, claim, grant = facts[0], claims["display_host"], grants["display_host"]
    surface = Surface(
        fact.output, fact.process, fact.app_epoch, fact.binding_generation, fact.config_revision, fact.frame_id
    )
    display = NodeDisplay(sessions, runtime=coordinator)
    trials = NodeCalibration(sessions, display=display, registry=registry)
    display.trials = trials
    receipt = DisplayReceipt(surface, uuid4(), "weston-100", "baseline", 1400)
    request = DisplayExchange(
        grant.producer, uuid4(), 1400, fact.output, True, admitted=surface, receipt=receipt
    )

    def exchange(value):
        return parse_display_decision(
            display.exchange(claim.session_id, claim.credential, encode_display_exchange(value))
        )

    exchange(request)
    return trials, request, exchange


def test_latest_exact_ack_and_atomic_save_retry(registry, monkeypatch):
    trials, request, exchange = setup_trial(registry)
    row = trials.begin("node-f0")
    trial_id = __import__("uuid").UUID(row["trial_id"])
    with pytest.raises(NodeControlError, match="latest_not_presented"):
        trials.operate("node-f0", trial_id, operation="save", expected_sequence=1)
    edited = Calibration.model_validate(row["calibration"]).model_copy(update={"gain": 0})
    row = trials.operate(
        "node-f0",
        trial_id,
        operation="edit",
        expected_sequence=1,
        calibration=edited.model_dump(mode="json"),
    )
    request = replace(request, request_id=uuid4(), sampled_boottime_ms=1500)
    candidate = parse_trial(exchange(request).trial)
    assert candidate.sequence == 2 and candidate.candidate_sha256 == row["candidate_sha256"]
    with pytest.raises(NodeControlError, match="latest_not_presented"):
        trials.operate("node-f0", trial_id, operation="save", expected_sequence=2)
    request = replace(
        request,
        request_id=uuid4(),
        sampled_boottime_ms=1600,
        receipt=replace(
            request.receipt,
            sampled_boottime_ms=1600,
            buffer_id="weston-101",
            frame_tag=candidate.frame_tag,
        ),
    )
    exchange(request)
    original = registry.commit_calibration_trial_in

    def failed(*args, **kwargs):
        raise ValueError("fixture_save_failure")

    monkeypatch.setattr(registry, "commit_calibration_trial_in", failed)
    with pytest.raises(ValueError, match="fixture_save_failure"):
        trials.operate("node-f0", trial_id, operation="save", expected_sequence=2)
    assert (
        trials.operate("node-f0", trial_id, operation="status", expected_sequence=2)["state"]
        == "active"
    )
    monkeypatch.setattr(registry, "commit_calibration_trial_in", original)
    saved = trials.operate("node-f0", trial_id, operation="save", expected_sequence=2)
    assert saved["state"] == "saved" and saved["saved_calibration"]["gain"] == 0
    assert saved["saved_calibration"]["revision"] == edited.revision + 1
    assert (
        trials.operate("node-f0", trial_id, operation="save", expected_sequence=2)["state"]
        == "saved"
    )


def test_local_terminal_expiry_rejects_delayed_trial_and_canonical_output_primitives(registry):
    trials, request, exchange = setup_trial(registry)
    row = trials.begin("node-f0")
    request = replace(request, request_id=uuid4(), sampled_boottime_ms=1500)
    candidate = parse_trial(exchange(request).trial)
    binding = trials.display.installation.configuration_in
    with registry.db.transaction() as conn:
        config = binding(conn, row["player_id"], row["authority_epoch"])["bindings"][0]
    calibration = Calibration(
        revision=config.calibration.revision,
        gain=0,
        corners=((0.4, 0.1), (0.9, 0.3), (0.8, 0.9), (0.1, 0.8)),
    )
    raw = canonical(calibration.model_dump(mode="json"))
    candidate = replace(
        candidate,
        calibration_json=raw,
        candidate_sha256=candidate_hash(
            candidate.trial_id, candidate.generation, candidate.sequence, candidate.baseline, raw
        ),
    )
    from contracts.node_calibration import encode_trial

    state = TrialState()
    applied = state.apply(
        encode_trial(candidate), OutputComposition(config, config.calibration), 1500
    )
    assert applied.calibration.gain == 0
    for observed, expected in zip(applied.primitives, calibration.corners, strict=True):
        assert observed == pytest.approx(expected)
    assert (
        state.apply(
            encode_trial(candidate),
            OutputComposition(config, config.calibration),
            candidate.expires_boottime_ms,
        )
        is None
    )
    # A delayed lease with a later deadline cannot resurrect the terminal generation.
    delayed = replace(candidate, expires_boottime_ms=candidate.hard_expires_boottime_ms)
    assert (
        state.apply(
            encode_trial(delayed),
            OutputComposition(config, config.calibration),
            candidate.expires_boottime_ms + 1,
        )
        is None
    )
    registry.clock.advance(6)
    expired = trials.operate("node-f0", candidate.trial_id, operation="status", expected_sequence=1)
    assert expired["state"] == "expired"


def test_native_capability_survives_session_loss_and_refuses_legacy_write(registry):
    setup_trial(registry)
    assert registry.calibration_capability("node-f0")["mode"] == "native_trial"
    from central.registry import RegistryError
    with registry.db.transaction() as conn:
        conn.execute("UPDATE node_sessions SET revoked_at=999 WHERE owner='display_host'")
    assert registry.calibration_capability("node-f0")["mode"] == "native_trial"
    with pytest.raises(RegistryError, match="calibration_trial_required"):
        registry.calibrate("node-f0", "commit", 2, Calibration(), expected_generation=1)


def test_native_capability_survives_generation_revocation_without_current_boot(registry):
    setup_trial(registry)
    from central.registry import RegistryError

    with registry.db.transaction() as conn:
        conn.execute("UPDATE node_sessions SET revoked_at=999")
        conn.execute("UPDATE node_boot_admissions SET superseded_at=999")
        conn.execute("UPDATE fleet_device_lifecycle SET generation=generation+1")
    assert registry.calibration_capability("node-f0")["mode"] == "native_trial"
    for operation in ("preview", "commit"):
        with pytest.raises(RegistryError, match="calibration_trial_required"):
            registry.calibrate("node-f0", operation, 2, Calibration(), expected_generation=1)


def test_keepalive_slides_the_idle_deadline_up_to_the_hard_one(registry):
    """An open Position or Picture tab keeps its trial alive with `keepalive` (the console's
    Frame page): the idle deadline slides, the candidate and its sequence stay, and the Node is
    handed the later deadline under the same hard one. (The hard deadline ending it is
    `test_keepalive_never_passes_the_hard_deadline`.)

    Mutation: make `keepalive` answer like `status` -> at 8 s the Node is handed no trial."""
    trials, request, exchange = setup_trial(registry)
    row = trials.begin("node-f0")
    trial_id = __import__("uuid").UUID(row["trial_id"])
    began = registry.clock.utc()

    def sample():  # the Node's next display exchange, on its boot clock
        at = 1500 + int((registry.clock.utc() - began) * 1000)
        return parse_trial(exchange(replace(
            request, request_id=uuid4(), sampled_boottime_ms=at,
            receipt=replace(request.receipt, sampled_boottime_ms=at, buffer_id=f"weston-{at}"),
        )).trial)

    first = sample()
    registry.clock.advance(4)
    kept = trials.operate("node-f0", trial_id, operation="keepalive", expected_sequence=1)
    assert kept["state"] == "active" and kept["sequence"] == 1
    assert kept["candidate_sha256"] == row["candidate_sha256"]
    registry.clock.advance(4)  # 8 s since begin: past the 5 s idle window it began with
    later = sample()
    assert later.sequence == 1 and later.candidate_sha256 == first.candidate_sha256
    assert later.expires_boottime_ms > first.expires_boottime_ms
    assert later.hard_expires_boottime_ms == first.hard_expires_boottime_ms
    assert trials.operate("node-f0", trial_id, operation="status", expected_sequence=1)["state"] == "active"


def test_keepalive_never_passes_the_hard_deadline(registry):
    """Keepalives a second apart keep a trial inside its idle window to the end, and the hard
    deadline, fixed at begin, ends it: the last keepalive before it is capped at it.

    Mutation: drop the `min(hard_expires_at, …)` cap from `keepalive` -> the capped deadline
    assertion goes RED (and the trial outlives its hard deadline)."""
    trials, request, exchange = setup_trial(registry)
    row = trials.begin("node-f0")
    trial_id = __import__("uuid").UUID(row["trial_id"])
    began, hard = registry.clock.utc(), row["hard_expires_at"]

    def sample():  # the Node's next display exchange, on its boot clock
        at = 1500 + int((registry.clock.utc() - began) * 1000)
        exchange(replace(
            request, request_id=uuid4(), sampled_boottime_ms=at,
            receipt=replace(request.receipt, sampled_boottime_ms=at, buffer_id=f"weston-{at}"),
        ))

    while registry.clock.utc() + 1 < hard:
        registry.clock.advance(1)
        sample()
        kept = trials.operate("node-f0", trial_id, operation="keepalive", expected_sequence=1)
        assert kept["state"] == "active"
        assert kept["expires_at"] == min(hard, registry.clock.utc() + trials.inactivity_seconds)
    registry.clock.advance(hard - 0.5 - registry.clock.utc())
    sample()
    kept = trials.operate("node-f0", trial_id, operation="keepalive", expected_sequence=1)
    assert kept["state"] == "active" and kept["expires_at"] == hard
    registry.clock.advance(1)
    sample()
    assert trials.operate("node-f0", trial_id, operation="keepalive", expected_sequence=1)["state"] == "expired"


def test_renew_hands_the_draft_to_the_next_trial_in_one_step(registry):
    """The console's handover before a trial's hard deadline: `renew` ends the trial and begins
    the next generation at the operator's draft in one transaction, so the Node's very next
    exchange is handed the draft (never the saved calibration in between) and nothing can
    begin in a gap. A draft whose base is not the saved revision is refused, by `renew` and by
    `begin`, and the trial stays as it was.

    Mutation: have `renew` begin the next trial at the saved calibration -> the Node is handed
    gain 1 -> RED. Mutation: drop the base check in `_open` -> the stale draft is accepted -> RED."""
    trials, request, exchange = setup_trial(registry)
    first = trials.begin("node-f0")
    first_id = __import__("uuid").UUID(first["trial_id"])
    draft = Calibration.model_validate(first["calibration"]).model_copy(update={"gain": 0.5})
    trials.operate("node-f0", first_id, operation="edit", expected_sequence=1,
                   calibration=draft.model_dump(mode="json"))
    with pytest.raises(NodeControlError, match="trial_sequence_conflict"):
        trials.operate("node-f0", first_id, operation="renew", expected_sequence=1,
                       calibration=draft.model_dump(mode="json"))
    second = trials.operate("node-f0", first_id, operation="renew", expected_sequence=2,
                            calibration=draft.model_dump(mode="json"))
    assert second["state"] == "active" and second["trial_id"] != first["trial_id"]
    assert second["generation"] == first["generation"] + 1 and second["sequence"] == 1
    assert second["calibration"]["gain"] == 0.5
    assert second["calibration_revision"] == first["calibration_revision"]
    assert second["hard_expires_at"] == registry.clock.utc() + trials.hard_seconds
    assert trials.operate("node-f0", first_id, operation="status", expected_sequence=2)["state"] == "ended"
    handed = parse_trial(exchange(replace(request, request_id=uuid4(), sampled_boottime_ms=1500)).trial)
    assert str(handed.trial_id) == second["trial_id"] and handed.generation == second["generation"]
    assert Calibration.model_validate_json(handed.calibration_json).gain == 0.5

    second_id = __import__("uuid").UUID(second["trial_id"])
    stale = draft.model_copy(update={"revision": draft.revision + 1})
    with pytest.raises(NodeControlError, match="trial_baseline_revision_changed"):
        trials.operate("node-f0", second_id, operation="renew", expected_sequence=1,
                       calibration=stale.model_dump(mode="json"))
    assert trials.operate("node-f0", second_id, operation="status", expected_sequence=1)["state"] == "active"
    trials.operate("node-f0", second_id, operation="end", expected_sequence=1)
    with pytest.raises(NodeControlError, match="trial_baseline_revision_changed"):
        trials.begin("node-f0", stale.model_dump(mode="json"))
    third = trials.begin("node-f0", draft.model_dump(mode="json"))
    assert third["state"] == "active" and third["calibration"]["gain"] == 0.5


def test_the_operator_routes_begin_at_a_draft_and_renew(registry):
    """Through the real app: `begin` with no body starts at the saved calibration, with
    `{"calibration": draft}` at the draft (a stale base is refused), and the operate route
    takes `renew` with its draft; any other begin field is refused."""
    from fastapi.testclient import TestClient
    from test_registry import ADMIN

    from central.app import create_app
    from central.fleet.node_sessions import NodeControlConfig

    trials, _request, _exchange = setup_trial(registry)
    saved = trials.begin("node-f0")
    trials.operate("node-f0", __import__("uuid").UUID(saved["trial_id"]), operation="end", expected_sequence=1)
    draft = dict(saved["calibration"], gain=0.5)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False,
                     node_control=NodeControlConfig("node-test"))
    path = "/v1/operator/frames/node-f0/calibration-trials"
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + ADMIN}
        stale = client.post(path, headers=headers, json={"calibration": dict(draft, revision=draft["revision"] + 1)})
        assert stale.status_code == 409 and stale.json()["error"] == "trial_baseline_revision_changed"
        assert client.post(path, headers=headers, json={"draft": draft}).status_code == 422
        begun = client.post(path, headers=headers, json={"calibration": draft})
        assert begun.status_code == 200, begun.text
        assert begun.json()["calibration"]["gain"] == 0.5
        renewed = client.post(f"{path}/{begun.json()['trial_id']}", headers=headers,
                              json={"operation": "renew", "expected_sequence": 1, "calibration": draft})
        assert renewed.status_code == 200, renewed.text
        assert renewed.json()["trial_id"] != begun.json()["trial_id"]
        assert renewed.json()["generation"] == begun.json()["generation"] + 1
        client.post(f"{path}/{renewed.json()['trial_id']}", headers=headers,
                    json={"operation": "end", "expected_sequence": 1})
        plain = client.post(path, headers=headers)
        assert plain.status_code == 200, plain.text
        assert plain.json()["calibration"]["gain"] == saved["calibration"]["gain"]
