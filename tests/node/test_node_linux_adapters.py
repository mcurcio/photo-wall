"""Linux boundary policies exercised portably; these do not claim systemd or pixels."""
from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import pytest
from support.repo import REPO

from appliance.apps.environment import (
    FORMAT,
    canonical_bytes,
    capacity,
    inventory,
    verify_root,
)
from appliance.host.host import HostCore, RebootRequest
from appliance.host.host_storage import FileRebootJournal
from appliance.kernel.boot_store import BootStore
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_commands import reboot_digest
from contracts.node_protocol import NodeProducerV2

# A fixed replacement identity: a parameter value must be the same in every collection (xdist
# workers each collect, and must agree).
REPLACEMENT_INVOCATION = "6b8cd57b-2a55-4ba0-89bc-456463455201"


def store(directory, boot, policy=None):
    directory.mkdir(mode=0o700, exist_ok=True)
    return BootStore(directory, boot_id=boot, policy=policy or {"owner": "test"}, owner_uid=os.getuid())


def test_boot_store_lock_binding_atomic_restart_and_close(tmp_path):
    boot = uuid4()
    first = store(tmp_path, boot)
    first.write("intent", {"started": True})
    with pytest.raises(BlockingIOError):
        store(tmp_path, boot)
    first.close()
    with pytest.raises(ValueError, match="poisoned"):
        first.write("intent", {})
    second = store(tmp_path, boot)
    assert second.read("intent") == {"started": True}
    second.close()
    with pytest.raises(ValueError, match="boot_or_policy"):
        store(tmp_path, uuid4())


def test_boot_store_rejects_symlink_and_corrupt_json(tmp_path):
    current = store(tmp_path, uuid4())
    (tmp_path / "outside").write_text('{}')
    (tmp_path / "bad.json").symlink_to(tmp_path / "outside")
    with pytest.raises(OSError):
        current.read("bad")
    current.write("duplicate", {})
    (tmp_path / "duplicate.json").write_text('{"x":1,"x":2}')
    with pytest.raises(ValueError, match="corrupt"):
        current.read("duplicate")
    current.close()


def test_reboot_unknown_effect_cannot_repeat_after_service_restart(tmp_path):
    boot, session, offer = uuid4(), uuid4(), uuid4()
    producer = NodeProducerV2("site", "device-" + "a" * 64, 1, boot, "host_core", uuid4())
    request = RebootRequest(uuid4(), "0" * 64, session, offer, producer)
    request = replace(request, command_sha256=reboot_digest(request))

    class Driver:
        calls = 0
        def initiate(self):
            self.calls += 1
            raise OSError("reply lost")

    driver = Driver()
    journal_store = store(tmp_path, boot)
    core = HostCore(producer=producer, session_id=session, offer_id=offer,
                    journal=FileRebootJournal(journal_store), driver=driver)
    assert core.receive(request, now_ms=1).decision == "accepted"
    assert core.initiate(request.command_id, now_ms=2).effect_unknown
    journal_store.close()
    recovered = store(tmp_path, boot)
    core = HostCore(producer=producer, session_id=session, offer_id=offer,
                    journal=FileRebootJournal(recovered), driver=driver)
    assert core.initiate(request.command_id, now_ms=3).effect_unknown
    assert driver.calls == 1
    recovered.close()


def fixture_root(directory: Path, *, deb_name: str = "player"):
    """A sealed release root as the image's mount shows it (rootfs/ and the three metadata files),
    and its reference, the digest and size placeholders standing for the image's."""
    source = directory / "source"
    root = source / "rootfs"
    root.mkdir(parents=True)
    entry = root / "entry"
    entry.write_text('#!/bin/sh\nexit 0\n')
    entry.chmod(0o755)
    python = root / "usr/bin/python3"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    lock, snapshot = b'{"packages":[]}', b'{"snapshot":"fixture"}'
    reference = AppEnvironmentRefV2("0" * 64, 1, "a" * 64, deb_name, "1.0", "amd64",
                                    hashlib.sha256(lock).hexdigest(), hashlib.sha256(snapshot).hexdigest(),
                                    "/entry", "base-v2", "graphics-v2", "plugins-v2")
    fields = asdict(reference)
    fields.pop("environment_sha256")
    fields.pop("size_bytes")
    (source / "environment.json").write_bytes(canonical_bytes({"schema": 2, "format": FORMAT, "reference": fields, "files": inventory(root), "capacity": capacity(inventory(root))}))
    (source / "dependency-lock.json").write_bytes(lock)
    (source / "sources.json").write_bytes(snapshot)
    for path in (source, *source.rglob("*")):
        if path.is_dir():
            path.chmod(0o755)
    return source, reference


