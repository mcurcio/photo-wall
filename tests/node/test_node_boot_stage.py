"""Base boot stage records and HostCore's boot report (docs/node-4gb-memory-design.md §4.3 T3), node side:
`fault_token`, best-effort `run_stage`, `read_boot_report`, the bootstrap stage wiring, the
sampler's `failed_units` and `memory_rows`, the facts sender comparing whole documents, and
the HostCore closure. No database, no PID1."""
from __future__ import annotations

import errno
import http.client
import json
import os
import subprocess
import sys
from functools import partial
from types import SimpleNamespace
from uuid import uuid4

import pytest
from support.repo import REPO
from systemd_environment import read_environment_file

from appliance.boot import node_bootstrap as bootstrap
from appliance.boot.bus_environment import BUS_ENVIRONMENT, bus_environment
from appliance.host.host_linux import LinuxHostSampler
from appliance.kernel import boot_stage
from appliance.kernel.boot_stage import fault_token, read_boot_report, run_stage, write_stage
from appliance.kernel.capacity import StorageShort
from contracts.node_host_facts import BootReportV2, BootStageV2, parse_host_facts


def _record(directory, stage):
    return json.loads((directory / f"{stage}.json").read_text())


# fault_token.
@pytest.mark.parametrize(("error", "expected"), [
    (StorageShort(10, 5, "node_memory_class"), "node_memory_class"),
    (StorageShort(10, 5), "node_storage_capacity"),
    (ValueError("node_measured_base_abi_mismatch"), "node_measured_base_abi_mismatch"),
    (ValueError("not a token"), "unexpected:ValueError"),
    (ValueError("x" * 65), "unexpected:ValueError"),
    (ValueError("a", "b"), "unexpected:ValueError"),
    (OSError(errno.ENOSPC, "No space left on device"), "os:ENOSPC"),
    (FileNotFoundError(errno.ENOENT, "missing"), "os:ENOENT"),
    (http.client.RemoteDisconnected("Remote end closed connection"), "os:RemoteDisconnected"),
    (OSError("no errno"), "os:OSError"),
    (OSError(99999, "unknown errno"), "os:OSError"),
    (subprocess.CalledProcessError(32, ["mount"]), "unexpected:CalledProcessError"),
    (RuntimeError("boom"), "unexpected:RuntimeError"),
    (type("E" * 80, (Exception,), {})(), "unexpected:" + "E" * 52),
    (type("Ä", (OSError,), {})(), "unexpected:error"),
])
def test_fault_token_maps_every_error_to_a_valid_token(error, expected):
    assert fault_token(error) == expected
    BootStageV2("prepare", "failed", fault_token(error), None, None)  # always a valid fault


# run_stage: each state.
def test_a_stage_records_running_at_entry_then_done(tmp_path):
    seen = []
    run_stage("handoff", lambda: seen.append(_record(tmp_path, "handoff")), directory=tmp_path)
    assert seen == [{"stage": "handoff", "state": "running", "fault": None,
                     "required_bytes": None, "room_bytes": None}]
    assert _record(tmp_path, "handoff")["state"] == "done"
    assert (tmp_path / "handoff.json").stat().st_mode & 0o777 == 0o644


def test_a_storage_short_is_refused_with_its_numbers_and_re_raised(tmp_path):
    short = StorageShort(3758096384, 2147483648, "node_memory_class")

    def action():
        raise short

    with pytest.raises(StorageShort) as raised:
        run_stage("storage", action, directory=tmp_path)
    assert raised.value is short
    assert _record(tmp_path, "storage") == {"stage": "storage", "state": "refused",
                                            "fault": "node_memory_class",
                                            "required_bytes": 3758096384, "room_bytes": 2147483648}


def test_a_storage_short_without_usable_numbers_is_failed_not_unrecorded(tmp_path):
    def action():
        raise StorageShort(-1, 5)

    with pytest.raises(StorageShort):
        run_stage("prepare", action, directory=tmp_path)
    assert _record(tmp_path, "prepare")["state"] == "failed"


@pytest.mark.parametrize(("error", "fault"), [
    (ValueError("node_measured_base_abi_mismatch"), "node_measured_base_abi_mismatch"),
    (OSError(errno.ENOSPC, "full"), "os:ENOSPC"),
    (KeyboardInterrupt(), "unexpected:KeyboardInterrupt"),
])
def test_any_other_error_is_failed_with_its_fault_and_re_raised(tmp_path, error, fault):
    def action():
        raise error

    with pytest.raises(type(error)):
        run_stage("prepare", action, directory=tmp_path)
    assert _record(tmp_path, "prepare") == {"stage": "prepare", "state": "failed", "fault": fault,
                                            "required_bytes": None, "room_bytes": None}


