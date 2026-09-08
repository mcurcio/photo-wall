"""Exact image identity, central boot authority, and safe fixture cleanup."""
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from central.installation_models import EnrollmentObservation, EquipmentSessionObservation
from scripts.boot_fixture import FixtureError
from scripts.test_appliance_e2e import ApplianceE2E, boot_reports, checked_inputs, enrollment
from scripts.vm_release_contract import (
    CentralBootEvidence,
    ReleaseEvidenceResult,
    ReleaseStageResult,
)

RELEASE = "a" * 64
CANDIDATE = "c" * 64
BOOT = "01234567-89ab-cdef-0123-456789abcdef"
PLAYER = "p-" + "b" * 32
DEVICE = "device-" + "d" * 64


def report(**changes):
    return dict(schema=2, boot_id=BOOT, device_id=DEVICE, ticket_sha256="e" * 64,
                release_id=RELEASE, trial=False, persistence="volatile", fault=None) | changes


def row(**changes):
    return EquipmentSessionObservation(**(dict(player_id=PLAYER, device_id=DEVICE, authority_epoch=1, retired=False) | changes))


def central_evidence(boot=None, **changes):
    boot = boot or report()
    return CentralBootEvidence(**({key: boot[key] for key in
        ("boot_id", "device_id", "ticket_sha256", "release_id", "trial")} | dict(
        status="booting", current=True, accepted_release_id=RELEASE, candidate_release_id=None,
        current_player_id=PLAYER, current_authority_epoch=1,
        trial_ticket_sha256=boot["ticket_sha256"] if boot["trial"] else None) | changes))


@pytest.mark.parametrize("invalid_json", [False, True])
def test_preflight_failure_writes_unqualified_report_without_starting_fixture(
        inputs, tmp_path, monkeypatch, invalid_json):
    from scripts import test_appliance_e2e as e2e

    manifest, disk, _ = inputs
    if invalid_json:
        manifest.write_text('{"private-field":"private-input-must-not-escape"')
    else:
        disk.write_bytes(b"changed image")
    result = tmp_path / "report.json"
    state = tmp_path / "state"
    monkeypatch.setattr(sys, "argv", ["e2e", "--manifest", str(manifest), "--state", str(state),
        "--report", str(result), "--central-image", "sha256:" + "c" * 64,
        "--builder-image", "sha256:" + "d" * 64])
    monkeypatch.setattr(e2e, "ApplianceE2E", lambda *args: pytest.fail("preflight started fixture"))
    with pytest.raises(SystemExit) as exit_info:
        e2e.main()
    assert exit_info.value.code == 1
    recorded = json.loads(result.read_text())
    assert recorded["status"] == "failed"
    assert recorded["phase"] == "preflight"
    assert recorded["failure"] == ("JSONDecodeError" if invalid_json else "disk_identity_mismatch")
    assert not any(recorded["qualification"].values())
    assert not state.exists()
    assert "private-input" not in result.read_text()



class CleanupFixture:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def down(self):
        self.calls += 1
        if self.error:
            raise FixtureError(self.error)



def make_cleanup_harness(tmp_path, run, fixture):
    disk = tmp_path / "signed.img"
    disk.write_bytes(b"signed disk")
    harness = object.__new__(ApplianceE2E)
    harness.run, harness.name = run, "pw-vm-fixture"
    harness.container_id, harness.builder_image = "original-container", "sha256:" + "d" * 64
    harness.fixture = fixture
    harness.inputs = {"disk": disk, "disk_record": {
        "sha256": hashlib.sha256(disk.read_bytes()).hexdigest()}}
    harness.report = {"checks": {}, "qualification": {"generic_vm": True}}
    return harness



