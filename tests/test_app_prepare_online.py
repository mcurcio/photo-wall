"""Online preparation stages two exact roots without gaining stop authority."""

from uuid import uuid4

import pytest

from appliance.app_executor import (
    STAGING_ALLOWANCE_BYTES,
    AppExecutor,
    CapacitySnapshot,
    ExecutorError,
)
from appliance.app_payload import verify_root
from appliance.online_activation import OnlineArtifactRef, OnlineAttempt
from appliance.process_identity import ProcessSample
from tests.test_app_executor import Service, payload

ABI = "sha256:" + "a" * 64
PROCESS = ProcessSample(123, 456, "d" * 32)


class Capacity:
    def __init__(self, *snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    def snapshot(self, _roots):
        self.calls += 1
        return self.snapshots.pop(0) if self.snapshots else CapacitySnapshot(10**12, 10**12)


class Sampler:
    def __init__(self, *samples):
        self.samples = list(samples)

    def sample(self):
        return self.samples.pop(0) if self.samples else PROCESS


def setup_online(tmp_path, *, capacity=None, abi_reader=None):
    first, fallback_digest = payload(b"working")
    second, target_digest = payload(b"candidate")
    service = Service()
    service.roots = tmp_path / "apps"
    subject = AppExecutor(
        roots=service.roots, journal=tmp_path / "journal.json",
        lock=tmp_path / "executor.lock", legacy_override=tmp_path / "legacy",
        service=service, capacity_probe=capacity or Capacity(),
        expected_abi_reader=abi_reader or (lambda: ABI),
        protected_memory_bytes=100, protected_filesystem_bytes=100)
    assert subject.activate(first, sha256=fallback_digest, size=len(first), base_abi=ABI,
                            attempt_id=str(uuid4()), expected_base_abi=ABI) == "committed"
    service.events.clear()
    fallback_archive, target_archive = tmp_path / "fallback.tar.gz", tmp_path / "target.tar.gz"
    fallback_archive.write_bytes(first)
    target_archive.write_bytes(second)
    attempt = OnlineAttempt(
        attempt_id=uuid4(), command_sha256="e" * 64,
        target=OnlineArtifactRef(target_digest, len(second), ABI),
        fallback=OnlineArtifactRef(fallback_digest, len(first), ABI),
    )
    return subject, service, attempt, fallback_archive, target_archive


def prepare(subject, attempt, fallback_archive, target_archive, *, sampler=None):
    return subject.prepare_online(
        attempt,
        fallback_archive=fallback_archive, target_archive=target_archive,
        expected_base_abi=ABI, process_sampler=sampler or Sampler())


def test_fallback_precedes_target_and_preparation_keeps_player_running(tmp_path, monkeypatch):
    subject, service, attempt, fallback_archive, target_archive = setup_online(tmp_path)
    old_journal = subject.journal.read_bytes()
    order = []
    from appliance import app_executor

    original = app_executor.stage_payload_file

    def stage(*args, **kwargs):
        order.append(kwargs["sha256"])
        assert service.active()
        return original(*args, **kwargs)

    monkeypatch.setattr(app_executor, "stage_payload_file", stage)
    ready = prepare(subject, attempt, fallback_archive, target_archive)
    assert order == [attempt.fallback.sha256, attempt.target.sha256]
    assert ready.attempt == attempt
    assert ready.attempt.command_sha256 == attempt.command_sha256
    assert ready.selected.digest == attempt.fallback.sha256
    assert ready.process == PROCESS
    verify_root(subject.roots / attempt.fallback.sha256, expected_abi=ABI)
    verify_root(subject.roots / attempt.target.sha256, expected_abi=ABI)
    assert subject.journal.read_bytes() == old_journal
    assert service.events == [] and service.running_digest() == ready.selected.digest


def test_executor_refuses_malformed_neutral_attempt_before_filesystem_access(tmp_path):
    subject, service, attempt, fallback_archive, target_archive = setup_online(tmp_path)
    malformed = OnlineAttempt(
        attempt.attempt_id, attempt.command_sha256,
        OnlineArtifactRef("../selected", attempt.target.size, ABI), attempt.fallback)
    with pytest.raises(ExecutorError, match="online_attempt_invalid"):
        subject.prepare_online(
            malformed, fallback_archive=fallback_archive, target_archive=target_archive,
            expected_base_abi=ABI, process_sampler=Sampler())
    assert service.events == []
    assert not (subject.roots / attempt.target.sha256).exists()


def test_unrelated_fallback_refuses_without_stop_or_root_mutation(tmp_path):
    subject, service, attempt, fallback_archive, target_archive = setup_online(tmp_path)
    unrelated = OnlineAttempt(
        attempt.attempt_id, attempt.command_sha256, attempt.target,
        OnlineArtifactRef("f" * 64, attempt.fallback.size, ABI))
    old_journal = subject.journal.read_bytes()
    old_roots = sorted(path.name for path in subject.roots.iterdir())
    with pytest.raises(ExecutorError, match="online_fallback_mismatch"):
        subject.prepare_online(
            unrelated, fallback_archive=fallback_archive, target_archive=target_archive,
            expected_base_abi=ABI, process_sampler=Sampler())
    assert subject.journal.read_bytes() == old_journal
    assert sorted(path.name for path in subject.roots.iterdir()) == old_roots
    assert service.events == [] and service.running_digest() == attempt.fallback.sha256


def test_sealed_base_abi_mismatch_refuses_before_staging(tmp_path):
    subject, service, attempt, fallback_archive, target_archive = setup_online(
        tmp_path, abi_reader=lambda: "sha256:" + "f" * 64)
    with pytest.raises(ExecutorError, match="base_abi_mismatch"):
        prepare(subject, attempt, fallback_archive, target_archive)
    assert service.events == []
    assert not (subject.roots / attempt.target.sha256).exists()


@pytest.mark.parametrize("bad", ["fallback_symlink", "fallback_corrupt",
                                 "target_directory", "target_corrupt"])
def test_untrusted_or_corrupt_archive_refuses_without_stop(tmp_path, bad):
    subject, service, attempt, fallback_archive, target_archive = setup_online(tmp_path)
    if bad == "fallback_symlink":
        fallback_archive.unlink()
        fallback_archive.symlink_to(target_archive)
    elif bad == "fallback_corrupt":
        fallback_archive.write_bytes(b"x" * attempt.fallback.size)
    elif bad == "target_directory":
        target_archive.unlink()
        target_archive.mkdir()
    else:
        target_archive.write_bytes(b"x" * attempt.target.size)
    with pytest.raises(ExecutorError):
        prepare(subject, attempt, fallback_archive, target_archive)
    assert service.events == []
    assert service.running_digest() == attempt.fallback.sha256
    assert not (subject.roots / attempt.target.sha256).exists()


def test_pre_stage_capacity_refuses_before_target_publication(tmp_path):
    low = Capacity(CapacitySnapshot(STAGING_ALLOWANCE_BYTES + 99, 10**12))
    subject, service, attempt, fallback_archive, target_archive = setup_online(
        tmp_path, capacity=low)
    # Cold boot used no healthy-app capacity check; the first probe belongs to online prep.
    with pytest.raises(ExecutorError, match="app_filesystem_capacity_insufficient"):
        prepare(subject, attempt, fallback_archive, target_archive)
    assert low.calls == 1
    assert service.events == []
    assert not (subject.roots / attempt.target.sha256).exists()


def test_malformed_capacity_probe_is_named_refusal_with_no_stop(tmp_path):
    subject, service, attempt, fallback_archive, target_archive = setup_online(tmp_path)

    class BrokenProbe:
        def snapshot(self, _roots):
            return object()

    subject.capacity_probe = BrokenProbe()
    with pytest.raises(ExecutorError, match="app_capacity_unavailable"):
        prepare(subject, attempt, fallback_archive, target_archive)
    assert service.events == []


def test_final_capacity_refusal_keeps_old_process_and_verified_roots(tmp_path):
    capacity = Capacity(CapacitySnapshot(10**12, 10**12), CapacitySnapshot(99, 10**12))
    subject, service, attempt, fallback_archive, target_archive = setup_online(
        tmp_path, capacity=capacity)
    old_journal = subject.journal.read_bytes()
    with pytest.raises(ExecutorError, match="app_filesystem_capacity_insufficient"):
        prepare(subject, attempt, fallback_archive, target_archive)
    assert capacity.calls == 2
    verify_root(subject.roots / attempt.target.sha256, expected_abi=ABI)
    assert subject.journal.read_bytes() == old_journal
    assert service.events == [] and service.active()


def test_process_replacement_during_staging_refuses_prepared_handle(tmp_path):
    subject, service, attempt, fallback_archive, target_archive = setup_online(tmp_path)
    replacement = ProcessSample(PROCESS.pid, PROCESS.start_ticks + 1, PROCESS.invocation_id)
    sampler = Sampler(PROCESS, PROCESS, replacement, replacement)
    with pytest.raises(ExecutorError, match="online_selection_changed"):
        prepare(subject, attempt, fallback_archive, target_archive, sampler=sampler)
    assert service.events == [] and service.active()


def test_corrupt_selected_root_never_enters_online_staging(tmp_path):
    subject, service, attempt, fallback_archive, target_archive = setup_online(tmp_path)
    selected = subject.roots / attempt.fallback.sha256 / "app/__main__.py"
    selected.write_text("tampered")
    with pytest.raises(ExecutorError, match="payload_root_integrity"):
        prepare(subject, attempt, fallback_archive, target_archive)
    assert service.events == []
    assert not (subject.roots / attempt.target.sha256).exists()