def test_write_stage_sets_0644_whatever_the_umask(tmp_path):
    previous = os.umask(0o077)
    try:
        write_stage(BootStageV2("storage", "done", None, None, None), directory=tmp_path / "new")
    finally:
        os.umask(previous)
    assert (tmp_path / "new/storage.json").stat().st_mode & 0o777 == 0o644


# run_stage: best effort.
def test_an_unwritable_record_never_changes_a_successful_outcome(tmp_path, caplog):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    ran = []
    run_stage("handoff", lambda: ran.append(1), directory=blocked)
    assert ran == [1]
    # Logged once, although both the entry and the exit write failed.
    assert [record.getMessage() for record in caplog.records] == ["boot stage record not written: handoff os:ENOTDIR"]


@pytest.mark.parametrize("error", [ValueError("node_boot_handoff_stale"), StorageShort(2, 1)])
def test_an_unwritable_record_never_changes_a_failing_outcome(tmp_path, monkeypatch, error):
    def unwritable(*args, **kwargs):
        raise PermissionError(errno.EACCES, "read-only file system")

    monkeypatch.setattr(boot_stage, "write_stage", unwritable)

    def action():
        raise error

    with pytest.raises(type(error)) as raised:
        run_stage("storage", action, directory=tmp_path)
    assert raised.value is error


# read_boot_report.
def _stage_file(directory, name, **fields):
    value = {"stage": name, "state": "done", "fault": None, "required_bytes": None,
             "room_bytes": None, **fields}
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(value))


def test_the_report_holds_the_present_valid_records_in_stage_order(tmp_path):
    _stage_file(tmp_path, "prepare", state="failed", fault="os:ENOSPC")
    _stage_file(tmp_path, "handoff")
    (tmp_path / "storage.json").write_text("{not json")
    report = read_boot_report(directory=tmp_path,
                              units=lambda: (("photo-wall-node-prepare.service",), 2))
    assert report == BootReportV2((BootStageV2("handoff", "done", None, None, None),
                                   BootStageV2("prepare", "failed", "os:ENOSPC", None, None)),
                                  ("photo-wall-node-prepare.service",), 2)


@pytest.mark.parametrize("content", [
    {"stage": "storage", "state": "done", "fault": None, "required_bytes": None, "room_bytes": None},  # wrong file
    {"stage": "handoff", "state": "refused", "fault": "x", "required_bytes": None, "room_bytes": None},
    {"stage": "handoff", "state": "done", "fault": None, "required_bytes": None},
    {"stage": "handoff", "state": "done", "fault": None, "required_bytes": None, "room_bytes": None, "x": 1},
    [],
])
def test_an_invalid_record_is_omitted(tmp_path, content):
    (tmp_path / "handoff.json").write_text(json.dumps(content))
    assert read_boot_report(directory=tmp_path, units=lambda: ((), 0)) == BootReportV2((), (), 0)


def test_unreadable_units_keep_the_records_and_none_only_when_both_are_unreadable(tmp_path):
    # Errata E-T3-2: units that were not read are None in the report ("not read"), never the
    # empty list that means "nothing failed".
    def broken():
        raise RuntimeError("no systemctl")

    assert read_boot_report(directory=tmp_path / "missing", units=broken) is None
    assert read_boot_report(directory=tmp_path / "missing", units=lambda: None) is None
    assert read_boot_report(directory=tmp_path, units=lambda: (("bad name",), 0)) is None
    assert read_boot_report(directory=tmp_path, units=lambda: ((), 0)) == BootReportV2((), (), 0)
    _stage_file(tmp_path, "storage", state="running")
    running = (BootStageV2("storage", "running", None, None, None),)
    for units in (broken, lambda: None):
        assert read_boot_report(directory=tmp_path, units=units) == BootReportV2(running, None, None)