def test_cleanup_refuses_replaced_vm_but_continues_owned_cleanup(tmp_path):
    calls = []
    fixture = CleanupFixture()

    def run(args, **kwargs):
        calls.append(args)
        if args[1] == "inspect":
            return ('other-container\nsha256:' + 'd' * 64 + '\npw-vm-fixture\n' +
                    json.dumps(dict(Running=True, ExitCode=0, OOMKilled=False))).encode()
        raise AssertionError(args)

    harness = make_cleanup_harness(tmp_path, run, fixture)
    with pytest.raises(FixtureError, match="cleanup_failed:vm:vm_identity_changed"):
        harness.cleanup()
    assert all(args[1] == "inspect" for args in calls)
    assert fixture.calls == 1
    assert harness.report["checks"]["signed_disk_unchanged"] is True
    assert harness.report["qualification"]["generic_vm"] is False



def test_cleanup_missing_vm_still_cleans_fixture_and_checks_disk(tmp_path):
    fixture = CleanupFixture()

    def run(args, **kwargs):
        if args[1] == "inspect":
            raise FixtureError("docker_command_failed")
        raise AssertionError(args)

    harness = make_cleanup_harness(tmp_path, run, fixture)
    with pytest.raises(FixtureError, match="cleanup_failed:vm:docker_command_failed"):
        harness.cleanup()
    assert fixture.calls == 1
    assert harness.report["checks"]["signed_disk_unchanged"] is True



def test_fixture_cleanup_failure_still_checks_disk(tmp_path):
    fixture = CleanupFixture("fixture_down_failed")

    def run(args, **kwargs):
        if args[1] == "inspect":
            return ("original-container\nsha256:" + "d" * 64 + "\npw-vm-fixture\n" +
                    json.dumps(dict(Running=False, ExitCode=0, OOMKilled=False))).encode()
        if args[1] == "logs":
            return b""
        if args[1] == "rm":
            return b""
        raise AssertionError(args)

    harness = make_cleanup_harness(tmp_path, run, fixture)
    with pytest.raises(FixtureError, match="cleanup_failed:fixture:fixture_down_failed"):
        harness.cleanup()
    assert harness.report["checks"]["signed_disk_unchanged"] is True
    assert harness.report["cleanup_phases"] == {"fixture": "fixture_down_failed"}
    assert harness.report["qualification"]["generic_vm"] is False



@pytest.fixture
def inputs(tmp_path, monkeypatch):
    def put(path, value):
        path.write_text(json.dumps(value))

    def record(path, content):
        path.write_bytes(content)
        return dict(size=len(content), sha256=hashlib.sha256(content).hexdigest())

    disk = tmp_path / "image.img"
    disk_record = record(disk, b"signed test disk")
    generic = tmp_path / "generic"
    generic.mkdir()
    kernel_record = record(generic / "Image", b"test kernel")
    initrd_record = record(generic / "initrd.img", b"preserved test initrd")
    production = dict(size=10, sha256="e" * 64)
    put(generic / "manifest.json", dict(kind="generic-vm-initramfs",
        validation=dict(reopened=True, protected_bytes_equal=True),
        outputs=dict(kernel=dict(name="Image", **kernel_record), initrd=dict(name="initrd.img", **initrd_record)),
        input=dict(expected_sha256=production["sha256"], expected_size=production["size"])))
    put(tmp_path / "artifact.json", dict(image=disk.name, image_sha256=disk_record["sha256"],
        image_size=disk_record["size"], release_id=RELEASE, pxe_files={"initrd.img": production}))
    for name in ("bundle", "deployment"):
        (tmp_path / name).mkdir()
    from scripts.build_rollback_candidate import FAULT_CONTENT, FAULT_PATH
    candidate_dir = tmp_path / "deployment/rollback-candidate"
    candidate_dir.mkdir()
    releases = {slot: SimpleNamespace(revision="f" * 40, release_id=identity,
                boot_abi="b" * 64, configuration_sha256="d" * 64,
                rootfs_sha256=identity, rootfs_size=123) for slot, identity in
                (("accepted", RELEASE), ("candidate", CANDIDATE))}
    metadata = dict(schema=1, kind="ci-rollback-candidate", source_revision="f" * 40,
                    boot_abi="b" * 64, configuration_sha256="d" * 64,
                    fault_path=FAULT_PATH, content_sha256=hashlib.sha256(FAULT_CONTENT).hexdigest(),
                    releases={slot: {key: getattr(value, key) for key in
                           ("release_id", "rootfs_sha256", "rootfs_size")} for slot, value in releases.items()})
    put(candidate_dir / "candidate.json", metadata)
    monkeypatch.setattr("scripts.boot_gateway.BootBundle.load", lambda directory, *_:
                        SimpleNamespace(release=releases["candidate" if directory == candidate_dir else "accepted"]))
    manifest = tmp_path / "ci-image.json"
    put(manifest, dict(schema=1, source_commit="f" * 40, disk=dict(path=str(disk), **disk_record),
        generic_boot=str(generic), bundle=str(tmp_path / "bundle"), deployment=str(tmp_path / "deployment"),
        rollback_candidate=dict(path=str(candidate_dir), metadata=metadata)))
    return manifest, disk, generic