def test_node_base_units_have_no_boot_switch():
    from node.apps.test_memory_lines import NODE_UNITS

    assert NODE_UNITS and not any("ConditionKernelCommandLine" in path.read_text()
                                  for path in NODE_UNITS)
    assert not any(path.name.endswith(".d") for path in (REPO / "appliance/systemd").iterdir())


def test_manager_no_fallback_exhausts_primary_once():
    from appliance.node.manager import ManagerRecovery, ManagerRecoveryState
    class Store:
        value = ManagerRecoveryState()
        def load(self): return self.value
        def save(self, value): self.value = value
    class Launcher:
        calls = 0
        def verify(self, root): return True
        def start(self, root):
            self.calls += 1
            return False
    launcher = Launcher()
    recovery = ManagerRecovery(launcher, primary="a" * 64, fallback=None, store=Store())
    assert recovery.recover().fault == "manager_recovery_required"
    recovery.recover()
    assert launcher.calls == 1


def test_safe_dangling_and_ancestor_alias_inventory_preserved(tmp_path):
    (tmp_path / "usr/share/X11").mkdir(parents=True)
    (tmp_path / "usr/share/X11/X11").symlink_to(".")
    (tmp_path / "optional").symlink_to("/etc/optional-missing")
    assert inventory(tmp_path) == {"optional": {"link": "/etc/optional-missing"}, "usr/share/X11/X11": {"link": "."}}


def test_inventory_rejects_real_xattrs_but_accepts_unsupported_fs(tmp_path, monkeypatch):
    import errno

    from appliance.apps import environment
    (tmp_path / "file").write_text("bytes")
    monkeypatch.setattr(environment.os, "listxattr", lambda *a, **kw: ["security.capability"], raising=False)
    with pytest.raises(ValueError, match="xattrs"):
        inventory(tmp_path)
    def unsupported(*args, **kwargs):
        raise OSError(errno.ENOTSUP, "unsupported")
    monkeypatch.setattr(environment.os, "listxattr", unsupported)
    assert inventory(tmp_path)["file"]["size"] == 5


def test_diskless_budget_deduplicates_exact_roots_and_refuses_small_memory(tmp_path):
    from appliance.kernel.capacity import GIB, MIB, admit_cold, cold_peak
    _, reference = fixture_root(tmp_path)
    reference = replace(reference, size_bytes=300 * MIB)
    # Each distinct image is held once: no unpack beside it, no second copy.
    assert cold_peak([reference, reference, None]) == 300 * MIB
    assert admit_cold([reference, reference], total=8 * GIB, available=7 * GIB, free=4 * GIB) == 300 * MIB
    # A 4 GB board is a class of its own (store 768 MiB); below it, the class refuses.
    assert admit_cold([reference], total=4 * GIB, available=3 * GIB, free=4 * GIB) == 300 * MIB
    with pytest.raises(ValueError, match="node_memory_class"):
        admit_cold([reference], total=3 * GIB, available=3 * GIB, free=4 * GIB)
    with pytest.raises(ValueError, match="capacity"):
        admit_cold([reference], total=8 * GIB, available=7 * GIB, free=200 * MIB)


def test_online_capacity_counts_only_incremental_available_memory():
    from appliance.kernel.capacity import GIB, MIB, admit_preparation
    size = 288 * MIB
    # Cold app and manager images already on the store; the app's resident pages reduce
    # MemAvailable independently, so only the target's image is incremental.
    used = 350 * MIB
    assert admit_preparation(size, total=8 * GIB, available=GIB, free=4 * GIB - used, used=used) == size
    with pytest.raises(ValueError, match="capacity"):
        admit_preparation(size, total=8 * GIB, available=700 * MIB, free=4 * GIB - used, used=used)
    with pytest.raises(ValueError, match="capacity"):
        admit_preparation(size, total=8 * GIB, available=6 * GIB, free=4 * GIB, used=638 * MIB)


