"""Fail-closed evidence binding for the real image boot harness."""

import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from scripts.boot_fixture import FixtureError
from scripts.test_appliance_e2e import (
    ApplianceE2E,
    boot_reports,
    checked_inputs,
    enrollment,
    rollback_event,
    trial_acceptance,
    trial_phases,
)

RELEASE = "a" * 64
CANDIDATE = "c" * 64
BOOT = "01234567-89ab-cdef-0123-456789abcdef"
PLAYER = "p-" + "b" * 32


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


def report(**changes):
    return dict(schema=1, boot_id=BOOT, release_id=RELEASE, slot="A", trial=True,
                persistence="durable", fault=None) | changes


def row(**changes):
    return dict(player_id=PLAYER, authority_epoch=1, persistence="durable", retired=False) | changes


def test_console_noise_and_duplicate_reports_do_not_invent_boots():
    serial = "kernel messages\n" + json.dumps(report()) + "\n" + json.dumps(report())
    assert boot_reports(serial, RELEASE) == [report()]
    assert boot_reports("invalid {not json}\n", RELEASE) == []


def acceptance_event(**changes):
    return dict(event="photo-wall-trial-acceptance", boot_id=BOOT, accepted=True) | changes


def test_only_completed_acceptance_for_selected_boot_counts():
    noise = "service: " + json.dumps(acceptance_event(boot_id="other")) + "\n"
    noise += json.dumps(acceptance_event(accepted=False)) + "\ninvalid {json\n"
    assert trial_acceptance(noise, BOOT) is None
    assert trial_acceptance(noise + "systemd: " + json.dumps(acceptance_event()), BOOT) == acceptance_event()


def test_acceptance_events_do_not_masquerade_as_boot_reports():
    serial = "\n".join(json.dumps(value) for value in (report(), acceptance_event()))
    assert boot_reports(serial, RELEASE) == [report()]
    assert trial_acceptance(serial, BOOT) == acceptance_event()


def test_trial_diagnostics_are_boot_bound_and_cannot_count_as_acceptance():
    events = [dict(event="photo-wall-trial-phase", boot_id=BOOT, phase=phase)
              for phase in ("verifying", "health")]
    serial = "\n".join(json.dumps(item) for item in events)
    assert trial_phases(serial, BOOT) == ["verifying", "health"]
    assert trial_phases(serial, "another-boot") == []
    assert trial_acceptance(serial, BOOT) is None
    assert boot_reports(serial, RELEASE) == []
    harness = object.__new__(ApplianceE2E)
    harness.report = {}
    harness.record_trial_events(serial, [report()])
    first = harness.report["trial_phases"][BOOT].copy()
    harness.record_trial_events(serial, [report()])
    assert harness.report["trial_phases"][BOOT] == first
    assert all(set(item) == {"phase", "observed_at"} for item in first)
    assert harness.report["trial_acceptances"] == {}


@pytest.mark.parametrize("changes", [dict(phase="accepted"), dict(private="secret")])
def test_trial_diagnostics_reject_unknown_fields_or_phases(changes):
    event = dict(event="photo-wall-trial-phase", boot_id=BOOT, phase="health") | changes
    with pytest.raises(FixtureError, match="invalid_trial_phase_event"):
        trial_phases(json.dumps(event), BOOT)


@pytest.mark.parametrize("changes", [dict(accepted=1), dict(accepted="true"), dict(private="hidden")])
def test_malformed_acceptance_event_fails_closed(changes):
    with pytest.raises(FixtureError, match="invalid_trial_acceptance_event"):
        trial_acceptance(json.dumps(acceptance_event(**changes)), BOOT)


def test_native_trial_wait_retains_event_and_requires_fresh_trial():
    harness = object.__new__(ApplianceE2E)
    harness.report = dict(boots=[report()], checks={})
    harness.checked_vm = lambda: dict(Running=True)
    harness.serial = lambda: json.dumps(acceptance_event())
    harness.wait_trial_acceptance()
    assert harness.report["trial_acceptances"] == {BOOT: acceptance_event()}
    assert harness.report["checks"]["native_healthy_trial_promoted"] is True
    harness.report["boots"][0]["trial"] = False
    with pytest.raises(FixtureError, match="fresh_boot_not_trial"):
        harness.wait_trial_acceptance()