def test_image_and_initramfs_are_bound_to_same_final_artifact(inputs):
    manifest, disk, generic = inputs
    assert checked_inputs(manifest)["disk"] == disk
    record = json.loads((generic / "manifest.json").read_text())
    record["input"]["expected_sha256"] = "0" * 64
    (generic / "manifest.json").write_text(json.dumps(record))
    with pytest.raises(FixtureError, match="initramfs_source_mismatch"):
        checked_inputs(manifest)



def test_full_generic_inventory_exceeding_fixture_json_limit_is_accepted(inputs):
    manifest, disk, generic = inputs
    record_path = generic / "manifest.json"
    record = json.loads(record_path.read_text())
    record["protected_inventory"] = {f"usr/lib/python3.12/module_{i}.py":
        {"size": 1024, "sha256": "a" * 64} for i in range(12_000)}
    record_path.write_text(json.dumps(record))
    assert record_path.stat().st_size > 1024**2
    assert checked_inputs(manifest)["disk"] == disk



def test_generic_inventory_over_its_own_limit_is_rejected(inputs):
    from scripts.build_vm_initrd import MAX_MANIFEST_BYTES

    manifest, _, generic = inputs
    with (generic / "manifest.json").open("wb") as stream:
        stream.truncate(MAX_MANIFEST_BYTES + 1)
    with pytest.raises(FixtureError, match="invalid_regular_file"):
        checked_inputs(manifest)



def test_changed_disk_is_rejected_before_starting_services(inputs):
    manifest, disk, _ = inputs
    disk.write_bytes(b"tampered image")
    with pytest.raises(FixtureError, match="disk_identity_mismatch"):
        checked_inputs(manifest)



def test_changed_generic_kernel_is_rejected(inputs):
    manifest, _, generic = inputs
    (generic / "Image").write_bytes(b"wrong kernel")
    with pytest.raises(FixtureError, match="generic_identity_mismatch"):
        checked_inputs(manifest)



@pytest.mark.parametrize("change", ["missing", "metadata", "slot", "abi", "fault"])
def test_candidate_preflight_rejects_unbound_evidence(inputs, change):
    manifest, _, _ = inputs
    data = json.loads(manifest.read_text())
    if change == "missing":
        del data["rollback_candidate"]
    else:
        candidate = data["rollback_candidate"]
        if change == "metadata":
            candidate["metadata"]["source_revision"] = "0" * 40
        else:
            if change == "slot":
                candidate["metadata"]["releases"]["candidate"]["rootfs_sha256"] = "0" * 64
            elif change == "abi":
                candidate["metadata"]["boot_abi"] = "0" * 64
            else:
                candidate["metadata"]["content_sha256"] = "0" * 64
            from pathlib import Path
            (Path(candidate["path"]) / "candidate.json").write_text(json.dumps(candidate["metadata"]))
    manifest.write_text(json.dumps(data))
    with pytest.raises(FixtureError, match="rollback_candidate_required|candidate_.*mismatch"):
        checked_inputs(manifest)