def test_a_record_that_is_not_a_bounded_regular_file_is_not_readable(tmp_path):
    # A symlink is never followed, a FIFO never blocks the reader, and an oversized file is
    # never read past MAX_STAGE_BYTES + 1.
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"stage": "handoff", "state": "done", "fault": None,
                                  "required_bytes": None, "room_bytes": None}))
    (tmp_path / "handoff.json").symlink_to(target)
    os.mkfifo(tmp_path / "storage.json")
    padded = {"stage": "prepare", "state": "done", "fault": None, "required_bytes": None,
              "room_bytes": None}
    raw = json.dumps(padded).encode()
    (tmp_path / "prepare.json").write_bytes(raw + b" " * (boot_stage.MAX_STAGE_BYTES + 1 - len(raw)))
    assert read_boot_report(directory=tmp_path, units=lambda: ((), 0)) == BootReportV2((), (), 0)
    # The same prepare record within the bound is read: the size, not the content, refused it.
    (tmp_path / "prepare.json").write_bytes(raw + b" " * (boot_stage.MAX_STAGE_BYTES - len(raw)))
    assert read_boot_report(directory=tmp_path, units=lambda: ((), 0)) == BootReportV2(
        (BootStageV2("prepare", "done", None, None, None),), (), 0)


# Bootstrap wiring.
@pytest.mark.parametrize("mode", ["handoff", "storage", "prepare"])
def test_each_bootstrap_mode_runs_as_its_stage(tmp_path, monkeypatch, mode):
    target = {"handoff": "materialize_handoff", "storage": "mount_storage", "prepare": "prepare_roots"}[mode]
    short = StorageShort(3758096384, 2147483648, "node_memory_class")

    def refuse():
        raise short

    monkeypatch.setattr(bootstrap, target, refuse)
    monkeypatch.setattr(bootstrap, "run_stage", partial(run_stage, directory=tmp_path))
    monkeypatch.setattr(sys, "argv", ["bootstrap", mode])
    with pytest.raises(StorageShort):
        bootstrap.main()
    assert _record(tmp_path, mode)["state"] == "refused"
    assert [path.name for path in tmp_path.iterdir()] == [f"{mode}.json"]


@pytest.mark.parametrize("failure", ["marker", "base_abi"])
def test_host_configuration_is_written_before_the_marker_and_abi_checks(tmp_path, monkeypatch, failure):
    from node.boot.test_node_boot_linux import ROOT, offer
    selected = offer()
    monkeypatch.setattr(bootstrap, "read_node_handoff", lambda _: (ROOT, selected))
    monkeypatch.setattr(bootstrap, "boot_id", lambda: selected.kernel_boot_id)

    def marker(path, keys):
        if failure == "marker":
            raise ValueError("node_base_marker_ownership")
        return {"base_abi": "another"}

    monkeypatch.setattr(bootstrap, "_marker", marker)
    with pytest.raises(ValueError, match="node_base_marker_ownership|node_measured_base_abi_mismatch"):
        bootstrap.materialize_handoff(root=tmp_path)
    host = json.loads((tmp_path / "run/photo-wall-node/host.json").read_bytes())
    assert host["offer_id"] == str(selected.offer_id)
    # The bus's environment is written before the checks too: it needs only the origin and serial.
    assert read_environment_file(tmp_path / BUS_ENVIRONMENT) == bus_environment(ROOT, selected.serial)


def test_a_stale_handoff_writes_no_host_configuration(tmp_path, monkeypatch):
    from node.boot.test_node_boot_linux import ROOT, offer
    selected = offer()
    monkeypatch.setattr(bootstrap, "read_node_handoff", lambda _: (ROOT, selected))
    monkeypatch.setattr(bootstrap, "boot_id", lambda: uuid4())
    with pytest.raises(ValueError, match="node_boot_handoff_stale"):
        bootstrap.materialize_handoff(root=tmp_path)
    assert not (tmp_path / "run/photo-wall-node").exists()
    assert not (tmp_path / BUS_ENVIRONMENT).exists()


@pytest.mark.parametrize("unit", ["photo-wall-node-handoff.service", "photo-wall-node-prepare.service"])
def test_the_sandboxed_stage_units_may_write_the_record_directory(unit):
    text = (REPO / "appliance/systemd" / unit).read_text()
    writable = next(line for line in text.splitlines() if line.startswith("ReadWritePaths="))
    assert str(boot_stage.DIRECTORY) in writable.split("=", 1)[1].split()


def test_the_storage_unit_is_not_file_system_sandboxed():
    # The storage stage mounts the store, so it has no ProtectSystem; its record needs no grant.
    text = (REPO / "appliance/systemd/photo-wall-node-storage.service").read_text()
    assert "ProtectSystem" not in text and "ReadOnlyPaths" not in text


