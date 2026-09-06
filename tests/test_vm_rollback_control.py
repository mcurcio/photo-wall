"""Fixed-path, fail-closed rollback-control protocol checks."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from contracts.release import Release
from scripts import vm_rollback_control as control

BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"
CURRENT_ID = "a" * 64
CANDIDATE_ID = "b" * 64


def _record(**changes):
    return {"schema": 1, "action": "stage-trial",
            "current": {"boot_id": BOOT_ID, "release_id": CURRENT_ID, "slot": "A"},
            "candidate": {"release_id": CANDIDATE_ID}} | changes


def _write_control(path: Path, value, *, mode="json"):
    if mode == "json":
        path.write_text(json.dumps(value), encoding="utf-8")
    else:
        path.write_bytes(value)


def test_control_schema_is_strict_and_bounded(tmp_path):
    path = tmp_path / "control.json"
    _write_control(path, _record())
    assert control._control(path)["candidate"]["release_id"] == CANDIDATE_ID

    path.write_text('{"schema":1,"schema":1,"action":"stage-trial",'
                    '"current":{},"candidate":{}}', encoding="utf-8")
    with pytest.raises(control.ControlError, match="duplicate_control_field"):
        control._control(path)

    path.write_bytes(b"{" + b" " * control.CONTROL_BYTES)
    with pytest.raises(control.ControlError, match="invalid_control"):
        control._control(path)


@pytest.mark.parametrize("change", [
    {"current": None},
    {"current": {"boot_id": 1, "release_id": CURRENT_ID, "slot": "A"}},
    {"current": {"boot_id": BOOT_ID, "release_id": CURRENT_ID, "slot": "B"}},
    {"candidate": {"release_id": 1}},
    {"candidate": {"release_id": CURRENT_ID}},
])
def test_control_rejects_malformed_identity_types_and_slots(tmp_path, change):
    path = tmp_path / "control.json"
    _write_control(path, _record(**change))
    with pytest.raises(control.ControlError, match="invalid_control"):
        control._control(path)


def test_stale_control_is_ignored_without_subprocess(tmp_path, monkeypatch):
    path = tmp_path / "control.json"
    stale = _record(current={"boot_id": "11111111-2222-3333-4444-555555555555",
                             "release_id": CURRENT_ID, "slot": "A"})
    _write_control(path, stale)
    monkeypatch.setattr(control, "_boot_id", lambda _path: BOOT_ID)
    monkeypatch.setattr(control, "_report", lambda *_: {"boot_id": BOOT_ID, "release_id": CURRENT_ID,
                                                           "slot": "A", "trial": False,
                                                           "persistence": "durable", "fault": None})
    calls = []
    assert not control.run_watcher(control=path, boot_id_path=tmp_path / "boot-id",
                                   boot_report=tmp_path / "boot.json", command_runner=lambda *args, **kwargs: calls.append(args))
    assert calls == []


def test_trial_boot_never_stages_or_invokes_acceptance(tmp_path, monkeypatch):
    path = tmp_path / "control.json"
    _write_control(path, _record())
    monkeypatch.setattr(control, "_boot_id", lambda _path: BOOT_ID)
    monkeypatch.setattr(control, "_report", lambda *_: (_ for _ in ()).throw(
        control.ControlError("boot_not_accepted")))
    calls = []
    assert not control.run_watcher(control=path, boot_id_path=tmp_path / "boot-id",
                                   boot_report=tmp_path / "boot.json", command_runner=lambda *args, **kwargs: calls.append(args))
    assert calls == []


def test_failed_acceptance_cli_does_not_reach_stage(tmp_path, monkeypatch):
    path = tmp_path / "control.json"
    _write_control(path, _record())
    monkeypatch.setattr(control, "_boot_id", lambda _path: BOOT_ID)
    monkeypatch.setattr(control, "_report", lambda *_: {"boot_id": BOOT_ID, "release_id": CURRENT_ID,
                                                           "slot": "A", "trial": False,
                                                           "persistence": "durable", "fault": None})
    monkeypatch.setattr(control.BootConfig, "load", lambda _: SimpleNamespace(
        boot_abi="c" * 64, configuration_sha256="d" * 64, directory=tmp_path))
    monkeypatch.setattr(control, "_accepted_noop", lambda *args: (_ for _ in ()).throw(
        control.ControlError("acceptance_not_noop")))
    staged = []
    monkeypatch.setattr(control, "_stage_and_reboot", lambda *args: staged.append(args))
    with pytest.raises(control.ControlError, match="acceptance_not_noop"):
        control.run_watcher(control=path, boot_id_path=tmp_path / "boot-id",
                            boot_report=tmp_path / "boot.json")
    assert staged == []


def _candidate(tmp_path):
    payload = b"candidate rootfs"
    release = Release(revision="e" * 40, boot_abi="c" * 64,
                      configuration_sha256="d" * 64,
                      rootfs_sha256=hashlib.sha256(payload).hexdigest(), rootfs_size=len(payload))
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "release.json").write_bytes(release.encode())
    (candidate / "release.sig").write_bytes(b"s" * 64)
    (candidate / release.rootfs_name).write_bytes(payload)
    return release, candidate


def test_stage_result_mismatch_never_reboots(tmp_path, monkeypatch):
    release, candidate_dir = _candidate(tmp_path)
    config = SimpleNamespace(boot_abi="c" * 64, configuration_sha256="d" * 64,
                             directory=tmp_path)
    monkeypatch.setattr(control.BootConfig, "load", lambda _: config)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == control.STAGE_TIMEOUT
        return SimpleNamespace(returncode=0, stdout="wrong\n")

    with pytest.raises(control.ControlError, match="stage_result_mismatch"):
        control._stage_and_reboot(tmp_path, tmp_path, {
            "boot_id": BOOT_ID, "release_id": CURRENT_ID, "slot": "A"}, release,
            candidate_dir / "release.json", candidate_dir / "release.sig",
            candidate_dir / release.rootfs_name, runner)
    assert len(calls) == 1
    assert calls[0][:4] == [control.PYTHON, "-I", "-m", "appliance.updates"]


def test_correct_stage_sequence_checks_state_before_reboot(tmp_path, monkeypatch, capsys):
    release, candidate_dir = _candidate(tmp_path)
    config = SimpleNamespace(boot_abi="c" * 64, configuration_sha256="d" * 64,
                             directory=tmp_path)
    monkeypatch.setattr(control.BootConfig, "load", lambda _: config)
    phases = []
    monkeypatch.setattr(control, "_state_proves_staged", lambda *args: phases.append("state"))

    def runner(argv, **kwargs):
        if argv[-1] == str(candidate_dir / release.rootfs_name):
            phases.append("stage")
            return SimpleNamespace(returncode=0, stdout=release.release_id + "\n")
        phases.append("reboot")
        return SimpleNamespace(returncode=0, stdout="")

    control._stage_and_reboot(tmp_path, tmp_path, {
        "boot_id": BOOT_ID, "release_id": CURRENT_ID, "slot": "A"}, release,
        candidate_dir / "release.json", candidate_dir / "release.sig",
        candidate_dir / release.rootfs_name, runner)
    assert phases == ["stage", "state", "reboot"]
    event = json.loads(capsys.readouterr().out)
    assert event == {"event": "photo-wall-stage-trial", "boot_id": BOOT_ID,
                     "current_release_id": CURRENT_ID, "candidate_release_id": release.release_id,
                     "slot": "B"}
