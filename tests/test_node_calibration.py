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
    Frame page): the idle deadline slides, the candidate and its sequence stay, the Node is
    handed the later deadline under the same hard one, and the hard deadline still ends it.

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
    while registry.clock.utc() + 4 < row["hard_expires_at"]:
        assert trials.operate("node-f0", trial_id, operation="keepalive",
                              expected_sequence=1)["state"] == "active"
        registry.clock.advance(4)
        sample()
    registry.clock.advance(4)
    assert trials.operate("node-f0", trial_id, operation="keepalive", expected_sequence=1)["state"] == "expired"
