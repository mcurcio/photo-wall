"""Linux boundary policies exercised portably; these do not claim systemd or pixels."""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
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
    stage_archive,
    verify_root,
)
from appliance.host.host import HostCore, RebootRequest
from appliance.host.host_storage import FileRebootJournal
from appliance.kernel.boot_store import BootStore
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_commands import reboot_digest
from contracts.node_protocol import NodeProducerV2
from scripts.build_app_environment import materialize
from scripts.build_node_base_deb import stage_tree

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


def fixture_archive(directory: Path, extra=None):
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
    reference = AppEnvironmentRefV2("0" * 64, 1, "a" * 64, "player", "1.0", "amd64",
                                    hashlib.sha256(lock).hexdigest(), hashlib.sha256(snapshot).hexdigest(),
                                    "/entry", "base-v2", "graphics-v2", "plugins-v2")
    fields = asdict(reference)
    fields.pop("environment_sha256")
    fields.pop("size_bytes")
    (source / "environment.json").write_bytes(canonical_bytes({"schema": 2, "format": FORMAT, "reference": fields, "files": inventory(root), "capacity": capacity(inventory(root))}))
    (source / "dependency-lock.json").write_bytes(lock)
    (source / "sources.json").write_bytes(snapshot)
    archive = directory / "sealed.tar"
    with tarfile.open(archive, "w", format=tarfile.GNU_FORMAT) as stream:
        for path in sorted(source.rglob("*")):
            stream.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)
        if extra is not None:
            for member in extra if isinstance(extra, list) else [extra]:
                stream.addfile(member)
    return archive, replace(reference, environment_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), size_bytes=archive.stat().st_size)


def test_environment_exact_staging_and_file_corruption_refusal(tmp_path):
    archive, reference = fixture_archive(tmp_path)
    roots = tmp_path / "roots"
    roots.mkdir()
    abi = dict(base_abi="base-v2", graphics_abi="graphics-v2", plugin_abi="plugins-v2", owner_uid=os.getuid())
    target = stage_archive(archive, roots, reference, **abi)
    assert verify_root(target, reference, **abi) == target / "rootfs"
    (target / "rootfs/entry").write_text("corrupt")
    with pytest.raises(ValueError, match="root_digest"):
        stage_archive(archive, roots, reference, **abi)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE])
def test_environment_rejects_links_and_special_members_before_publish(tmp_path, kind):
    extra = tarfile.TarInfo("rootfs/escape")
    extra.type, extra.linkname = kind, "../../../etc/passwd"
    archive, reference = fixture_archive(tmp_path, extra)
    roots = tmp_path / "roots"
    roots.mkdir()
    with pytest.raises(ValueError, match="archive_(member|link)|link_escape"):
        stage_archive(archive, roots, reference, base_abi="base-v2", graphics_abi="graphics-v2", plugin_abi="plugins-v2", owner_uid=os.getuid())
    assert list(roots.iterdir()) == []


def test_materializer_preserves_absolute_loader_link_bytes_and_rejects_cycle(tmp_path):
    archive = tmp_path / "root.tar"
    with tarfile.open(archive, "w") as stream:
        for name in ("usr", "usr/lib", "lib64"):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            stream.addfile(member)
        data = tarfile.TarInfo("usr/lib/loader")
        data.size, data.mode = 3, 0o755
        stream.addfile(data, io.BytesIO(b"ELF"))
        link = tarfile.TarInfo("lib64/ld.so")
        link.type, link.linkname = tarfile.SYMTYPE, "/usr/lib/loader"
        stream.addfile(link)
    materialize(archive, tmp_path / "root")
    assert (tmp_path / "root/lib64/ld.so").is_symlink()
    assert os.readlink(tmp_path / "root/lib64/ld.so") == "/usr/lib/loader"
    assert (tmp_path / "root/usr/lib/loader").read_bytes() == b"ELF"
    with tarfile.open(archive, "w") as stream:
        link = tarfile.TarInfo("cycle")
        link.type, link.linkname = tarfile.SYMTYPE, "cycle"
        stream.addfile(link)
    with pytest.raises(ValueError, match="link_cycle"):
        materialize(archive, tmp_path / "bad")


def test_node_base_packaging_has_separate_host_closure_and_opt_in(tmp_path):
    source = REPO
    stage_tree(source, tmp_path / "package")
    root = tmp_path / "package"
    assert not (root / "usr/lib/photo-wall-host-core/appliance/apps/broker.py").exists()
    assert not (root / "usr/lib/photo-wall-host-core/player").exists()
    assert (root / "usr/lib/photo-wall-host-core/appliance/host/host_runner.py").exists()
    control = (root / "DEBIAN/control").read_text()
    assert "login" in control and "libpam-systemd" in control
    assert (root / "lib/systemd/system/photo-wall-provision.service.d/node-cohort.conf").read_text().endswith("ConditionKernelCommandLine=!photowall.node=v2\n")