def test_serial_diagnostics_keep_only_fixed_public_fault_names():
    from scripts.test_appliance_e2e import serial_diagnostics

    result = serial_diagnostics(
        "systemd[1]: \x1b[0;31msystemd-networkd.service: Main process exited, status=200/CHDIR\x1b[0m\n"
        "ModuleNotFoundError: private-input-must-not-escape\n"
        "arbitrary-token.service: Failed with result secret\n")
    assert result == dict(bootstrap_failed=False, qemu_device_error=False, systemd_chdir_failure=True,
                         kernel_panic=False, out_of_memory=False,
                         failed_services=["systemd-networkd"], service_exit_status={},
                         namespace_failures={}, player_faults=[],
                         python_errors=["ModuleNotFoundError"])
    assert "private-input" not in json.dumps(result)
    assert "arbitrary-token" not in json.dumps(result)


def test_serial_diagnostics_classifies_bootstrap_failure_without_exporting_detail():
    from scripts.test_appliance_e2e import serial_diagnostics

    result = serial_diagnostics("private context\nphoto-wall: boot_failed\nprivate token")
    assert result["bootstrap_failed"] is True
    assert "private" not in json.dumps(result)



def test_serial_diagnostics_expose_bounded_service_exit_codes_without_messages():
    from scripts.test_appliance_e2e import serial_diagnostics

    result = serial_diagnostics(
        "photo-wall-player.service: Control process exited, code=exited, status=226/NAMESPACE\n"
        "photo-wall-player.service: Main process exited, code=killed, status=6/ABRT\n"
        "photo-wall-player.service: Control process exited, code=exited, status=226/NAMESPACE\n"
        "private-token.service: Main process exited, code=exited, status=203/EXEC\n"
        "photo-wall-player.service: private path and token must not escape\n")
    assert result["service_exit_status"] == {"photo-wall-player": [
        dict(code="exited", status=226, name="NAMESPACE"),
        dict(code="killed", status=6, name="ABRT")]}
    assert "private" not in json.dumps(result)



def test_namespace_diagnostics_classify_known_paths_without_exporting_guest_text():
    from scripts.test_appliance_e2e import serial_diagnostics

    result = serial_diagnostics(
        "systemd[123]: photo-wall-player.service: Failed to set up mount namespacing: "
        "/run/systemd/unit-root/run/photo-wall/player: No such file or directory\n"
        "photo-wall-player.service: Failed to set up mount namespacing: "
        "/var/lib/photo-wall/player: Read-only file system\n"
        "photo-wall-player.service: Failed to set up mount namespacing: Operation not permitted\n"
        "photo-wall-player.service: Failed to set up mount namespacing: "
        "/private-secret/path: private-secret error\n"
        "private-photo-wall-player.service: Failed to set up mount namespacing: /home: Permission denied\n")
    assert result["namespace_failures"] == {"photo-wall-player": [
        dict(path="player_runtime", errno="ENOENT"),
        dict(path="unclassified", errno="EROFS"),
        dict(path="unclassified", errno="unclassified"),
        dict(path="unspecified", errno="EPERM"),
    ]}
    assert "private" not in json.dumps(result)



def test_diagnostics_do_not_attribute_lookalike_units_to_the_player():
    from scripts.test_appliance_e2e import serial_diagnostics

    result = serial_diagnostics(
        "private-photo-wall-player.service: Failed to set up mount namespacing: /home: Permission denied\n"
        "private-photo-wall-player.service: Main process exited, code=exited, status=226/NAMESPACE\n")
    assert result["failed_services"] == []
    assert result["service_exit_status"] == {}
    assert result["namespace_failures"] == {}



def test_namespace_diagnostics_deduplicate_and_bound_reported_failures():
    from scripts.test_appliance_e2e import serial_diagnostics

    lines = [f"photo-wall-player.service: Failed to set up mount namespacing: {path}: Permission denied"
             for path in ("/", "/home", "/root", "/run/user", "/run/user/10001", "/run/photo-wall",
                          "/run/photo-wall/player", "/var/lib/photo-wall", "/tmp", "/var/tmp")]
    result = serial_diagnostics("\n".join(lines * 5))["namespace_failures"]["photo-wall-player"]
    assert len(result) == 8
    assert len({(item["path"], item["errno"]) for item in result}) == 8