def test_trial_wait_times_out_without_turning_enrollment_into_acceptance(monkeypatch):
    from scripts import test_appliance_e2e as e2e

    now = [0]
    monkeypatch.setattr(e2e, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)))
    harness = object.__new__(ApplianceE2E)
    harness.report = dict(boots=[report()], checks={})
    harness.checked_vm = lambda: dict(Running=True)
    harness.serial = lambda: json.dumps(acceptance_event(accepted=False))
    with pytest.raises(FixtureError, match="native_trial_acceptance_timeout"):
        harness.wait_trial_acceptance()
    assert harness.report["checks"] == {}


def test_failed_native_trial_service_stops_without_waiting_for_host_deadline(monkeypatch):
    from scripts import test_appliance_e2e as e2e

    monkeypatch.setattr(e2e.time, "sleep", lambda _: pytest.fail("waited after terminal failure"))
    harness = object.__new__(ApplianceE2E)
    harness.report = dict(boots=[report()], checks={})
    harness.checked_vm = lambda: dict(Running=True)
    harness.serial = lambda: "photo-wall-accept-trial.service: Main process exited, code=exited, status=1/FAILURE"
    with pytest.raises(FixtureError, match="native_trial_service_failed"):
        harness.wait_trial_acceptance()
    assert harness.report["checks"] == {}


@pytest.mark.parametrize("restart_trial", [False, True])
def test_image_execution_requires_accepted_state_on_actual_restart(tmp_path, monkeypatch, restart_trial):
    from scripts import test_appliance_e2e as e2e

    fixture = SimpleNamespace(up=lambda: None)
    monkeypatch.setattr(e2e.BootFixture, "prepare", lambda *args: fixture)
    monkeypatch.setattr(e2e.time, "sleep", lambda seconds: None)
    harness = object.__new__(ApplianceE2E)
    harness.state, harness.name, harness.central_image = tmp_path, "vm", "central-image"
    harness.inputs = dict(bundle=tmp_path, deployment=tmp_path)
    harness.report = dict(boots=[], checks={}, qualification=e2e.unqualified())
    rows = iter(([], [row(authority_epoch=2)]))
    harness.inventory = lambda: next(rows)
    harness.start_vm = lambda: None
    harness.fixture_central = lambda: "central"
    harness.wait_player_requests = lambda since: None
    harness.exercise_rollback = lambda previous: None
    running = [True]
    harness.checked_vm = lambda: dict(Running=running[0])
    harness.serial = lambda: json.dumps(acceptance_event())

    def command(args, **kwargs):
        if args[-1] == "vm":
            running[0] = args[1] == "start"

    harness.run = command

    def enroll(previous=None):
        boot = report() if previous is None else report(
            boot_id="11234567-89ab-cdef-0123-456789abcdef", trial=restart_trial)
        harness.report["boots"].append(boot)
        return row(authority_epoch=1 if previous is None else 2)

    harness.wait_enrollment = enroll
    if restart_trial:
        with pytest.raises(FixtureError, match="accepted_trial_not_durable"):
            harness.execute()
        assert not any(harness.report["qualification"].values())
    else:
        harness.execute()
        assert harness.report["qualification"]["healthy_trial"]
        assert harness.report["qualification"]["generic_vm"]
        assert not harness.report["qualification"]["native_rendering"]
        assert harness.report["qualification"]["automatic_rollback"]


@pytest.mark.parametrize("changes", [dict(release_id="c" * 64), dict(persistence="volatile"),
                                     dict(fault="update_storage"), dict(boot_id="invalid")])
def test_faulted_or_wrong_release_boot_cannot_pass(changes):
    with pytest.raises(FixtureError, match="guest_boot_report_invalid"):
        boot_reports(json.dumps(report(**changes)), RELEASE)


def test_old_inventory_cannot_prove_restart_and_changed_identity_fails():
    assert enrollment([]) is None
    assert enrollment([row()], row()) is None
    assert enrollment([row(authority_epoch=2)], row())["authority_epoch"] == 2
    with pytest.raises(FixtureError, match="durable_identity_changed"):
        enrollment([row(player_id="p-" + "c" * 32, authority_epoch=2)], row())
    with pytest.raises(FixtureError, match="unexpected_player_count"):
        enrollment([row(), row()])
    with pytest.raises(FixtureError, match="invalid_guest_enrollment"):
        enrollment([row(persistence="volatile")])


