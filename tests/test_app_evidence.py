"""Local app facts require verified selected bytes and a stable live process sample."""

import fcntl
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from appliance.app_evidence import AppEvidenceCollector, ProcessSample, SystemdProcessSampler
from appliance.app_executor import AppExecutor
from contracts.player_payload import canonical_json

ABI = "sha256:" + "a" * 64
BOOT = "11111111-2222-3333-4444-555555555555"
ATTEMPT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
A = "b" * 64
B = "c" * 64
PROCESS = ProcessSample(123, 456, "d" * 32)


class Service:
    def __init__(self, digest=None, active=True):
        self.digest, self.is_active = digest, active

    def active(self):
        return self.is_active

    def running_digest(self):
        return self.digest


class Sampler:
    def __init__(self, *samples):
        self.samples = list(samples)

    def sample(self):
        return self.samples.pop(0)


def root(apps: Path, digest: str):
    files = {"app/__main__.py": b"print('ready')\n", "app/closure.json": b"{}\n"}
    selected = apps / digest
    (selected / "app").mkdir(parents=True)
    for name, data in files.items():
        (selected / name).write_bytes(data)
    manifest = {"schema": 1, "format": "pw-player-data-v1", "revision": "e" * 40,
                "base_abi": ABI, "entrypoint": "app",
                "files": {name: {"sha256": hashlib.sha256(data).hexdigest(),
                                 "size": len(data)} for name, data in files.items()}}
    (selected / "manifest.json").write_bytes(canonical_json(manifest))
    return selected


def subject(tmp_path, *, state="committed", target=A, fallback=None,
            selected=A, service=None):
    apps = tmp_path / "apps"
    root(apps, A)
    if target != A:
        root(apps, target)
    (apps / "active.json").write_text(json.dumps({"schema": 1, "sha256": selected}))
    journal = tmp_path / "mutation.json"
    journal.write_text(json.dumps({"schema": 1, "attempt_id": ATTEMPT,
                                   "target": target, "fallback": fallback, "state": state}))
    lock = tmp_path / "executor.lock"
    lock.touch()
    active_service = service or Service(selected)
    executor = AppExecutor(roots=apps, journal=journal, lock=lock,
                           legacy_override=tmp_path / "legacy", service=active_service)
    return executor


def observe(executor, *samples):
    return AppEvidenceCollector(executor, sampler=Sampler(*samples),
                                expected_abi_reader=lambda: ABI).collect(BOOT)


def test_committed_root_and_stable_process_are_separate_current_boot_facts(tmp_path):
    facts = observe(subject(tmp_path), PROCESS, PROCESS)
    assert facts.kernel_boot_id == BOOT
    assert facts.installed_sha256 == A and facts.installed_reason is None
    assert facts.running.sha256 == A and facts.running.process == PROCESS
    assert facts.running_reason is None


def test_rolled_back_attempt_reports_only_verified_fallback(tmp_path):
    facts = observe(subject(tmp_path, state="rolled_back", target=B,
                            fallback=A, selected=A), PROCESS, PROCESS)
    assert facts.installed_sha256 == A and facts.running.sha256 == A
    assert facts.installed_sha256 != B


def test_unsettled_mutation_and_legacy_install_cannot_claim_installed(tmp_path):
    executor = subject(tmp_path, state="start_requested")
    assert observe(executor).installed_reason == "executor_mutation_unsettled"
    executor.legacy_override.touch()
    assert observe(executor).installed_reason == "legacy_app_unverified"


def test_locked_executor_and_tampered_root_yield_unknown(tmp_path):
    executor = subject(tmp_path)
    with executor.lock.open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert observe(executor).installed_reason == "executor_busy"
    (executor.roots / A / "app/__main__.py").write_text("changed")
    assert observe(executor).installed_reason == "payload_root_integrity"


def test_process_sampling_keeps_the_shared_mutation_fence(tmp_path):
    executor = subject(tmp_path)

    class FencedSampler:
        def sample(self):
            with executor.lock.open("rb") as competing:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return PROCESS

    facts = AppEvidenceCollector(executor, sampler=FencedSampler(),
                                 expected_abi_reader=lambda: ABI).collect(BOOT)
    assert facts.running is not None
    executor.lock.unlink()
    assert observe(executor).installed_reason == "executor_not_initialized"
    assert not executor.lock.exists()  # observing cannot create executor state


def test_inactive_or_racing_process_never_becomes_running_proof(tmp_path):
    service = Service(A, active=False)
    executor = subject(tmp_path, service=service)
    facts = observe(executor)
    assert facts.installed_sha256 == A and facts.running is None
    assert facts.running_reason == "unit_inactive"
    service.is_active = True
    changed = ProcessSample(PROCESS.pid, PROCESS.start_ticks + 1, PROCESS.invocation_id)
    facts = observe(executor, PROCESS, changed)
    assert facts.installed_sha256 == A and facts.running is None
    assert facts.running_reason == "process_unconfirmed"
    service.digest = B
    facts = observe(executor, PROCESS, PROCESS)
    assert facts.running is None and facts.running_reason == "process_unconfirmed"


def test_systemd_sampler_uses_main_pid_invocation_and_proc_start_tick(tmp_path, monkeypatch):
    proc = tmp_path / "123"
    proc.mkdir()
    # The final ')' matters: a process name can itself contain a close parenthesis.
    (proc / "stat").write_text("123 (odd ) name) S " + " ".join(["0"] * 18 + ["456"]))
    monkeypatch.setattr("appliance.app_evidence.subprocess.run", lambda *_a, **_k:
                        SimpleNamespace(returncode=0, stdout="MainPID=123\n"
                                        "InvocationID=" + "d" * 32 + "\nActiveState=active\n"))
    assert SystemdProcessSampler(tmp_path).sample() == PROCESS
    (proc / "stat").write_text("123 (name) S 0")
    assert SystemdProcessSampler(tmp_path).sample() is None


def test_bad_boot_id_is_not_an_evidence_scope(tmp_path):
    collector = AppEvidenceCollector(subject(tmp_path), sampler=Sampler(PROCESS, PROCESS),
                                     expected_abi_reader=lambda: ABI)
    with pytest.raises(ValueError, match="boot_id_invalid"):
        collector.collect("not-a-boot-id")