# failed_units.
def _systemctl(monkeypatch, stdout=None, error=None):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if error is not None:
            raise error
        return SimpleNamespace(stdout=stdout)

    monkeypatch.setattr("appliance.host.host_linux.subprocess.run", run)
    return calls


def _line(unit):
    return f"{unit} loaded failed failed Some description here\n"


def test_failed_units_is_one_bounded_systemctl_call(monkeypatch):
    calls = _systemctl(monkeypatch, "")
    assert LinuxHostSampler().failed_units() == ((), 0)
    [(command, kwargs)] = calls
    assert command == ["/usr/bin/systemctl", "list-units", "--state=failed", "--plain", "--no-legend",
                       "photo-wall-*"]
    assert kwargs["timeout"] == 0.25 and kwargs["check"] is True


def test_failed_units_are_sorted_unique_and_include_the_stage_units(monkeypatch):
    # The stage units stay in the facts (a killed stage leaves its record `running`; a record
    # write may fail); the console subtracts them (§4.3 T4).
    stdout = "".join(_line(unit) for unit in ("photo-wall-node-prepare.service", "photo-wall-app-broker.service",
                                              "photo-wall-node-prepare.service"))
    _systemctl(monkeypatch, stdout + "\n")
    assert LinuxHostSampler().failed_units() == (
        ("photo-wall-app-broker.service", "photo-wall-node-prepare.service"), 0)


def test_failed_units_cap_at_four_and_count_the_rest_and_refused_names(monkeypatch):
    units = [f"photo-wall-unit-{index}.service" for index in range(6)]
    stdout = "".join(_line(unit) for unit in reversed(units)) + _line("photo-wall-" + "x" * 90 + ".service")
    _systemctl(monkeypatch, stdout + _line("other.service"))
    assert LinuxHostSampler().failed_units() == (tuple(units[:4]), 4)


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("systemctl", 0.25), OSError("missing"),
                                   subprocess.CalledProcessError(1, "systemctl")], ids=str)
def test_failed_units_on_error_raises_never_reads_as_none_failed(monkeypatch, error):
    # Errata E-T3-2: an unreadable list is not "nothing failed" (host_linux.py: a value that
    # cannot be read is never sent as zero).
    _systemctl(monkeypatch, error=error)
    with pytest.raises(ValueError, match="^failed_units_unreadable$"):
        LinuxHostSampler().failed_units()


def test_failed_units_over_4096_bytes_raises(monkeypatch):
    _systemctl(monkeypatch, _line("photo-wall-a.service") * 100)
    with pytest.raises(ValueError, match="^failed_units_unreadable$"):
        LinuxHostSampler().failed_units()


def test_the_runner_reuses_the_last_failed_units_read_and_sends_none_before_any(tmp_path, monkeypatch):
    from node.host.test_node_host_facts import INTERVAL, VALUES, _run, _runner
    clock = [100.0]
    runner, sent = _runner(monkeypatch, clock, dict(VALUES), {})
    runner.boot_stage_directory = tmp_path
    run_stage("prepare", lambda: None, directory=tmp_path)
    readings = [ValueError("failed_units_unreadable")]

    def failed_units():
        if isinstance(readings[0], Exception):
            raise readings[0]
        return readings[0]

    runner.sampler.failed_units = failed_units
    _run(runner, clock, 100 + INTERVAL)
    # Never read on this process: None, not the empty list.
    assert parse_host_facts(sent[-1][1]).boot.failed_units is None
    readings[0] = (("photo-wall-app-broker.service",), 0)
    _run(runner, clock, clock[0] + 2 * INTERVAL)
    assert parse_host_facts(sent[-1][1]).boot.failed_units == ("photo-wall-app-broker.service",)
    # A slow systemctl reuses the last reading: the failed unit is not cleared, nothing is resent.
    count = len(sent)
    readings[0] = ValueError("failed_units_unreadable")
    _run(runner, clock, clock[0] + 3 * INTERVAL)
    assert len(sent) == count


# memory_rows.
SLICES = {"hostcore": "photowallhostcore", "base": "photowallbase", "preparation": "photowallpreparation",
          "app": "photowallapp"}
DISPLAY = "photowallbase.slice/photo-wall-display.service"
BUS = "photowallbus.slice"