def test_enrollment_can_follow_a_verified_boot_report_leaving_the_log_tail(monkeypatch):
    from scripts import test_appliance_e2e as e2e

    harness = object.__new__(ApplianceE2E)
    harness.report = {"boots": []}
    harness.inputs = {"release": SimpleNamespace(release_id=RELEASE)}
    harness.checked_vm = lambda: dict(Running=True, OOMKilled=False)
    inventories = iter(([], [row()]))
    serials = iter((json.dumps(report()), "later service messages"))
    harness.inventory = lambda: next(inventories)
    harness.serial = lambda: next(serials)
    monkeypatch.setattr(e2e.time, "sleep", lambda seconds: None)
    assert harness.wait_enrollment() == row()
    assert harness.report["boots"] == [report()]
    assert harness.report["last_inventory_count"] == 1


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
                (("A", RELEASE), ("B", CANDIDATE))}
    metadata = dict(schema=1, kind="ci-rollback-candidate", source_revision="f" * 40,
                    boot_abi="b" * 64, configuration_sha256="d" * 64,
                    fault_path=FAULT_PATH, content_sha256=hashlib.sha256(FAULT_CONTENT).hexdigest(),
                    slots={slot: {key: getattr(value, key) for key in
                           ("release_id", "rootfs_sha256", "rootfs_size")} for slot, value in releases.items()})
    put(candidate_dir / "candidate.json", metadata)
    monkeypatch.setattr("scripts.boot_gateway.BootBundle.load", lambda directory, *_:
                        SimpleNamespace(release=releases["B" if directory == candidate_dir else "A"]))
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
                candidate["metadata"]["slots"]["B"]["rootfs_sha256"] = "0" * 64
            elif change == "abi":
                candidate["metadata"]["boot_abi"] = "0" * 64
            else:
                candidate["metadata"]["content_sha256"] = "0" * 64
            from pathlib import Path
            (Path(candidate["path"]) / "candidate.json").write_text(json.dumps(candidate["metadata"]))
    manifest.write_text(json.dumps(data))
    with pytest.raises(FixtureError, match="rollback_candidate_required|candidate_.*mismatch"):
        checked_inputs(manifest)


def test_rollback_event_requires_exact_fields_and_boolean_types():
    expected = dict(event="photo-wall-rollback-allowed", boot_id=BOOT, allowed=True)
    assert rollback_event(json.dumps(expected), expected) == expected
    assert rollback_event(json.dumps(expected | dict(boot_id="old")), expected) is None
    for changes in (dict(allowed=1), dict(extra="private"), dict(allowed=False)):
        with pytest.raises(FixtureError, match="invalid_rollback_event"):
            rollback_event(json.dumps(expected | changes), expected)


def rollback_harness(tmp_path, monkeypatch, fault=None):
    from scripts import test_appliance_e2e as e2e

    now = [0]
    monkeypatch.setattr(e2e, "time", SimpleNamespace(monotonic=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)))
    harness = object.__new__(ApplianceE2E)
    harness.state = tmp_path
    (tmp_path / "vm/share").mkdir(parents=True)
    harness.inputs = dict(release=SimpleNamespace(release_id=RELEASE),
                          candidate=SimpleNamespace(release_id=CANDIDATE))
    boots = [report(boot_id=f"{number}1234567-89ab-cdef-0123-456789abcdef",
                    release_id=CANDIDATE if number == 2 else RELEASE,
                    slot="B" if number == 2 else "A", trial=number in {0, 2})
             for number in range(4)]
    harness.report = dict(boots=boots[:2], observed_boot_reports=boots[:2], checks={})
    harness.checked_vm = lambda: dict(Running=True, OOMKilled=False)
    stage = dict(event="photo-wall-stage-trial", boot_id=boots[1]["boot_id"],
                 current_release_id=RELEASE, candidate_release_id=CANDIDATE, slot="B")
    recovery = dict(event="photo-wall-rollback-allowed", boot_id=boots[2]["boot_id"], allowed=True)
    if fault == "wrong_stage":
        stage["candidate_release_id"] = RELEASE
    if fault == "stale_stage":
        stage["boot_id"] = boots[0]["boot_id"]
    if fault == "stale_recovery":
        recovery["boot_id"] = boots[0]["boot_id"]
    if fault == "trial_fallback":
        boots[3]["trial"] = True
    observations = [stage, boots[2], recovery, boots[3]]
    if fault == "missing_trial":
        observations.remove(boots[2])
    if fault == "candidate_accepted":
        observations.insert(2, acceptance_event(boot_id=boots[2]["boot_id"]))
    if fault == "panic":
        observations.insert(2, "Kernel panic - not syncing")
    if fault == "loop":
        observations[-1] = json.dumps(boots[3]) + "\n" + json.dumps(report(
            boot_id="41234567-89ab-cdef-0123-456789abcdef", trial=False))
    serials = iter(observations)
    seen = []

    def serial():
        value = next(serials, None)
        if value is not None:
            seen.append(value if isinstance(value, str) else json.dumps(value))
        return "\n".join(seen)

    harness.serial = serial
    restored = row(authority_epoch=2 if fault == "stale_inventory" else 3)
    if fault == "changed_identity":
        restored["player_id"] = "p-" + "d" * 32
    harness.inventory = lambda: [restored]
    harness.run = lambda *_args, **_kwargs: pytest.fail("host attempted to control fallback")
    return harness, boots


