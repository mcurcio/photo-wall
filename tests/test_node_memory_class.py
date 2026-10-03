"""4 GB tracer T1: the device memory class chosen at construction, refusals with numbers, the
memory controller required before the store mounts, one source per memory cap, and the base
units' dependency, start-limit and OOM-score hygiene. No database, no systemd."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from test_netboot_liveness import _parse_unit

from appliance.node import capacity, storage_mount
from appliance.node.capacity import (
    CLASSES,
    GIB,
    MIB,
    OVERHEAD,
    PREPARATION_SLICE_BYTES,
    DeviceClass,
    StorageShort,
    admit_cold,
    device_class,
    memory_controller_present,
    memory_total,
    preparation_room,
)

SYSTEMD = Path(__file__).resolve().parents[1] / "appliance/systemd"
BASE_SERVICES = ("photo-wall-app-broker.service", "photo-wall-manager-supervisor.service",
                 "photo-wall-display.service", "photo-wall-display-controller.service")
SIZES = {"K": 1024, "M": MIB, "G": GIB}


def _unit(name: str) -> dict[str, dict[str, list[str]]]:
    return _parse_unit((SYSTEMD / name).read_text())


def _words(values: list[str]) -> list[str]:
    return [word for value in values for word in value.split()]


def _meminfo(tmp_path, total_kb: int) -> Path:
    path = tmp_path / "meminfo"
    path.write_text(f"MemTotal:       {total_kb} kB\nMemFree:        1000 kB\nMemAvailable:   900 kB\n")
    return path


# --- the device class ---------------------------------------------------------------------

def test_classes_are_the_interim_4gb_and_8gb_classes_in_ascending_order():
    assert CLASSES == (DeviceClass("pi5-4gb", 3584 * MIB, 2560 * MIB),
                       DeviceClass("pi5-8gb", 7168 * MIB, 3840 * MIB))
    for name in ("MIN_MEMORY", "RESERVE", "MAX_STORE", "storage_budget"):
        assert not hasattr(capacity, name), name


@pytest.mark.parametrize("args", [("x", 4 * GIB, 0), ("x", 4 * GIB, 4 * GIB), ("x", 4 * GIB, 5 * GIB),
                                  ("x", 4 * GIB, -1), ("", 4 * GIB, GIB), ("x", 4.0 * GIB, GIB)])
def test_an_invalid_device_class_cannot_be_constructed(args):
    with pytest.raises(ValueError, match="^device_class_invalid$"):
        DeviceClass(*args)


@pytest.mark.parametrize("total,name", [(3584 * MIB, "pi5-4gb"), (4045 * MIB, "pi5-4gb"),
                                        (7168 * MIB - 1, "pi5-4gb"), (7168 * MIB, "pi5-8gb"),
                                        (8 * GIB, "pi5-8gb"), (16 * GIB, "pi5-8gb")])
def test_device_class_is_the_largest_class_the_total_admits(total, name):
    assert device_class(total).name == name


@pytest.mark.parametrize("total", [0, 2 * GIB, 3584 * MIB - 1])
def test_a_board_below_the_smallest_class_is_refused_with_its_numbers(total):
    with pytest.raises(StorageShort, match="^node_memory_class$") as refused:
        device_class(total)
    assert (refused.value.required, refused.value.room, refused.value.fault) == (
        3584 * MIB, total, "node_memory_class")
    assert isinstance(refused.value, ValueError)


def test_storage_short_defaults_to_the_storage_capacity_fault():
    short = StorageShort(2, 1)
    assert (short.required, short.room, short.fault, str(short)) == (2, 1, "node_storage_capacity",
                                                                     "node_storage_capacity")


def test_memory_total_reads_memtotal_and_refuses_anything_else(tmp_path):
    assert memory_total(_meminfo(tmp_path, 4142000)) == 4142000 * 1024
    for text in ("MemAvailable: 1 kB\n", "MemTotal: x kB\n", "MemTotal: 1 MB\n", "MemTotal: 1 kB\nMemTotal: 2 kB\n"):
        (tmp_path / "bad").write_text(text)
        with pytest.raises(ValueError, match="^meminfo_invalid$"):
            memory_total(tmp_path / "bad")
    with pytest.raises(ValueError, match="^meminfo_invalid$"):
        memory_total(tmp_path / "absent")


def test_memory_controller_present_reads_cgroup_controllers_and_is_false_on_error(tmp_path):
    path = tmp_path / "cgroup.controllers"
    path.write_text("cpuset cpu io memory pids\n")
    assert memory_controller_present(path)
    path.write_text("cpuset cpu io pids\n")
    assert not memory_controller_present(path)
    assert not memory_controller_present(tmp_path / "absent")


def test_cold_admission_on_a_4gb_board_uses_its_class_store_and_refuses_with_numbers():
    reference = SimpleNamespace(environment_sha256="a" * 64, size_bytes=GIB)
    # 4 GB: store 2560 MiB holds a 1 GiB root twice plus overhead (tar era: MemAvailable admission).
    assert admit_cold([reference], total=4045 * MIB, available=3 * GIB, free=4 * GIB) == 2 * GIB + OVERHEAD
    big = SimpleNamespace(environment_sha256="b" * 64, size_bytes=1200 * MIB)
    with pytest.raises(StorageShort, match="^node_storage_capacity$") as refused:
        admit_cold([big], total=4045 * MIB, available=3 * GIB, free=4 * GIB)
    assert (refused.value.required, refused.value.room) == (2400 * MIB + OVERHEAD, 2560 * MIB)
    with pytest.raises(StorageShort) as short:
        admit_cold([reference], total=8 * GIB, available=GIB, free=4 * GIB)
    assert (short.value.required, short.value.room) == (2 * GIB + OVERHEAD, 512 * MIB)
    with pytest.raises(StorageShort, match="^node_memory_class$"):
        admit_cold([reference], total=3 * GIB, available=3 * GIB, free=4 * GIB)


def test_preparation_room_is_bounded_by_the_class_store():
    assert preparation_room(total=4045 * MIB, available=8 * GIB, free=8 * GIB, used=GIB) == 1536 * MIB
    assert preparation_room(total=8 * GIB, available=8 * GIB, free=8 * GIB, used=GIB) == 2816 * MIB


# --- the store mount ----------------------------------------------------------------------

def test_storage_refuses_an_absent_memory_controller_before_reading_memory(tmp_path):
    (tmp_path / "controllers").write_text("cpu io pids\n")
    for controllers in (tmp_path / "controllers", tmp_path / "absent"):
        with pytest.raises(ValueError, match="^memory_controller_absent$"):
            storage_mount.mount_storage(controllers=controllers, meminfo=tmp_path / "no-meminfo")


def test_storage_refuses_a_board_below_the_smallest_class_before_mounting(tmp_path, monkeypatch):
    (tmp_path / "controllers").write_text("cpu memory\n")
    monkeypatch.setattr(storage_mount.subprocess, "run", lambda *a, **k: pytest.fail("mounted"))
    with pytest.raises(StorageShort, match="^node_memory_class$") as refused:
        storage_mount.mount_storage(controllers=tmp_path / "controllers", meminfo=_meminfo(tmp_path, 2097152))
    assert (refused.value.required, refused.value.room) == (3584 * MIB, 2 * GIB)


@pytest.mark.parametrize("blocks,admitted", [(0, False), (640, True), (639, True), (641, False)])
def test_the_mounted_store_is_nonempty_and_within_the_class_store(monkeypatch, tmp_path, blocks, admitted):
    monkeypatch.setattr(storage_mount.os, "statvfs", lambda path: SimpleNamespace(f_blocks=blocks, f_frsize=4 * MIB))
    if admitted:
        storage_mount.require_mounted_size(tmp_path, 2560 * MIB)
    else:
        with pytest.raises(ValueError, match="^node_storage_mount_budget$"):
            storage_mount.require_mounted_size(tmp_path, 2560 * MIB)


# --- one source per cap --------------------------------------------------------------------

def _bytes(value: str) -> int:
    return int(value[:-1]) * SIZES[value[-1]] if value[-1] in SIZES else int(value)


def test_the_preparation_slice_cap_is_the_largest_store_plus_the_process():
    assert PREPARATION_SLICE_BYTES == 4096 * MIB
    assert _bytes(_unit("photowallpreparation.slice")["Slice"]["MemoryMax"][-1]) == PREPARATION_SLICE_BYTES


def test_preparation_transients_inherit_the_slice_cap_and_the_app_manager_scores_300(monkeypatch, tmp_path):
    from appliance.node import import_worker, manager_launcher
    launcher = manager_launcher.SystemdManagerLauncher(tmp_path, {"d": SimpleNamespace(entry_point="/entry")},
                                                       base_abi="b", graphics_abi="g", plugin_abi="p")
    monkeypatch.setattr(launcher, "verify", lambda digest: True)
    monkeypatch.setattr(launcher, "observed", lambda digest: False)
    launched = []

    def run(args, **kwargs):
        launched.append(args)
        raise RuntimeError("stop")

    monkeypatch.setattr(manager_launcher.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="stop"):
        launcher.start("d")
    [args] = launched
    properties = [args[i + 1] for i, word in enumerate(args) if word == "--property"]
    assert "Slice=photowallpreparation.slice" in properties and "OOMScoreAdjust=300" in properties
    assert not [p for p in properties if p.startswith("MemoryMax=")]
    source = Path(import_worker.__file__).read_text()
    assert "Slice=photowallpreparation.slice" in source and "MemoryMax=" not in source


def test_the_app_unit_scores_500_for_the_global_oom_killer():
    from appliance.node.process_linux import app_unit_properties
    properties = app_unit_properties(Path("/root"))
    assert [p for p in properties if p.startswith("OOMScoreAdjust=")] == ["OOMScoreAdjust=500"]


# --- base units ----------------------------------------------------------------------------

def test_prepare_requires_handoff_and_storage_and_the_owners_require_prepare():
    prepare = _unit("photo-wall-node-prepare.service")["Unit"]
    assert {"photo-wall-node-handoff.service", "photo-wall-node-storage.service"} <= set(_words(prepare["Requires"]))
    assert {"photo-wall-node-handoff.service", "photo-wall-node-storage.service"} <= set(_words(prepare["After"]))
    for name in ("photo-wall-app-broker.service", "photo-wall-manager-supervisor.service"):
        unit = _unit(name)["Unit"]
        assert "photo-wall-node-prepare.service" in _words(unit.get("Requires", [])), name
        assert "photo-wall-node-prepare.service" in _words(unit["After"]), name


@pytest.mark.parametrize("name", BASE_SERVICES)
def test_a_base_service_crash_loop_fails_the_unit_without_a_reboot(name):
    unit = _unit(name)
    assert unit["Unit"].get("StartLimitIntervalSec") == ["10min"]
    assert unit["Unit"].get("StartLimitBurst") == ["10"]
    assert "StartLimitAction" not in unit["Unit"] and "StartLimitAction" not in unit["Service"]
    assert unit["Service"].get("OOMScoreAdjust") == ["-500"]


def test_host_core_is_the_last_global_oom_victim():
    assert _unit("photo-wall-host-core.service")["Service"].get("OOMScoreAdjust") == ["-900"]
    scores = {name: int(_unit(name)["Service"]["OOMScoreAdjust"][-1]) for name in BASE_SERVICES}
    assert max(scores.values()) < 0 and min(scores.values()) > -900


def test_no_node_unit_sets_a_reboot_start_limit_action():
    for path in SYSTEMD.glob("photo-wall-*.service"):
        if path.name == "photo-wall-provision.service":
            continue  # the pre-node bootstrapper, rebooting into PXE by design
        unit = _unit(path.name)
        assert not any("StartLimitAction" in section for section in unit.values()), path.name