def test_vm_replacement_during_log_collection_cannot_be_stopped(tmp_path):
    fixture = CleanupFixture()
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[1] == "inspect":
            identity = "original-container" if len(calls) == 1 else "replacement"
            return (identity + "\nsha256:" + "d" * 64 + "\npw-vm-fixture\n" +
                    json.dumps(dict(Running=True, ExitCode=0, OOMKilled=False))).encode()
        raise AssertionError(args)

    harness = make_cleanup_harness(tmp_path, run, fixture)
    with pytest.raises(FixtureError, match="vm_identity_changed"):
        harness.cleanup()
    assert all(args[1] == "inspect" for args in calls)
    assert fixture.calls == 1
    assert harness.report["checks"]["signed_disk_unchanged"] is True



def test_serial_player_faults_export_only_known_complete_codes():
    from scripts.test_appliance_e2e import serial_diagnostics

    result = serial_diagnostics("player fault: native_initialization\n"
        "player fault: secret_token\nplayer fault: connection_failed_extra\n"
        "player fault: health_storage\n")
    assert result["player_faults"] == ["native_initialization", "health_storage"]
    assert "secret_token" not in json.dumps(result)


def test_serial_diagnostics_classifies_qemu_device_startup_failure_without_exporting_detail():
    from scripts.test_appliance_e2e import serial_diagnostics

    result = serial_diagnostics(
        "qemu-system-aarch64: -device missing: Device 'missing' not found\nprivate detail"
    )
    assert result["qemu_device_error"] is True
    assert "missing" not in json.dumps(result)



def test_boot_evidence_redacts_capability_and_rejects_conflicting_or_durable_context():
    assert boot_reports(json.dumps(report()) + '\n' + json.dumps(report()), RELEASE) == [report()]
    for changed in (report(schema=1), report(ticket_id='f' * 48), report(persistence='durable')):
        with pytest.raises(FixtureError, match='guest_boot_report_invalid'):
            boot_reports(json.dumps(changed), RELEASE)
    with pytest.raises(FixtureError, match='conflicting_boot_report'):
        boot_reports(json.dumps(report()) + '\n' + json.dumps(report(ticket_sha256='f' * 64)), RELEASE)


def test_stable_equipment_requires_fresh_session_after_reboot():
    assert enrollment((row(authority_epoch=2),), row()) == EnrollmentObservation(state="ready", session=row(authority_epoch=2))
    assert enrollment((row(),), row()) == EnrollmentObservation(state="pending")
    assert enrollment(()) == EnrollmentObservation(state="pending")
    with pytest.raises(FixtureError, match='equipment_identity_changed'):
        enrollment((row(device_id='device-'+'f'*64, authority_epoch=2),), row())


def test_enrollment_probe_retries_bounded_runner_failures(monkeypatch):
    from scripts import test_appliance_e2e as e2e

    harness = object.__new__(ApplianceE2E)
    harness.report = {}
    attempts = iter((FixtureError("docker_command_failed"), FixtureError("docker_timeout"), "ready"))
    monkeypatch.setattr(e2e.time, "sleep", lambda seconds: seconds == 2)

    def probe():
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    assert harness.enrollment_probe("central_inventory", probe) == "ready"
    assert harness.report["probe_retries"] == {"central_inventory": 2}


def test_enrollment_probe_names_persistent_failure(monkeypatch):
    from scripts import test_appliance_e2e as e2e

    harness = object.__new__(ApplianceE2E)
    harness.report = {}
    monkeypatch.setattr(e2e.time, "sleep", lambda _: None)

    def unavailable():
        raise FixtureError("docker_command_failed")

    with pytest.raises(FixtureError, match="central_inventory_unavailable:docker_command_failed"):
        harness.enrollment_probe("central_inventory", unavailable)
    assert harness.report["probe_retries"] == {"central_inventory": e2e.PROBE_ATTEMPTS}


