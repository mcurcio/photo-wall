"""Data-only activation ordering and crash recovery; no systemd or hardware side effects."""

import gzip
import hashlib
import io
import json
import subprocess
import tarfile

import pytest

from appliance.app_executor import AppExecutor, ExecutorError, SystemdPlayer
from appliance.app_launcher import selected_app
from appliance.app_payload import PayloadError, stage_payload, verify_root
from contracts.player_payload import canonical_json

ABI = "sha256:" + "a" * 64
ATTEMPT_A = "11111111-2222-3333-4444-555555555555"
ATTEMPT_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def payload(marker=b"a", *, member_type=tarfile.REGTYPE):
    files = {"app/__main__.py": b"print('" + marker + b"')\n",
             "app/closure.json": b"{}\n"}
    manifest = {"schema": 1, "format": "pw-player-data-v1", "revision": "b" * 40,
                "base_abi": ABI, "entrypoint": "app",
                "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                          for name, data in files.items()}}
    members = {"manifest.json": canonical_json(manifest), **files}
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o644
                info.type = member_type if name == "app/__main__.py" else tarfile.REGTYPE
                archive.addfile(info, io.BytesIO(data))
    body = raw.getvalue()
    return body, hashlib.sha256(body).hexdigest()


class Service:
    def __init__(self):
        self.is_active = False
        self.running = None
        self.roots = None
        self.events = []
        self.on_stop = None
        self.fail_stop = False
        self.fail_start = False
        self.crash_start = False

    def active(self):
        return self.is_active

    def running_digest(self):
        return self.running

    def stop(self):
        if self.on_stop:
            self.on_stop()
        self.events.append("stop")
        if self.fail_stop:
            raise KeyboardInterrupt("executor crashed during stop")
        self.is_active = False
        self.running = None

    def start(self):
        self.events.append("start")
        if self.crash_start:
            raise KeyboardInterrupt("executor crashed during start")
        if self.fail_start:
            raise ExecutorError("unit_failed")
        self.is_active = True
        self.running = json.loads((self.roots / "active.json").read_text())["sha256"]


def executor(tmp_path, service):
    service.roots = tmp_path / "apps"
    return AppExecutor(roots=tmp_path / "apps", journal=tmp_path / "journal.json",
                       lock=tmp_path / "executor.lock", legacy_override=tmp_path / "legacy",
                       service=service)


def activate(subject, body, digest, attempt):
    return subject.activate(body, sha256=digest, size=len(body), base_abi=ABI,
                            attempt_id=attempt, expected_base_abi=ABI)


def test_target_is_verified_and_intent_is_durable_before_first_stop(tmp_path):
    body, digest = payload()
    service = Service()
    subject = executor(tmp_path, service)

    def before_stop():
        journal = json.loads((tmp_path / "journal.json").read_text())
        assert journal["state"] == "intent_stop"
        assert journal["target"] == digest
        verify_root(tmp_path / "apps" / digest, expected_abi=ABI)

    service.on_stop = before_stop
    assert activate(subject, body, digest, ATTEMPT_A) == "committed"
    assert service.events == ["stop", "start"]
    assert json.loads((tmp_path / "apps/active.json").read_text())["sha256"] == digest
    assert selected_app(tmp_path / "apps") == tmp_path / "apps" / digest / "app"
    assert activate(subject, body, digest, ATTEMPT_A) == "committed"
    assert service.events == ["stop", "start"]


def test_invalid_payload_or_fallback_cannot_stop_a_healthy_app(tmp_path):
    service = Service()
    subject = executor(tmp_path, service)
    body, digest = payload()
    with pytest.raises(ExecutorError):
        activate(subject, body + b"x", digest, ATTEMPT_A)
    assert service.events == []
    assert activate(subject, body, digest, ATTEMPT_A) == "committed"
    service.events.clear()
    (tmp_path / "apps" / digest / "app/__main__.py").write_text("tampered")
    next_body, next_digest = payload(b"b")
    with pytest.raises(ExecutorError):
        activate(subject, next_body, next_digest, ATTEMPT_B)
    assert service.events == []


def test_writable_fallback_root_is_rejected_before_service_stop(tmp_path):
    service = Service()
    subject = executor(tmp_path, service)
    first, digest = payload()
    assert activate(subject, first, digest, ATTEMPT_A) == "committed"
    service.events.clear()
    root = tmp_path / "apps" / digest
    root.chmod(0o777)
    next_body, next_digest = payload(b"b")
    with pytest.raises(ExecutorError, match="payload_root_ownership"):
        activate(subject, next_body, next_digest, ATTEMPT_B)
    assert service.events == []


def test_crash_during_stop_reconciles_same_attempt_before_admitting_another(tmp_path):
    body, digest = payload()
    service = Service()
    subject = executor(tmp_path, service)
    service.fail_stop = True
    with pytest.raises(KeyboardInterrupt):
        activate(subject, body, digest, ATTEMPT_A)
    assert json.loads((tmp_path / "journal.json").read_text())["state"] == "intent_stop"
    service.fail_stop = False
    resumed = executor(tmp_path, service)
    assert activate(resumed, body, digest, ATTEMPT_A) == "committed"
    assert service.events == ["stop", "stop", "start"]


def test_failed_candidate_rolls_back_verified_fallback_immediately(tmp_path):
    first, first_digest = payload()
    second, second_digest = payload(b"b")
    service = Service()
    subject = executor(tmp_path, service)
    assert activate(subject, first, first_digest, ATTEMPT_A) == "committed"
    service.fail_start = True
    # Fail only the candidate's first start; fallback can still restart.
    def fail_once():
        service.fail_start = False
        raise ExecutorError("unit_failed")

    original_start = service.start

    def start_once():
        if service.fail_start:
            fail_once()
        original_start()

    service.start = start_once
    assert activate(subject, second, second_digest, ATTEMPT_B) == "rolled_back"
    service.fail_start = False
    assert activate(executor(tmp_path, service), second, second_digest, ATTEMPT_B) == "rolled_back"
    assert json.loads((tmp_path / "apps/active.json").read_text())["sha256"] == first_digest
    assert service.is_active


def test_crash_after_start_intent_recovers_fallback_before_retry(tmp_path):
    first, first_digest = payload()
    second, second_digest = payload(b"b")
    service = Service()
    subject = executor(tmp_path, service)
    assert activate(subject, first, first_digest, ATTEMPT_A) == "committed"
    service.crash_start = True
    with pytest.raises(KeyboardInterrupt):
        activate(subject, second, second_digest, ATTEMPT_B)
    assert json.loads((tmp_path / "journal.json").read_text())["state"] == "start_requested"
    service.crash_start = False
    assert activate(executor(tmp_path, service), second, second_digest, ATTEMPT_B) == "rolled_back"
    assert service.running_digest() == first_digest


def test_recover_interrupted_cold_start_without_payload_or_network(tmp_path):
    body, digest = payload()
    service = Service()
    subject = executor(tmp_path, service)
    service.crash_start = True
    with pytest.raises(KeyboardInterrupt):
        activate(subject, body, digest, ATTEMPT_A)
    assert json.loads((tmp_path / "journal.json").read_text())["state"] == "start_requested"
    service.crash_start = False
    recovered = executor(tmp_path, service).recover(expected_base_abi=ABI)
    assert recovered is not None
    assert (recovered.attempt_id, recovered.state, recovered.active_sha256) == (
        ATTEMPT_A, "committed", digest)
    assert service.running_digest() == digest
    assert json.loads((tmp_path / "journal.json").read_text())["state"] == "committed"


def test_recover_interrupted_candidate_uses_local_fallback(tmp_path):
    first, first_digest = payload()
    second, second_digest = payload(b"b")
    service = Service()
    subject = executor(tmp_path, service)
    assert activate(subject, first, first_digest, ATTEMPT_A) == "committed"
    service.crash_start = True
    with pytest.raises(KeyboardInterrupt):
        activate(subject, second, second_digest, ATTEMPT_B)
    service.crash_start = False
    recovered = executor(tmp_path, service).recover(expected_base_abi=ABI)
    assert recovered is not None
    assert (recovered.attempt_id, recovered.state, recovered.active_sha256) == (
        ATTEMPT_B, "rolled_back", first_digest)
    assert service.running_digest() == first_digest


def test_recover_terminal_commit_repairs_service_without_reverting_target(tmp_path):
    first, first_digest = payload()
    second, second_digest = payload(b"b")
    service = Service()
    subject = executor(tmp_path, service)
    assert activate(subject, first, first_digest, ATTEMPT_A) == "committed"
    assert activate(subject, second, second_digest, ATTEMPT_B) == "committed"
    service.is_active, service.running = False, None
    recovered = executor(tmp_path, service).recover(expected_base_abi=ABI)
    assert recovered is not None
    assert recovered.state == "committed" and recovered.active_sha256 == second_digest
    assert service.running_digest() == second_digest


def test_recover_terminal_commit_falls_back_when_target_restart_fails(tmp_path):
    first, first_digest = payload()
    second, second_digest = payload(b"b")
    service = Service()
    subject = executor(tmp_path, service)
    assert activate(subject, first, first_digest, ATTEMPT_A) == "committed"
    assert activate(subject, second, second_digest, ATTEMPT_B) == "committed"
    service.is_active, service.running = False, None
    original_start = service.start
    starts = 0

    def fail_first_start():
        nonlocal starts
        starts += 1
        if starts == 1:
            raise ExecutorError("target_restart_failed")
        original_start()

    service.start = fail_first_start
    recovered = subject.recover(expected_base_abi=ABI)
    assert recovered is not None
    assert recovered.state == "rolled_back" and recovered.active_sha256 == first_digest
    assert service.running_digest() == first_digest


def test_recover_corrupt_selected_root_never_stops_running_player(tmp_path):
    body, digest = payload()
    service = Service()
    subject = executor(tmp_path, service)
    assert activate(subject, body, digest, ATTEMPT_A) == "committed"
    service.events.clear()
    (tmp_path / "apps" / digest / "app/__main__.py").write_bytes(b"tampered")
    with pytest.raises(ExecutorError, match="payload_root_integrity"):
        subject.recover(expected_base_abi=ABI)
    assert service.events == []
    assert service.running_digest() == digest


def test_failed_cold_start_is_fenced_until_operator_action(tmp_path):
    body, digest = payload()
    service = Service()
    service.fail_start = True
    subject = executor(tmp_path, service)
    with pytest.raises(ExecutorError, match="candidate_start_failed"):
        activate(subject, body, digest, ATTEMPT_A)
    assert not (tmp_path / "apps/active.json").exists()
    assert json.loads((tmp_path / "journal.json").read_text())["state"] == "recovery_required"
    events = list(service.events)
    service.fail_start = False
    with pytest.raises(ExecutorError, match="cold_attempt_requires_operator"):
        activate(executor(tmp_path, service), body, digest, ATTEMPT_A)
    assert service.events == events


def test_nonregular_archive_member_is_rejected_before_staging(tmp_path):
    body, digest = payload(member_type=tarfile.SYMTYPE)
    with pytest.raises(PayloadError):
        stage_payload(body, sha256=digest, size=len(body), expected_abi=ABI,
                      roots=tmp_path / "apps")


def test_systemd_running_digest_requires_main_pid_exact_base_launcher_argv(
        tmp_path, monkeypatch):
    proc = tmp_path / "proc/123"
    proc.mkdir(parents=True)
    roots = tmp_path / "apps"
    digest = "b" * 64
    monkeypatch.setattr("appliance.app_executor.subprocess.run",
                        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "123\n", ""))
    service = SystemdPlayer(roots=roots, proc_root=tmp_path / "proc")
    cmdline = proc / "cmdline"
    cmdline.write_bytes(b"/usr/bin/python3\x00-I\x00-B\x00" +
                        str(roots / digest / "app").encode() +
                        b"\x00--config\x00/etc/photo-wall/public.json\x00")
    assert service.running_digest() == digest
    cmdline.write_bytes(cmdline.read_bytes() + b"--extra\x00")
    assert service.running_digest() is None
    cmdline.write_bytes(b"/usr/bin/python3\x00-I\x00-B\x00" +
                        str(roots / ("c" * 64) / "app").encode() +
                        b"\x00--config\x00/etc/photo-wall/public.json\x00")
    assert service.running_digest() != digest
    cmdline.write_bytes(b"/usr/bin/python3\x00-I\x00-B\x00/usr/lib/photo-wall-player\x00")
    assert service.running_digest() is None