def _memory(tmp_path, controllers="cpu io memory pids\n", cma=True):
    proc, sys_root = tmp_path / "proc", tmp_path / "sys"
    proc.mkdir()
    (proc / "meminfo").write_text("MemTotal: 4000000 kB\n" + ("CmaTotal: 524288 kB\nCmaFree: 100 kB\n" if cma else ""))
    cgroup = sys_root / "fs/cgroup"
    cgroup.mkdir(parents=True)
    if controllers is not None:
        (cgroup / "cgroup.controllers").write_text(controllers)
    for index, (name, directory) in enumerate(SLICES.items()):
        (cgroup / f"{directory}.slice").mkdir()
        (cgroup / f"{directory}.slice/memory.peak").write_text(f"{1000 + index}\n")
        (cgroup / f"{directory}.slice/memory.events").write_text(f"low 0\nhigh 0\nmax 0\noom 1\noom_kill {index}\n")
    (cgroup / DISPLAY).mkdir()
    (cgroup / DISPLAY / "memory.peak").write_text("1004\n")
    (cgroup / DISPLAY / "memory.events").write_text("oom_kill 9\n")  # no row: the base slice counts it
    (cgroup / BUS).mkdir(parents=True)
    (cgroup / BUS / "memory.peak").write_text("1005\n")
    (cgroup / BUS / "memory.events").write_text("oom 4\noom_kill 4\n")
    # The unit's own cgroup, recreated by PID1 at the restart after each kill: never read.
    (cgroup / BUS / "photo-wall-bus.service").mkdir()
    (cgroup / BUS / "photo-wall-bus.service/memory.peak").write_text("7\n")
    (cgroup / BUS / "photo-wall-bus.service/memory.events").write_text("oom 0\noom_kill 0\n")
    return LinuxHostSampler(proc, tmp_path, sys_root), cgroup


def test_memory_rows_with_the_controller(tmp_path):
    sampler, _ = _memory(tmp_path)
    assert sampler.memory_rows() == (
        ("memcg_present", 1, "boolean", "cgroup"),
        ("cma_total", 524288 * 1024, "bytes"), ("cma_free", 100 * 1024, "bytes"),
        ("memory_peak:hostcore", 1000, "bytes", "cgroup"),
        ("memory_peak:base", 1001, "bytes", "cgroup"), ("oom_kill:base", 1, "count", "cgroup"),
        ("memory_peak:preparation", 1002, "bytes", "cgroup"), ("oom_kill:preparation", 2, "count", "cgroup"),
        ("memory_peak:app", 1003, "bytes", "cgroup"), ("oom_kill:app", 3, "count", "cgroup"),
        ("memory_peak:display", 1004, "bytes", "cgroup"),
        ("memory_peak:bus", 1005, "bytes", "cgroup"), ("oom_kill:bus", 4, "count", "cgroup"))


def test_the_display_peak_is_read_from_the_display_units_own_slice():
    # Weston's cgroup is <Slice=>/<unit> (photo-wall-display.service), so a slice move is caught.
    from test_netboot_liveness import _parse_unit
    unit = _parse_unit((REPO / "appliance/systemd/photo-wall-display.service").read_text())
    assert DISPLAY == f"{unit['Service']['Slice'][-1]}/photo-wall-display.service"


def test_the_bus_is_read_from_its_own_persistent_slice():
    # photo-wall-bus.service's Slice= is the bus slice, whose counters outlive the unit's restarts
    # (erratum E-E3C-S5-3), so a move back to system.slice is caught.
    from test_netboot_liveness import _parse_unit
    unit = _parse_unit((REPO / "appliance/systemd/photo-wall-bus.service").read_text())
    assert unit["Service"]["Slice"] == [BUS]


@pytest.mark.parametrize("controllers", ["cpu io pids\n", None])
def test_memory_rows_without_the_controller_omit_the_peaks(tmp_path, controllers):
    sampler, _ = _memory(tmp_path, controllers=controllers, cma=False)
    rows = sampler.memory_rows()
    assert rows[0] == ("memcg_present", 0, "boolean", "cgroup")
    assert [row[0] for row in rows[1:]] == ["oom_kill:base", "oom_kill:preparation", "oom_kill:app",
                                            "oom_kill:bus"]


def test_memory_rows_omit_each_unreadable_or_malformed_file(tmp_path):
    sampler, cgroup = _memory(tmp_path)
    (cgroup / "photowallapp.slice/memory.peak").unlink()
    (cgroup / DISPLAY / "memory.peak").write_text("")
    (cgroup / "photowallbase.slice/memory.events").write_text("oom 1\n")
    (cgroup / "photowallpreparation.slice/memory.peak").write_text("max\n")
    (tmp_path / "proc/meminfo").unlink()
    names = [row[0] for row in sampler.memory_rows()]
    assert names == ["memcg_present", "memory_peak:hostcore", "memory_peak:base",
                     "oom_kill:preparation", "oom_kill:app", "memory_peak:bus", "oom_kill:bus"]