def test_rollback_requires_production_reboot_and_new_identity_epoch(tmp_path, monkeypatch):
    harness, boots = rollback_harness(tmp_path, monkeypatch)
    harness.exercise_rollback(row(authority_epoch=2))
    assert harness.report["boots"] == boots
    assert harness.report["fallback_enrollment"] == row(authority_epoch=3)
    assert all(harness.report["checks"].values())
    control = json.loads((tmp_path / "vm/share/control.json").read_text())
    assert control == dict(schema=1, action="stage-trial", current={key: boots[1][key]
        for key in ("boot_id", "release_id", "slot")}, candidate=dict(release_id=CANDIDATE))


@pytest.mark.parametrize("fault, failure", [
    ("wrong_stage", "invalid_rollback_event"), ("stale_stage", "candidate_staging_timeout"),
    ("stale_recovery", "production_recovery_timeout"), ("missing_trial", "unexpected_boot_sequence"),
    ("trial_fallback", "unexpected_boot_sequence"), ("candidate_accepted", "failed_candidate_accepted"),
    ("panic", "guest_crashed_during_rollback"), ("loop", "unexpected_boot_count"),
    ("stale_inventory", "guest_enrollment_timeout"), ("changed_identity", "durable_identity_changed"),
])
def test_unrelated_restarts_and_incomplete_rollback_cannot_pass(tmp_path, monkeypatch, fault, failure):
    harness, _ = rollback_harness(tmp_path, monkeypatch, fault)
    with pytest.raises(FixtureError, match=failure):
        harness.exercise_rollback(row(authority_epoch=2))
    assert "production_automatic_rollback" not in harness.report["checks"]


def test_serial_diagnostics_keep_only_fixed_public_fault_names():
    from scripts.test_appliance_e2e import serial_diagnostics

    result = serial_diagnostics(
        "systemd[1]: \x1b[0;31msystemd-networkd.service: Main process exited, status=200/CHDIR\x1b[0m\n"
        "ModuleNotFoundError: private-input-must-not-escape\n"
        "arbitrary-token.service: Failed with result secret\n")
    assert result == dict(systemd_chdir_failure=True, kernel_panic=False, out_of_memory=False,
                         failed_services=["systemd-networkd"], service_exit_status={},
                         namespace_failures={}, player_faults=[],
                         python_errors=["ModuleNotFoundError"])
    assert "private-input" not in json.dumps(result)
    assert "arbitrary-token" not in json.dumps(result)


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
        dict(path="player_state", errno="EROFS"),
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


def test_health_samples_are_bounded_boot_bound_and_never_promote_a_trial():
    from scripts.vm_health_probe import sample_once

    sample = sample_once(1, BOOT, 100, report_reader=lambda: {},
        systemctl_runner=lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="private-secret"),
        socket_state=lambda: "missing")
    harness = object.__new__(ApplianceE2E)
    harness.report = dict(boots=[report()], checks={})
    values = [sample | dict(sample_index=index) for index in range(1, 62)]
    values += [sample | dict(boot_id="11111111-1111-1111-1111-111111111111"),
               sample | dict(private="private-secret")]
    serial = "\n".join(json.dumps(value) for value in values)
    harness.record_trial_events(serial, [report()])
    harness.record_trial_events(serial, [report()])
    assert len(harness.report["health_diagnostics"][BOOT]) == 60
    assert harness.report["trial_acceptances"] == {} and harness.report["checks"] == {}
    assert boot_reports(serial, RELEASE) == []
    assert "private-secret" not in json.dumps(harness.report)


def test_serial_player_faults_export_only_known_complete_codes():
    from scripts.test_appliance_e2e import serial_diagnostics

    result = serial_diagnostics("player fault: native_initialization\n"
        "player fault: secret_token\nplayer fault: connection_failed_extra\n"
        "player fault: health_storage\n")
    assert result["player_faults"] == ["native_initialization", "health_storage"]
    assert "secret_token" not in json.dumps(result)