def test_systemd_adapter_normalizes_real_invocation_and_refuses_malformed(monkeypatch):
    from types import SimpleNamespace

    from appliance.apps import process_linux
    incarnation = uuid4()
    raw = f"LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=12\nInvocationID={incarnation.hex}\nControlGroup=/photowallapp.slice/u.service\nRootDirectory=/root\n"
    monkeypatch.setattr(process_linux.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=raw))
    assert process_linux.systemctl_show("u.service")["InvocationID"] == str(incarnation)
    raw = raw.replace(incarnation.hex, "not-a-uuid")
    with pytest.raises(ValueError):
        process_linux.systemctl_show("u.service")


def test_expected_process_root_is_pinned_and_symlink_replacement_refused(tmp_path):
    from appliance.apps.process_linux import process_root_matches
    root = tmp_path / "root"
    root.mkdir()
    proc = tmp_path / "proc/12"
    proc.mkdir(parents=True)
    (proc / "root").symlink_to(root)
    assert process_root_matches(tmp_path / "proc", 12, root)
    renamed = tmp_path / "old"
    root.rename(renamed)
    root.symlink_to(renamed)
    assert not process_root_matches(tmp_path / "proc", 12, root)


def test_environment_rejects_rootfs_directory_with_wrong_owner(tmp_path, monkeypatch):
    target, reference = fixture_root(tmp_path)
    abi = dict(base_abi="base-v2", graphics_abi="graphics-v2", plugin_abi="plugins-v2", owner_uid=os.getuid())
    assert verify_root(target, reference, **abi) == target / "rootfs"
    original = Path.lstat

    def wrong_root_owner(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == target / "rootfs":
            fields = list(info)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", wrong_root_owner)
    with pytest.raises(ValueError, match="environment_member_ownership"):
        verify_root(target, reference, **abi)


@pytest.fixture
def stop_driver(tmp_path, monkeypatch):
    from appliance.apps import process_linux as linux
    from appliance.apps.broker import RunningApp
    from contracts.node_protocol import NodeProcessIdentity

    _, reference = fixture_root(tmp_path)
    expected = RunningApp(reference, NodeProcessIdentity(321, 1234, uuid4()), 4, uuid4())
    driver = linux.SystemdAppProcessDriver(tmp_path / "roots", None, base_abi="base-v2",
        graphics_abi="graphics-v2", plugin_abi="plugins-v2", proc=tmp_path / "proc", cgroups=tmp_path / "cgroups")
    group = "/photowallapp.slice/photo-wall-node-player.service"
    rows = dict(LoadState="loaded", ActiveState="deactivating", SubState="stop-sigterm", MainPID="321",
                InvocationID=str(expected.process.invocation_id), ControlGroup=group,
                RootDirectory=str(driver.roots / reference.environment_sha256 / "rootfs"))
    (driver.proc / "321").mkdir(parents=True)
    (driver.proc / "321/cgroup").write_text("0::" + group + "\n")
    members = driver.cgroups / group.lstrip("/") / "cgroup.events"
    members.parent.mkdir(parents=True)
    members.write_text("populated 0\nfrozen 0\n")
    ticks = {"value": 1234}
    monkeypatch.setattr(linux, "systemctl_show", lambda unit: dict(rows))
    monkeypatch.setattr(linux, "read_proc_start_ticks", lambda proc, pid: ticks["value"])
    monkeypatch.setattr(linux, "process_root_matches", lambda *args: True)
    return linux, driver, expected, group, rows, ticks, members


def test_stop_observation_pending_does_not_relax_public_current(stop_driver, monkeypatch):
    linux, driver, expected, group, rows, ticks, _ = stop_driver
    assert not driver._stop_sample(expected, group)
    with pytest.raises(ValueError, match="app_process_ambiguous"):
        driver._observe(expected.environment, expected.operation_id, expected.app_epoch)
    rows["MainPID"] = "0"
    assert not driver._stop_sample(expected, group)
    rows["MainPID"] = "321"
    ticks["value"] = None
    assert not driver._stop_sample(expected, group)
    ticks["value"] = 1234
    monkeypatch.setattr(driver, "_process_observation", lambda *args: (_ for _ in ()).throw(ValueError("app_process_changed_during_observation")))
    assert not driver._stop_sample(expected, group)


@pytest.mark.parametrize("field,value", [("MainPID", "322"), ("InvocationID", REPLACEMENT_INVOCATION),
    ("RootDirectory", "/wrong"), ("ControlGroup", "/photowallapp.slice/other.service")])
def test_stop_observation_refuses_replacement_identity(stop_driver, field, value):
    _, driver, expected, group, rows, _, _ = stop_driver
    rows[field] = value
    with pytest.raises(ValueError, match="app_stop_identity_changed"):
        driver._stop_sample(expected, group)


def test_stop_observation_refuses_changed_birth_and_kernel_cgroup(stop_driver):
    _, driver, expected, group, _, ticks, _ = stop_driver
    ticks["value"] = 9999
    with pytest.raises(ValueError, match="app_stop_identity_changed"):
        driver._stop_sample(expected, group)
    ticks["value"] = 1234
    (driver.proc / "321/cgroup").write_text("0::/other\n")
    with pytest.raises(ValueError, match="app_process_cgroup_mismatch"):
        driver._stop_sample(expected, group)


def test_stop_terminal_requires_process_cgroup_and_job_absence(stop_driver, monkeypatch):
    _, driver, expected, group, rows, ticks, members = stop_driver
    rows.update(ActiveState="inactive", SubState="dead", MainPID="0", InvocationID="", ControlGroup="")
    monkeypatch.setattr(driver, "absent_and_quiescent", lambda: True)
    assert not driver._stop_sample(expected, group)  # Old process still exists.
    ticks["value"] = None
    members.write_text("populated 1\nfrozen 0\n")
    assert not driver._stop_sample(expected, group)  # Remaining child is not exit.
    members.write_text("populated 0\nfrozen 0\n")
    monkeypatch.setattr(driver, "absent_and_quiescent", lambda: False)
    assert not driver._stop_sample(expected, group)  # Pending PID1 job.
    monkeypatch.setattr(driver, "absent_and_quiescent", lambda: True)
    assert driver._stop_sample(expected, group)
    members.unlink()
    with pytest.raises(ValueError, match="app_stop_cgroup_evidence_missing"):
        driver._stop_sample(expected, group)
    members.parent.rmdir()
    rows["LoadState"] = "not-found"
    assert driver._stop_sample(expected, group)


def test_player_health_has_bounded_private_mount():
    from appliance.apps.process_linux import app_unit_properties
    props = app_unit_properties(Path("/sealed/rootfs"), ())
    temporary = next(value for value in props if value.startswith("TemporaryFileSystem="))
    assert "/run/photo-wall/player:rw,nosuid,nodev,noexec,size=1M,uid=10004,gid=10004,mode=0700" in temporary.split()
    assert not any(value.startswith(("BindPaths=", "RuntimeDirectory=")) for value in props)


@pytest.mark.parametrize("field,value", [("InvocationID", REPLACEMENT_INVOCATION), ("RootDirectory", "/wrong"),
    ("ControlGroup", "/photowallapp.slice/replacement.service")])
def test_terminal_stop_observation_rejects_retained_replacement(stop_driver, monkeypatch, field, value):
    _, driver, expected, group, rows, ticks, _ = stop_driver
    rows.update(ActiveState="inactive", SubState="dead", MainPID="0")
    rows[field] = value
    ticks["value"] = None
    monkeypatch.setattr(driver, "absent_and_quiescent", lambda: True)
    with pytest.raises(ValueError, match="app_stop_identity_changed"):
        driver._stop_sample(expected, group)


@pytest.mark.parametrize("collected", [False, True])
def test_start_preflight_waits_for_collection_without_mutation(stop_driver, monkeypatch, collected):
    from appliance.apps.lifecycle_storage import primitive
    linux, driver, expected, _, rows, _, _ = stop_driver
    rows.update(ActiveState="inactive", SubState="dead", MainPID="0")
    monkeypatch.setattr(driver, "current", lambda: None)
    monkeypatch.setattr(driver, "_stop_sample", lambda *args: True)
    calls = []
    monkeypatch.setattr(linux.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    clock = iter(range(20))
    monkeypatch.setattr(linux.time, "monotonic", lambda: next(clock))
    sleeps = []
    def advance(value):
        sleeps.append(value)
        if collected:
            rows["LoadState"] = "not-found"
    monkeypatch.setattr(linux.time, "sleep", advance)
    previous = {"running": primitive(expected)}
    if collected:
        driver._await_unit_unloaded(previous)
        assert sleeps == [0.05]
    else:
        with pytest.raises(ValueError, match="app_previous_unit_not_unloaded"):
            driver._await_unit_unloaded(previous)
    assert calls == []


@pytest.mark.parametrize("collected", [False, True])
def test_failed_exec_intent_waits_for_collection_before_fallback(stop_driver, monkeypatch, collected):
    from appliance.apps.lifecycle_storage import primitive
    linux, driver, expected, _, rows, _, _ = stop_driver
    rows.update(ActiveState="failed", SubState="failed", MainPID="0")
    monkeypatch.setattr(driver, "current", lambda: None)
    monkeypatch.setattr(driver, "absent_and_quiescent", lambda: True)
    calls = []
    monkeypatch.setattr(linux.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    clock = iter(range(20))
    monkeypatch.setattr(linux.time, "monotonic", lambda: next(clock))
    def advance(value):
        if collected:
            rows["LoadState"] = "not-found"
    monkeypatch.setattr(linux.time, "sleep", advance)
    previous = {"running": None, "environment": primitive(expected.environment),
                "epoch": 5, "operation_id": str(uuid4())}
    if collected:
        driver._await_unit_unloaded(previous)
    else:
        with pytest.raises(ValueError, match="app_previous_unit_not_unloaded"):
            driver._await_unit_unloaded(previous)
    assert calls == []
    for load_state in ("loaded", "not-found"):
        rows.update(LoadState=load_state, RootDirectory="/replacement")
        clock = iter(range(20))
        with pytest.raises(ValueError, match="app_stop_identity_changed"):
            driver._await_unit_unloaded(previous)


def test_a_start_records_its_boottime_and_the_unit_dumps_python_stacks(stop_driver, tmp_path,
                                                                     monkeypatch):
    """The broker paces relaunches from `launched().started_ms`, which `start` wrote; a crash
    leaves the Python stack in the journal (PYTHONFAULTHANDLER=1)."""
    from appliance.apps.broker import Launch
    from appliance.apps.lifecycle_storage import primitive
    linux, driver, expected, _, _, _, _ = stop_driver
    driver.store = store(tmp_path / "broker", uuid4())
    try:
        driver.store.write("selected", {"environment": primitive(expected.environment)})
        assert driver.launched() is None
        observed = []
        monkeypatch.setattr(driver, "current", lambda: None)
        monkeypatch.setattr(driver, "verify", lambda environment: True)
        monkeypatch.setattr(driver, "_await_unit_unloaded", lambda previous: None)
        monkeypatch.setattr(driver, "display_incarnation", lambda: "weston-7")
        monkeypatch.setattr(driver, "_observe", lambda *args: observed[0] if observed else None)
        monkeypatch.setattr(linux, "player_device_grants", lambda sysfs: ())
        monkeypatch.setattr(linux, "boottime_ms", lambda: 424_242)
        commands = []
        monkeypatch.setattr(linux.subprocess, "run", lambda command, **kwargs: (
            commands.append(command), observed.append(expected)))
        assert driver.start(expected.environment, expected.operation_id) == expected
        assert driver.launched() == Launch(1, "weston-7", 424_242)
        (command,) = commands
        environment = next(value for value in command if value.startswith("Environment="))
        assert "PYTHONFAULTHANDLER=1" in environment.split()
        assert "Restart=no" in command
    finally:
        driver.store.close()