def test_supervision_carries_no_per_unit_rows(monkeypatch):
    monkeypatch.setattr("appliance.host.host_linux.read_supervisor_status",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("absent")))
    monkeypatch.setattr("appliance.host.host_linux.boot_id", lambda: uuid4())
    monkeypatch.setattr("appliance.host.host_linux.boottime_ms", lambda: 1)
    _systemctl(monkeypatch, error=AssertionError("supervision runs no systemctl"))
    assert LinuxHostSampler().supervision() == (("manager_summary_known", 0, "boolean", "base_supervisor"),)


# The facts sender compares whole documents, boot included.
def test_a_boot_change_sends_a_new_document_and_an_unchanged_boot_sends_nothing(tmp_path, monkeypatch):
    from node.host.test_node_host_facts import INTERVAL, VALUES, _run, _runner
    clock, facts = [100.0], dict(VALUES)
    runner, sent = _runner(monkeypatch, clock, facts, {})
    units = [((), 0)]
    runner.sampler.failed_units = lambda: units[0]
    runner.boot_stage_directory = tmp_path
    run_stage("storage", lambda: None, directory=tmp_path)
    _run(runner, clock, 100 + 4 * INTERVAL)
    assert len(sent) == 1
    first = parse_host_facts(sent[0][1])
    assert first.boot == BootReportV2((BootStageV2("storage", "done", None, None, None),), (), 0)
    write_stage(BootStageV2("prepare", "running", None, None, None), directory=tmp_path)
    _run(runner, clock, clock[0] + 2 * INTERVAL)
    units[0] = (("photo-wall-app-broker.service",), 0)
    _run(runner, clock, clock[0] + 2 * INTERVAL)
    assert len(sent) == 3
    second, third = (parse_host_facts(body) for _, body in sent[1:])
    assert [stage.stage for stage in second.boot.stages] == ["storage", "prepare"]
    assert third.boot.failed_units == ("photo-wall-app-broker.service",)
    assert first.sequence < second.sequence < third.sequence
    count = len(sent)
    _run(runner, clock, clock[0] + 4 * INTERVAL)
    assert len(sent) == count


def test_observation_rows_pass_through_valid_metrics(monkeypatch):
    from test_node_host_cadence import _runner
    clock = [100.0]
    runner, posts, session_for = _runner(monkeypatch, clock)
    runner.sampler.memory_rows = lambda: (("memcg_present", 1, "boolean", "cgroup"),
                                          ("memcg_present", 0, "boolean", "cgroup"),
                                          ("broker_failed", 0, "boolean", "pid1"))
    session_for(uuid4())
    runner.tick()
    [(_, observation)] = posts
    rows = {(metric.name, metric.value) for metric in observation.metrics}
    assert ("memcg_present", 1) in rows and ("memcg_present", 0) not in rows
    assert observation.metrics[-1].name == "metrics_dropped" and observation.metrics[-1].value == 2


# The HostCore closure and the record directory's package line.
def test_host_core_closure_carries_the_boot_report_reader_and_no_forbidden_module(tmp_path):
    from scripts.build_node_base_deb import POLICIES, stage_tree
    from scripts.module_closure import closure_for
    modules = closure_for(POLICIES["host-core"], repo=REPO).modules
    assert "appliance.kernel.boot_stage" in modules
    forbidden = (*POLICIES["host-core"].forbidden, "appliance.apps.process_linux",
                 "appliance.apps.environment", "appliance.apps.lifecycle_storage", "appliance.node.preparer")
    assert not [module for module in modules
                if any(module == name or module.startswith(name + ".") for name in forbidden)]
    stage_tree(REPO, tmp_path / "package")  # the host-core deny list refuses a forbidden module itself
    root = tmp_path / "package"
    assert (root / "usr/lib/photo-wall-host-core/appliance/kernel/boot_stage.py").exists()
    assert (root / "usr/lib/photo-wall-node-bootstrap/appliance/kernel/boot_stage.py").exists()
    tmpfiles = (root / "usr/lib/tmpfiles.d/photo-wall-node.conf").read_text().splitlines()
    assert f"d {boot_stage.DIRECTORY} 0755 root root -" in tmpfiles