def test_manager_package_has_executable_without_effect_closure(tmp_path):
    from scripts.build_node_manager_deb import stage_tree as stage_manager
    stage_manager(REPO, tmp_path / "package")
    root = tmp_path / "package/usr/lib/photo-wall-node-manager"
    assert (root / "entry").stat().st_mode & 0o111
    assert (root / "appliance/node/manager_runner.py").exists()
    for path in ("appliance/host/host.py", "appliance/host/host_linux.py", "appliance/apps/broker.py",
                 "appliance/apps/process_linux.py"):
        assert not (root / path).exists()


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


def test_stream_mutation_after_path_stat_never_publishes(tmp_path, monkeypatch):
    from appliance.apps import environment
    archive, reference = fixture_archive(tmp_path)
    roots = tmp_path / "roots"
    roots.mkdir()
    original = environment.os.open
    def mutate_before_open(path, *args, **kwargs):
        if path == archive:
            data = bytearray(archive.read_bytes())
            data[-1] = 1
            archive.write_bytes(data)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(environment.os, "open", mutate_before_open)
    with pytest.raises(ValueError, match="digest_mismatch"):
        stage_archive(archive, roots, reference, base_abi="base-v2", graphics_abi="graphics-v2", plugin_abi="plugins-v2", owner_uid=os.getuid())
    assert not list(roots.iterdir())


def test_link_parent_even_when_declared_after_child_never_publishes(tmp_path):
    directory = tarfile.TarInfo("rootfs/alias/child")
    directory.type = tarfile.DIRTYPE
    link = tarfile.TarInfo("rootfs/alias")
    link.type, link.linkname = tarfile.SYMTYPE, "/usr"
    archive, reference = fixture_archive(tmp_path, [directory, link])
    roots = tmp_path / "roots"
    roots.mkdir()
    with pytest.raises(ValueError, match="link_parent"):
        stage_archive(archive, roots, reference, base_abi="base-v2", graphics_abi="graphics-v2", plugin_abi="plugins-v2", owner_uid=os.getuid())
    assert not list(roots.iterdir())


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
    from appliance.kernel.capacity import GIB, OVERHEAD, admit_cold, cold_peak
    _, reference = fixture_archive(tmp_path)
    reference = replace(reference, size_bytes=GIB)
    assert cold_peak([reference, reference]) == 2 * GIB + OVERHEAD
    assert admit_cold([reference, reference], total=8 * GIB, available=7 * GIB, free=4 * GIB) == 2 * GIB + OVERHEAD
    # A 4 GB board is a class of its own (store 2560 MiB); below it, the class refuses.
    assert admit_cold([reference], total=4 * GIB, available=3 * GIB, free=4 * GIB) == 2 * GIB + OVERHEAD
    with pytest.raises(ValueError, match="node_memory_class"):
        admit_cold([reference], total=3 * GIB, available=3 * GIB, free=4 * GIB)
    with pytest.raises(ValueError, match="capacity"):
        admit_cold([reference], total=8 * GIB, available=7 * GIB, free=2 * GIB)


def test_online_capacity_counts_only_incremental_available_memory():
    from appliance.kernel.capacity import GIB, OVERHEAD, admit_preparation
    size = 984207360
    # Approximate actual 8GiB class: old root + manager roots consume storage;
    # app/kernel resident memory already reduces MemAvailable independently.
    used = 960908679 + 300 * 1024**2
    assert admit_preparation(size, total=8 * GIB, available=3 * GIB,
        free=4 * GIB-used, used=used) == size * 2 + OVERHEAD
    with pytest.raises(ValueError, match="capacity"):
        admit_preparation(size, total=8 * GIB, available=2 * GIB,
            free=4 * GIB-used, used=used)
    with pytest.raises(ValueError, match="capacity"):
        admit_preparation(size, total=8 * GIB, available=6 * GIB,
            free=4 * GIB, used=3 * GIB)


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


def test_node_base_abi_binds_exact_dependency_declaration(tmp_path, monkeypatch):
    import json

    from scripts import build_node_base_deb

    source = REPO
    original = tmp_path / 'original'
    changed = tmp_path / 'changed'
    original_version = build_node_base_deb.stage_tree(source, original)
    dependencies = build_node_base_deb.packages('node-base')
    monkeypatch.setattr(build_node_base_deb, 'packages', lambda consumer: (*dependencies, 'fixture-dependency'))
    changed_version = build_node_base_deb.stage_tree(source, changed)
    marker = 'usr/lib/photo-wall-node-base/abi.json'
    assert json.loads((original / marker).read_text()) != json.loads((changed / marker).read_text())
    assert original_version != changed_version
    assert 'fixture-dependency' in (changed / 'DEBIAN/control').read_text()