def test_inventory_transport_soak_uses_authenticated_fixture_probe():
    harness = object.__new__(ApplianceE2E)
    harness.report = {"checks": {}}
    calls = []
    harness.inventory = lambda: calls.append("inventory") or ()
    harness.enrollment_probe = lambda name, callback: calls.append(name) or callback()

    harness.verify_inventory_transport(3)

    assert calls == ["central_inventory", "inventory"] * 3
    assert harness.report["checks"]["central_inventory_pre_vm"] is True


def test_inventory_exec_uses_the_checked_observer_container():
    harness = object.__new__(ApplianceE2E)
    name = "pw-boot-" + "a" * 16 + "-observer"
    resource = dict(kind="container", name=name, id="pinned-container-id")
    checks, calls = [], []
    harness.fixture = SimpleNamespace(project="pw-boot-" + "a" * 16,
        resources={"container:" + name: resource}, check=lambda value: checks.append(value))
    def run(args, **kwargs):
        calls.append(args)
        return b"[]"
    harness.run = run
    assert harness.inventory() == ()
    assert checks == [resource]
    assert calls == [["docker", "exec", name, "python", "-m", "scripts.vm_inventory_probe"]]


def test_boot_report_is_a_distinct_gate_before_player_enrollment(monkeypatch):
    from scripts import test_appliance_e2e as e2e

    harness = object.__new__(ApplianceE2E)
    harness.report = {"checks": {}, "boots": [], "observed_boot_reports": []}
    harness.checked_vm = lambda: dict(Running=True, OOMKilled=False)
    harness.serial = lambda: json.dumps(report())
    harness.enrollment_probe = lambda _name, callback: callback()
    harness.observe_serial = lambda serial: [report()] if serial else []
    monkeypatch.setattr(e2e.time, "sleep", lambda _: None)

    assert harness.wait_boot_report() == report()
    assert harness.report["checks"]["bootstrap_reached_rootfs"] is True


def test_central_boot_evidence_is_a_distinct_gate():
    harness = object.__new__(ApplianceE2E)
    harness.report = {"checks": {}}
    calls = []
    harness.boot_evidence = lambda boot: calls.append(boot) or {"recorded": True}
    harness.enrollment_probe = lambda name, callback: calls.append(name) or callback()

    harness.verify_boot_attempt(report())

    assert calls == ["central_boot_evidence", report()]
    assert harness.report["checks"]["central_boot_attempt_recorded"] is True


def test_smoke_scope_stops_after_the_production_player_enrolls(tmp_path, monkeypatch):
    from scripts import test_appliance_e2e as e2e

    fixture = SimpleNamespace(up=lambda: None)
    monkeypatch.setattr(e2e.BootFixture, "prepare", lambda *args, **kwargs: fixture)
    harness = object.__new__(ApplianceE2E)
    harness.scope = "smoke"
    harness.state = tmp_path
    harness.inputs = {"bundle": tmp_path / "bundle", "deployment": tmp_path / "deployment"}
    harness.central_image = "sha256:" + "c" * 64
    harness.report = {"checks": {}, "boots": [], "qualification": e2e.unqualified()}
    harness.verify_inventory_transport = lambda: harness.report["checks"].update(
        central_inventory_pre_vm=True)
    harness.start_vm = lambda: None
    harness.wait_boot_report = lambda: report(trial=False)
    harness.verify_boot_attempt = lambda *_: harness.report["checks"].update(
        central_boot_attempt_recorded=True)

    def wait_enrollment(**_kwargs):
        harness.report["boots"].append(report(trial=False))
        return row()

    harness.wait_enrollment = wait_enrollment
    harness.execute()

    assert harness.fixture is fixture
    assert harness.report["first_enrollment"] == row().model_dump(mode="json")
    assert harness.report["checks"] == {
        "signed_https_dns_ntp": True,
        "central_inventory_pre_vm": True,
        "central_boot_attempt_recorded": True,
        "accepted_release_selected": True,
        "production_player_enrolled": True,
    }
    assert harness.report["qualification"]["generic_vm"] is True
    assert harness.report["qualification"]["automatic_rollback"] is False


