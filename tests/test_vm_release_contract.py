"""Release evidence fails closed across the fixture/host serialization boundary."""

import json
import sys

import pytest

from scripts.vm_release_contract import (
    MAX_RELEASE_RESULT_BYTES,
    CentralBootEvidence,
    ReleaseEvidenceResult,
    ReleaseFailureResult,
    ReleaseProbeFailure,
    ReleaseStageResult,
    decode_release_result,
    trusted_release_failure,
)


def evidence(**changes):
    return dict(device_id="device-" + "d" * 64,
        boot_id="01234567-89ab-cdef-0123-456789abcdef", ticket_sha256="a" * 64,
        release_id="b" * 64, trial=True, status="failed", accepted_release_id="c" * 64,
        candidate_release_id="b" * 64, current=False, current_player_id="p-" + "e" * 32,
        current_authority_epoch=3, trial_ticket_sha256="a" * 64) | changes


def encoded(value):
    return json.dumps(value).encode()


def result(**changes):
    return dict(schema_version=1, kind="release-evidence", evidence=evidence()) | changes


def test_round_trip_preserves_consumption_and_current_session_separately():
    parsed = decode_release_result("evidence", encoded(result()))
    assert isinstance(parsed, ReleaseEvidenceResult)
    assert parsed.evidence.trial_ticket_sha256 == parsed.evidence.ticket_sha256
    assert parsed.evidence.current_authority_epoch == 3
    assert parsed.model_dump() == result()
    assert decode_release_result("evidence", encoded(result(evidence=None))).evidence is None


@pytest.mark.parametrize("field", list(CentralBootEvidence.model_fields))
def test_every_boot_field_is_required_including_nullable_consumption(field):
    value = evidence()
    del value[field]
    with pytest.raises(ValueError):
        decode_release_result("evidence", encoded(result(evidence=value)))


@pytest.mark.parametrize("changes", [
    dict(trial="true"), dict(current=0), dict(current_authority_epoch=True),
    dict(current_authority_epoch=3.0), dict(current_authority_epoch="3"),
    dict(current_authority_epoch=0), dict(current_player_id=None),
    dict(current_authority_epoch=None), dict(device_id="device-other"),
    dict(boot_id="not-a-boot"), dict(release_id="b" * 63), dict(status="unknown"),
    dict(trial_ticket_sha256=None), dict(trial_ticket_sha256="f" * 64),
    dict(trial=False), dict(current=True), dict(ticket_id="private-capability"),
    dict(player_id="p-" + "e" * 32),
])
def test_malformed_or_inconsistent_boot_data_never_becomes_evidence(changes):
    with pytest.raises(ValueError):
        decode_release_result("evidence", encoded(result(evidence=evidence(**changes))))


@pytest.mark.parametrize("changes", [
    dict(schema_version=True), dict(schema_version="1"), dict(schema_version=2),
    dict(kind="release-staged"), dict(unexpected="private-data"),
])
def test_evidence_envelope_is_closed_and_strict(changes):
    with pytest.raises(ValueError):
        decode_release_result("evidence", encoded(result(**changes)))


@pytest.mark.parametrize("model,action,value", [
    (ReleaseEvidenceResult, "evidence", result()),
    (ReleaseStageResult, "stage", dict(schema_version=1, kind="release-staged", staged=True, release_id="a" * 64)),
])
def test_action_envelopes_require_every_field(model, action, value):
    assert isinstance(decode_release_result(action, encoded(value)), model)
    for field in model.model_fields:
        incomplete = value.copy()
        del incomplete[field]
        with pytest.raises(ValueError):
            decode_release_result(action, encoded(incomplete))


@pytest.mark.parametrize("staged", [False, 1, "true"])
def test_stage_requires_an_actual_success_boolean(staged):
    with pytest.raises(ValueError):
        decode_release_result("stage", encoded(dict(schema_version=1, kind="release-staged",
            staged=staged, release_id="a" * 64)))


@pytest.mark.parametrize("raw", [
    b"null", b"[]", b"{}", b'{"error":"release_probe_failed"}', b"not-json",
    b" " * (MAX_RELEASE_RESULT_BYTES + 1),
    b'{"schema_version":2,"schema_version":1,"kind":"release-evidence","evidence":null}',
])
def test_ambiguous_unbounded_and_untyped_results_fail(raw):
    with pytest.raises(ValueError):
        decode_release_result("evidence", raw)


def test_accepted_release_may_retain_an_earlier_promoted_trial_consumption():
    # Nontrial attempts may observe an earlier consumed trial for the same
    # accepted release. It must never be misattributed to the nontrial attempt.
    value = evidence(trial=False, status="healthy", current=True,
        accepted_release_id="b" * 64, candidate_release_id=None, trial_ticket_sha256="f" * 64)
    assert decode_release_result("evidence", encoded(result(evidence=value))).evidence.trial is False


def failure(**changes):
    return dict(schema_version=1, kind="release-failure", role="observer", action="evidence",
        stage="result", code="release_probe_evidence_invalid") | changes


def probe_args():
    boot = evidence()
    return ["docker", "exec", "pw-boot-" + "a" * 16 + "-observer", "python", "-m",
        "scripts.vm_release_probe", "evidence", "--device-id", boot["device_id"], "--boot-id", boot["boot_id"]]


