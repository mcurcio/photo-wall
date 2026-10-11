"""4 GB tracer T1: the device memory class chosen at construction, refusals with numbers, the
memory controller reported (never required) before the store mounts, one source per memory cap, and the base
units' dependency, start-limit and OOM-score hygiene. No database, no systemd."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from support.repo import REPO
from test_netboot_liveness import _parse_unit

from appliance.boot import storage_mount
from appliance.kernel import capacity
from appliance.kernel.capacity import (
    CLASSES,
    GIB,
    MIB,
    STORE_BYTES,
    DeviceClass,
    StorageShort,
    admit_cold,
    admit_preparation,
    device_class,
    line,
    memory_controller_present,
    memory_total,
    preparation_room,
)

SYSTEMD = REPO / "appliance/systemd"
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

def test_classes_are_the_4gb_and_8gb_classes_in_ascending_order_with_the_image_store():
    # The store holds the image lines and its residue (E2c): 320 + 320 + 96 + 32 MiB.
    assert STORE_BYTES == 768 * MIB
    assert CLASSES == (DeviceClass("pi5-4gb", 3584 * MIB, STORE_BYTES),
                       DeviceClass("pi5-8gb", 7168 * MIB, STORE_BYTES))
    for name in ("MIN_MEMORY", "RESERVE", "MAX_STORE", "storage_budget", "OVERHEAD",
                 "PREPARATION_SLICE_BYTES", "PREPARATION_PROCESS_BYTES"):
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


def _image(digest: str, size: int) -> SimpleNamespace:
    return SimpleNamespace(environment_sha256=digest * 64, size_bytes=size)


def test_cold_admission_on_a_4gb_board_counts_each_image_once_and_refuses_with_numbers():
    app, manager = _image("a", 288 * MIB), _image("b", 62 * MIB)
    # Today's images: bytes are counted once (no unpack, no second copy).
    assert admit_cold([app, manager, app], total=4045 * MIB, available=3 * GIB, free=4 * GIB) == 350 * MIB
    assert admit_cold([app, manager], total=4045 * MIB, available=3 * GIB, free=4 * GIB,
                      resident=frozenset({"a" * 64})) == 62 * MIB
    big = _image("c", 720 * MIB)
    with pytest.raises(StorageShort, match="^node_storage_capacity$") as refused:
        admit_cold([big, manager], total=4045 * MIB, available=3 * GIB, free=4 * GIB)
    assert (refused.value.required, refused.value.room) == (782 * MIB, 768 * MIB)
    with pytest.raises(StorageShort) as short:
        admit_cold([app, manager], total=8 * GIB, available=800 * MIB, free=4 * GIB)
    assert (short.value.required, short.value.room) == (350 * MIB, 288 * MIB)
    with pytest.raises(StorageShort, match="^node_memory_class$"):
        admit_cold([app], total=3 * GIB, available=3 * GIB, free=4 * GIB)


def test_preparation_room_is_bounded_by_the_class_store():
    assert preparation_room(total=4045 * MIB, available=8 * GIB, free=8 * GIB, used=350 * MIB) == 418 * MIB
    assert preparation_room(total=8 * GIB, available=8 * GIB, free=8 * GIB, used=0) == STORE_BYTES


def test_online_admission_on_4gb_admits_one_target_beside_the_cold_images_and_refuses_a_third_app():
    roomy = {"total": 4045 * MIB, "available": 3 * GIB, "free": 4 * GIB}
    # Today's sizes: cold app 288 + manager 62 resident; an online target of 288 is admitted.
    assert admit_preparation(288 * MIB, **roomy, used=350 * MIB) == 288 * MIB
    # A third distinct app image in one boot is refused with both numbers (eviction is E5).
    with pytest.raises(StorageShort, match="^node_storage_capacity$") as refused:
        admit_preparation(288 * MIB, **roomy, used=638 * MIB)
    assert (refused.value.required, refused.value.room) == (288 * MIB, 130 * MIB)
    # At the line caps: app 320 + manager 96 resident plus one 16 KiB store file, a 320 target fits.
    at_caps = line("app-image").cap_bytes + line("manager-image").cap_bytes + 16 * 1024
    assert admit_preparation(line("app-image-rollback").cap_bytes, **roomy, used=at_caps) == 320 * MIB


# --- the store mount ----------------------------------------------------------------------

class _RootOwned(type(Path())):
    """A real directory that reads as root-owned and not group/other-writable."""

    def stat(self, *, follow_symlinks=True):
        real = super().stat(follow_symlinks=follow_symlinks)
        return SimpleNamespace(st_uid=0, st_mode=real.st_mode & ~0o022)


def _mount_fixture(monkeypatch, tmp_path):
    """mount_storage against a temporary store: the mount, its mountinfo row, the size check
    and chown are recorded, never run."""
    store = _RootOwned(tmp_path / "store")
    calls = {"mount": [], "sized": []}
    monkeypatch.setattr(storage_mount, "STORE", store)
    monkeypatch.setattr(storage_mount.os.path, "ismount", lambda path: False)
    monkeypatch.setattr(storage_mount.os, "chown", lambda *args: None)
    monkeypatch.setattr(storage_mount.subprocess, "run", lambda args, **kwargs: calls["mount"].append(args))
    monkeypatch.setattr(storage_mount, "require_mounted_size",
                        lambda path, size: calls["sized"].append((path, size)))
    mountinfo = SimpleNamespace(read_text=lambda: f"36 25 0:32 / {store} rw,nosuid - tmpfs photo-wall-node rw\n")
    monkeypatch.setattr(storage_mount, "Path", lambda value: mountinfo)
    return store, calls


@pytest.mark.parametrize("controllers", ["cpu memory\n", "cpu io pids\n", None])
def test_storage_mounts_the_class_store_and_checks_its_size_with_or_without_memcg(
        monkeypatch, tmp_path, controllers, caplog):
    # Report-only (errata E-FX2-1): an absent memory controller never refuses the store; it is
    # logged here, and HostCore reports it as memcg_present 0.
    path = tmp_path / "controllers"
    if controllers is not None:
        path.write_text(controllers)
    store, calls = _mount_fixture(monkeypatch, tmp_path)
    storage_mount.mount_storage(controllers=path, meminfo=_meminfo(tmp_path, 4045 * 1024))
    [mount] = calls["mount"]
    assert mount[:3] == ["/usr/bin/mount", "-t", "tmpfs"] and mount[-1] == str(store)
    assert f"size={768 * MIB}" in mount[mount.index("-o") + 1].split(",")
    assert calls["sized"] == [(store, 768 * MIB)]
    assert {child.name for child in store.iterdir()} == {"app-roots", "manager-roots", "root-images", "downloads", "preparation"}
    absent = "memory controller absent: memory limits not enforced" in caplog.text
    assert absent == (controllers is None or "memory" not in controllers.split())


def test_storage_refuses_a_board_below_the_smallest_class_before_mounting(tmp_path, monkeypatch):
    # The device class stays fail-closed, with or without the memory controller.
    monkeypatch.setattr(storage_mount.subprocess, "run", lambda *a, **k: pytest.fail("mounted"))
    for controllers in ("cpu memory\n", "cpu io\n"):
        (tmp_path / "controllers").write_text(controllers)
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


def test_the_preparation_slice_cap_is_its_members_sum():
    members = sum(item.cap_bytes for item in capacity.LINES if item.parent == "preparation")
    assert line("preparation").cap_bytes == members == 960 * MIB
    assert _bytes(_unit("photowallpreparation.slice")["Slice"]["MemoryMax"][-1]) == line("preparation").cap_bytes


def test_preparation_transients_inherit_the_slice_cap_and_the_app_manager_scores_300(monkeypatch, tmp_path):
    from appliance.apps import import_worker
    from appliance.node import manager_launcher
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
    from appliance.apps.process_linux import app_unit_properties
    properties = app_unit_properties(Path("/root"), ())
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


def test_an_oom_kill_inside_the_display_unit_does_not_stop_weston():
    # The diagnostic client runs in Weston's cgroup: its memcg OOM kill must not stop the unit
    # (systemd's default OOMPolicy=stop) and spend the start limit.
    assert _unit("photo-wall-display.service")["Service"].get("OOMPolicy") == ["continue"]


def test_host_core_is_the_last_global_oom_victim():
    assert _unit("photo-wall-host-core.service")["Service"].get("OOMScoreAdjust") == ["-900"]
    scores = {name: int(_unit(name)["Service"]["OOMScoreAdjust"][-1]) for name in BASE_SERVICES}
    assert max(scores.values()) < 0 and min(scores.values()) > -900


def test_no_node_unit_sets_a_reboot_start_limit_action():
    for path in SYSTEMD.glob("photo-wall-*.service"):
        unit = _unit(path.name)
        assert not any("StartLimitAction" in section for section in unit.values()), path.name