def test_serial_health_does_not_promote_central_acceptance(monkeypatch):
    from scripts import test_appliance_e2e as e2e
    harness = object.__new__(ApplianceE2E)
    harness.report = dict(boots=[report()], checks={})
    harness.checked_vm = lambda: dict(Running=True)
    observations = iter([central_evidence(), central_evidence(status='healthy')])
    harness.boot_evidence = lambda boot: next(observations)
    monkeypatch.setattr(e2e.time, 'sleep', lambda _: None)
    harness.wait_central_health()
    assert harness.report['checks']['native_healthy_boot_accepted']
    assert harness.report['central_health'][BOOT]['status'] == 'healthy'


def test_current_ticket_is_required_for_central_boot_evidence():
    harness = object.__new__(ApplianceE2E)
    harness.release_probe = lambda *_: ReleaseEvidenceResult(schema_version=1, kind='release-evidence',
        evidence=central_evidence(ticket_sha256='f' * 64))
    with pytest.raises(FixtureError, match='central_boot_mismatch'):
        harness.boot_evidence(report())


@pytest.mark.parametrize("present", [True, False])
def test_health_timeout_retains_only_last_validated_central_observation(monkeypatch, present):
    from scripts import test_appliance_e2e as e2e

    harness = object.__new__(ApplianceE2E)
    harness.report = {"boots": [report()], "checks": {}}
    harness.checked_vm = lambda: {"Running": True}
    pending = central_evidence() if present else None
    harness.boot_evidence = lambda _: pending
    ticks = iter([100, 101, 641])
    monkeypatch.setattr(e2e.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(e2e.time, "sleep", lambda _: None)
    with pytest.raises(FixtureError, match="native_central_health_timeout"):
        harness.wait_central_health()
    assert harness.report["last_central_health"] == (pending.model_dump(mode="json") if pending else None)
    if present:
        saved = harness.report["last_central_health"]
        assert saved["status"] == "booting" and saved["current"] is True
        assert saved["ticket_sha256"] == report()["ticket_sha256"]
        assert "ticket_id" not in saved and "token" not in saved
    assert harness.report["checks"] == {}


@pytest.mark.parametrize('fault', [None, 'trial_status', 'trial_candidate', 'trial_release',
    'trial_accepted', 'fallback_candidate', 'fallback_release', 'fallback_trial',
    'fallback_session', 'fallback_epoch', 'fallback_device'])
def test_rollback_requires_consumed_failed_trial_and_central_fallback(tmp_path, fault):
    harness = object.__new__(ApplianceE2E)
    share = tmp_path / 'vm/share'
    share.mkdir(parents=True)
    (tmp_path / 'release.json').write_bytes(b'signed candidate manifest')
    (tmp_path / 'release.sig').write_bytes(b's' * 64)
    boots = [report(boot_id=f'{i}1234567-89ab-cdef-0123-456789abcdef',
                    ticket_sha256=str(i + 1) * 64, release_id=CANDIDATE if i == 2 else RELEASE,
                    trial=i == 2) for i in range(4)]
    harness.state = tmp_path
    harness.inputs = dict(candidate=SimpleNamespace(release_id=CANDIDATE), candidate_dir=tmp_path,
                          release=SimpleNamespace(release_id=RELEASE))
    harness.report = dict(boots=boots[:2], observed_boot_reports=boots, checks={}, recovery_event={})
    harness.release_probe = lambda *args: ReleaseStageResult(schema_version=1, kind='release-staged',
        staged=True, release_id=CANDIDATE)
    harness.wait_rollback_evidence = lambda predicate, **kwargs: None if predicate() else pytest.fail('missing evidence')
    def enroll(previous):
        harness.report['boots'].append(boots[3])
        return row(authority_epoch=3)
    harness.wait_enrollment = enroll
    failed = dict(status='failed', current=False, candidate_release_id=CANDIDATE)
    fallback = dict(current=True, candidate_release_id=CANDIDATE, current_authority_epoch=3)
    if fault == 'trial_status':
        failed['status'] = 'healthy'
    elif fault == 'trial_candidate':
        failed['candidate_release_id'] = None
    elif fault == 'trial_release':
        failed['release_id'] = 'f' * 64
    elif fault == 'trial_accepted':
        failed['accepted_release_id'] = 'f' * 64
    elif fault == 'fallback_candidate':
        fallback['candidate_release_id'] = None
    elif fault == 'fallback_release':
        fallback['release_id'] = 'f' * 64
    elif fault == 'fallback_trial':
        fallback.update(trial=True, trial_ticket_sha256=boots[3]['ticket_sha256'])
    elif fault == 'fallback_session':
        fallback['current_player_id'] = 'p-' + 'f' * 32
    elif fault == 'fallback_epoch':
        fallback['current_authority_epoch'] = 2
    elif fault == 'fallback_device':
        fallback['device_id'] = 'device-' + 'f' * 64
    harness.boot_evidence = lambda boot: central_evidence(boot, **(failed if boot['trial'] else fallback))
    if fault is not None:
        with pytest.raises(FixtureError, match='central_rollback_unproven'):
            harness.exercise_rollback(row(authority_epoch=2))
        assert not harness.report['checks'].get('production_automatic_rollback')
        assert not harness.report['checks'].get('central_trial_consumed')
    else:
        harness.exercise_rollback(row(authority_epoch=2))
        control = json.loads((share / 'control.json').read_bytes())
        assert control['action'] == 'reboot-for-trial' and control['schema'] == 2
        assert 'ticket_id' not in control['current']
        assert harness.report['checks']['production_automatic_rollback']
        assert harness.report['checks']['central_trial_consumed']
        assert harness.report['central_failed_trial']['trial_ticket_sha256'] == boots[2]['ticket_sha256']
        assert harness.report['central_fallback']['current_authority_epoch'] == 3
        assert harness.report['central_fallback']['release_id'] == RELEASE


def test_wait_enrollment_keeps_empty_and_prior_epoch_pending_and_serializes_ready(monkeypatch):
    from scripts import test_appliance_e2e as e2e

    harness = object.__new__(ApplianceE2E)
    harness.report = {'boots': []}
    harness.checked_vm = lambda: dict(Running=True, OOMKilled=False)
    samples = iter(((), (row(),), (row(authority_epoch=2),)))
    harness.inventory = lambda: next(samples)
    harness.serial = lambda: ''  # The verified bootstrap report may have left the log tail.
    harness.observe_serial = lambda _: [report()]
    harness.enrollment_probe = lambda _name, callback: callback()
    pending = []
    def wait(_):
        pending.append(json.loads(json.dumps(harness.report)))
    monkeypatch.setattr(e2e.time, 'sleep', wait)

    assert harness.wait_enrollment(previous=row(), boot=report()) == row(authority_epoch=2)
    assert len(pending) == 2
    assert all(item['last_enrollment_observation']['state'] == 'pending' and not item['boots'] for item in pending)
    serialized = json.loads(json.dumps(harness.report))
    assert serialized['last_enrollment_observation'] == dict(state='ready', session=row(authority_epoch=2).model_dump())
    assert serialized['last_inventory'] == [row(authority_epoch=2).model_dump()]
    assert serialized['boots'] == [report()]


def test_wait_enrollment_rejects_equipment_other_than_the_selected_boot():
    harness = object.__new__(ApplianceE2E)
    harness.report = {'boots': []}
    harness.checked_vm = lambda: dict(Running=True, OOMKilled=False)
    harness.inventory = lambda: (row(device_id='device-'+'f'*64),)
    harness.serial = lambda: ''
    harness.observe_serial = lambda _: [report()]
    harness.enrollment_probe = lambda _name, callback: callback()
    with pytest.raises(FixtureError, match='equipment_identity_changed'):
        harness.wait_enrollment(boot=report())
    assert harness.report['boots'] == []