@pytest.mark.parametrize("mask", [0o022, 0o077])
def test_environment_directory_policy_ignores_umask_and_preserves_private_wrapper(tmp_path, mask):
    archive, reference = fixture_archive(tmp_path)
    source = tmp_path / "source"
    root = source / "rootfs"
    host_target = tmp_path / "host-directory"
    host_target.mkdir(mode=0o700)
    (root / "link-only").mkdir()
    (root / "link-only/absolute").symlink_to(host_target)
    (root / "private-file").write_bytes(b"private")
    (root / "private-file").chmod(0o600)
    metadata = json.loads((source / "environment.json").read_text())
    metadata.update(files=inventory(root), capacity=capacity(inventory(root)))
    (source / "environment.json").write_bytes(canonical_bytes(metadata))
    # Omit all explicit directories, including link-only and implicit parents.
    implicit = tmp_path / "implicit.tar"
    with tarfile.open(implicit, "w", format=tarfile.GNU_FORMAT) as target:
        for path in sorted(source.rglob("*")):
            if path.is_symlink() or not path.is_dir():
                target.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)
    reference = replace(reference, environment_sha256=hashlib.sha256(implicit.read_bytes()).hexdigest(), size_bytes=implicit.stat().st_size)
    roots = tmp_path / "roots"
    roots.mkdir(mode=0o700)
    abi = dict(base_abi="base-v2", graphics_abi="graphics-v2", plugin_abi="plugins-v2", owner_uid=os.getuid())
    previous = os.umask(mask)
    try:
        target = stage_archive(implicit, roots, reference, **abi)
    finally:
        os.umask(previous)
    assert target.stat().st_mode & 0o777 == 0o700
    assert roots.stat().st_mode & 0o777 == 0o700
    assert host_target.stat().st_mode & 0o777 == 0o700
    assert os.readlink(target / "rootfs/link-only/absolute") == str(host_target)
    assert (target / "rootfs/private-file").stat().st_mode & 0o777 == 0o600
    assert (target / "rootfs/link-only").stat().st_mode & 0o777 == 0o755
    for path in [target / "rootfs", target / "rootfs/usr", target / "rootfs/usr/bin"]:
        assert path.stat().st_mode & 0o777 == 0o755
    (target / "rootfs/usr").chmod(0o700)
    with pytest.raises(ValueError, match="environment_directory_mode"):
        stage_archive(implicit, roots, reference, **abi)
    assert (target / "rootfs/usr").stat().st_mode & 0o777 == 0o700
    (target / "rootfs/usr").chmod(0o777)
    with pytest.raises(ValueError, match="environment_directory_mode"):
        verify_root(target, reference, **abi)


@pytest.mark.parametrize("mask", [0o022, 0o077])
def test_materializer_implicit_directory_modes_and_no_follow_links(tmp_path, mask):
    host_target = tmp_path / "host-directory"
    host_target.mkdir(mode=0o700)
    archive = tmp_path / "root.tar"
    with tarfile.open(archive, "w") as stream:
        data = tarfile.TarInfo("private/implicit/file")
        data.size, data.mode = 3, 0o600
        stream.addfile(data, io.BytesIO(b"abc"))
        link = tarfile.TarInfo("link-only/absolute")
        link.type, link.linkname = tarfile.SYMTYPE, str(host_target)
        stream.addfile(link)
    root = tmp_path / "root"
    previous = os.umask(mask)
    try:
        materialize(archive, root)
    finally:
        os.umask(previous)
    assert host_target.stat().st_mode & 0o777 == 0o700
    assert os.readlink(root / "link-only/absolute") == str(host_target)
    assert (root / "private/implicit/file").stat().st_mode & 0o777 == 0o600
    assert (root / "etc/photo-wall/public.json").stat().st_mode & 0o777 == 0o444
    for path in [root, *(p for p in root.rglob("*") if not p.is_symlink() and p.is_dir())]:
        assert path.stat().st_mode & 0o777 == 0o755


def test_environment_rejects_rootfs_directory_with_wrong_owner(tmp_path, monkeypatch):
    archive, reference = fixture_archive(tmp_path)
    roots = tmp_path / "roots"
    roots.mkdir()
    abi = dict(base_abi="base-v2", graphics_abi="graphics-v2", plugin_abi="plugins-v2", owner_uid=os.getuid())
    target = stage_archive(archive, roots, reference, **abi)
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

    _, reference = fixture_archive(tmp_path)
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
    props = app_unit_properties(Path("/sealed/rootfs"))
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