def test_failure_envelope_requires_every_field_and_never_counts_as_success():
    value = failure()
    parsed = trusted_release_failure(probe_args(), encoded(value))
    assert isinstance(parsed, ReleaseProbeFailure)
    assert parsed.failure.model_dump() == value
    for field in ReleaseFailureResult.model_fields:
        incomplete = value.copy()
        del incomplete[field]
        assert trusted_release_failure(probe_args(), encoded(incomplete)) is None
    with pytest.raises(ValueError):
        decode_release_result("evidence", encoded(value))


@pytest.mark.parametrize("changes", [dict(schema_version=True), dict(kind="untrusted"),
    dict(role="worker"), dict(role="central"), dict(action="stage"), dict(stage="query"), dict(code="private-failure"),
    dict(detail="private diagnostic"), dict(schema_version=2)])
def test_malformed_failure_metadata_is_discarded(changes):
    assert trusted_release_failure(probe_args(), encoded(failure(**changes))) is None


@pytest.mark.parametrize("change", ["unrelated_command", "unrelated_container", "other_role",
    "old_central", "other_module", "other_action", "extra_arg", "bad_device", "bad_boot"])
def test_unrelated_command_cannot_supply_failure_metadata(change):
    args = probe_args()
    if change == "unrelated_command":
        args[1] = "logs"
    elif change == "unrelated_container":
        args[2] = "unrelated-central"
    elif change == "other_role":
        args[2] = args[2].replace("observer", "worker")
    elif change == "old_central":
        args[2] = args[2].replace("observer", "central")
    elif change == "other_module":
        args[5] = "scripts.vm_inventory_probe"
    elif change == "other_action":
        args[6] = "stage"
    elif change == "extra_arg":
        args += ["--unrelated"]
    elif change == "bad_device":
        args[8] = "other-device"
    else:
        args[10] = "other-boot"
    assert trusted_release_failure(args, encoded(failure())) is None


@pytest.mark.parametrize("trusted", [True, False])
def test_real_nonzero_command_attaches_only_exact_helper_failure(monkeypatch, trusted):
    from scripts import boot_fixture

    args = probe_args()
    if not trusted:
        args[2] = "unrelated-central"
    payload = json.dumps(failure())
    monkeypatch.setattr(boot_fixture, "docker_debug_args", lambda _: [sys.executable, "-c",
        f"import sys; print({payload!r}); sys.exit(1)"])
    monkeypatch.setattr(boot_fixture, "record_docker_debug", lambda *args: None)
    with pytest.raises(boot_fixture.FixtureError) as raised:
        boot_fixture.command(args, timeout=5)
    if trusted:
        assert str(raised.value) == "release_probe_failed"
        assert isinstance(raised.value.__cause__, ReleaseProbeFailure)
        assert raised.value.__cause__.command == tuple(args)
    else:
        assert str(raised.value) == "docker_command_failed"
        assert raised.value.__cause__ is None


@pytest.mark.parametrize("source", ["expected", "other_fixture", "other_boot", "other_action"])
def test_host_journals_only_failure_for_its_exact_command(source):
    from scripts.boot_fixture import FixtureError
    from scripts.test_appliance_e2e import ApplianceE2E

    args = probe_args()
    changed = args.copy()
    value = failure()
    if source == "other_fixture":
        changed[2] = "pw-boot-" + "b" * 16 + "-observer"
    elif source == "other_boot":
        changed[10] = "11234567-89ab-cdef-0123-456789abcdef"
    elif source == "other_action":
        changed[6] = "stage"
        changed += ["--manifest", "eA==", "--signature", "eA=="]
        value = failure(action="stage", stage="stage", code="release_probe_stage_failed")
    cause = trusted_release_failure(changed, encoded(value))
    assert cause is not None
    harness = object.__new__(ApplianceE2E)
    harness.report = {}
    harness.fixture_observer = lambda: args[2]
    def run(actual, **kwargs):
        assert actual == args
        raise FixtureError("release_probe_failed") from cause
    harness.run = run
    with pytest.raises(FixtureError) as raised:
        harness.release_probe("evidence", evidence())
    if source == "expected":
        assert str(raised.value) == "release_probe_evidence_invalid"
        assert harness.report["release_probe_failures"] == [failure()]
    else:
        assert str(raised.value) == "release_probe_failed"
        assert "release_probe_failures" not in harness.report


@pytest.mark.parametrize("action,code", [("evidence", "release_probe_query_failed"),
    ("stage", "release_probe_stage_failed")])
def test_producer_failure_has_nonzero_exit_and_only_finite_metadata(monkeypatch, capsys, action, code):
    from scripts import vm_release_probe

    def private_failure(*args):
        raise RuntimeError("secret-password-and-private-path")
    args = probe_args()[5:]
    args[1] = action
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setenv("PHOTO_WALL_DATABASE_URL", "private-connection")
    monkeypatch.setattr(vm_release_probe, "Database", private_failure)
    monkeypatch.setattr(vm_release_probe, "stage", private_failure)
    with pytest.raises(SystemExit) as raised:
        vm_release_probe.main()
    assert raised.value.code == 1
    output = capsys.readouterr().out
    parsed = ReleaseFailureResult.model_validate_json(output)
    assert parsed.action == action and parsed.code == code
    assert "secret" not in output and "private" not in output
