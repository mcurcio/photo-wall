"""The Player unit's device grants: the GPU render node and the HEVC decoder, never raw KMS.

The sysfs tree is the Pi 5's as read on the test Pi (75628d0c, kernel 6.18, 2026-10-09):
v3d owns card0 and renderD128, vc4-drm owns card1 (the display controller), and the HEVC
decoder (a module the v0.22.1 base does not yet load) registers a video node and its
media-controller node on one platform device.
"""
import configparser
import shlex
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from appliance.apps import device_grants
from appliance.apps import process_linux as linux
from appliance.apps.device_grants import DeviceGrant, player_device_grants
from appliance.apps.process_linux import DISPLAY_SOCKET, app_unit_properties

RENDER_GID, VIDEO_GID = 991, 44  # the Pi's base; tests resolve them through the group lookup
GROUPS = {"render": RENDER_GID, "video": VIDEO_GID}
REPO = Path(__file__).resolve().parents[3]


def node(sysfs: Path, klass: str, name: str, device: str, driver: str, *, title: str | None = None,
         devname: str | None = None) -> None:
    platform = sysfs / "devices/platform/axi" / device
    (platform / "driver").parent.mkdir(parents=True, exist_ok=True)
    drivers = sysfs / "bus/platform/drivers" / driver
    drivers.mkdir(parents=True, exist_ok=True)
    if not (platform / "driver").is_symlink():
        (platform / "driver").symlink_to(drivers)
    entry = platform / klass / name
    entry.mkdir(parents=True)
    (entry / "device").symlink_to(platform)
    (entry / "uevent").write_text(f"MAJOR=1\nMINOR=2\nDEVNAME={devname or name}\n")
    if title is not None:
        (entry / "name").write_text(title + "\n")
    (sysfs / "class" / klass).mkdir(parents=True, exist_ok=True)
    (sysfs / "class" / klass / name).symlink_to(entry)


@pytest.fixture
def pi5(tmp_path):
    sysfs = tmp_path / "sys"
    node(sysfs, "drm", "card0", "1002000000.v3d", "v3d", devname="dri/card0")
    node(sysfs, "drm", "renderD128", "1002000000.v3d", "v3d", devname="dri/renderD128")
    node(sysfs, "drm", "card1", "axi:gpu", "vc4-drm", devname="dri/card1")
    node(sysfs, "video4linux", "video19", "1000800000.codec", "rpi-hevc-dec", title="rpi-hevc-dec")
    node(sysfs, "media", "media0", "1000800000.codec", "rpi-hevc-dec")
    node(sysfs, "video4linux", "video0", "1f00128000.csi", "rp1-cfe", title="rp1-cfe-csi2_ch0")
    node(sysfs, "media", "media1", "1f00128000.csi", "rp1-cfe")
    return sysfs


def test_the_pi5_grants_are_the_v3d_render_node_and_the_hevc_decoder(pi5):
    assert player_device_grants(pi5, group_id=GROUPS.get) == (
        DeviceGrant("/dev/dri/renderD128", RENDER_GID),
        DeviceGrant("/dev/video19", VIDEO_GID),
        DeviceGrant("/dev/media0", VIDEO_GID))


def test_a_render_node_of_another_driver_and_a_missing_group_are_not_granted(tmp_path):
    sysfs = tmp_path / "sys"
    node(sysfs, "drm", "renderD128", "gpu", "vc4-drm", devname="dri/renderD128")
    node(sysfs, "drm", "renderD129", "1002000000.v3d", "v3d", devname="dri/renderD129")
    assert player_device_grants(sysfs, group_id=GROUPS.get) == (
        DeviceGrant("/dev/dri/renderD129", RENDER_GID),)
    assert player_device_grants(sysfs, group_id={}.get) == ()
    assert player_device_grants(sysfs, group_id={"render": 0}.get) == ()  # never root's group
    assert player_device_grants(tmp_path / "empty", group_id=GROUPS.get) == ()


@pytest.mark.parametrize("path", ["/dev/dri/card0", "/dev/dri/card1", "/dev/dma_heap/system",
                                  "/dev/dri/renderD128/../card1", "/run/dri/renderD128"])
def test_raw_kms_and_anything_else_can_never_be_a_grant(path):
    with pytest.raises(ValueError, match="device_grant"):
        DeviceGrant(path, RENDER_GID)
    with pytest.raises(ValueError, match="device_grant"):
        DeviceGrant("/dev/dri/renderD128", 0)


def test_the_unit_keeps_private_devices_and_grants_exactly_the_nodes(pi5):
    props = app_unit_properties(Path("/r"), player_device_grants(pi5, group_id=GROUPS.get))
    assert "PrivateDevices=yes" in props
    assert [p for p in props if p.startswith("DeviceAllow=")] == [
        "DeviceAllow=/dev/dri/renderD128 rw", "DeviceAllow=/dev/video19 rw", "DeviceAllow=/dev/media0 rw"]
    assert "BindReadOnlyPaths=/dev/dri/renderD128 /dev/video19 /dev/media0" in props
    assert f"SupplementaryGroups={VIDEO_GID} {RENDER_GID} 10005" in props
    assert not any("card" in p or "dma_heap" in p or "char-drm" in p for p in props)
    assert not any(p.startswith(("BindPaths=", "DevicePolicy=")) for p in props)


def test_without_a_gpu_the_unit_has_no_device_grant():
    props = app_unit_properties(Path("/r"), ())
    assert "PrivateDevices=yes" in props and "SupplementaryGroups=10005" in props
    assert not any(p.startswith("DeviceAllow=") or "/dev/" in p for p in props)


def test_start_spawns_the_player_with_this_nodes_grants(pi5, tmp_path, monkeypatch):
    driver = linux.SystemdAppProcessDriver(tmp_path / "roots", None, base_abi="b", graphics_abi="g",
                                           plugin_abi="p", sysfs=pi5)
    environment = SimpleNamespace(environment_sha256="e" * 64, entry_point="/usr/bin/photo-wall-player")
    store = {"selected": {"environment": "env"}}
    driver.store = SimpleNamespace(read=store.get, write=store.__setitem__)
    monkeypatch.setattr(linux, "primitive", lambda value: "env")
    monkeypatch.setattr(driver, "current", lambda: None)
    monkeypatch.setattr(driver, "verify", lambda environment: True)
    monkeypatch.setattr(driver, "_await_unit_unloaded", lambda previous: None)
    monkeypatch.setattr(driver, "_observe", lambda *args: "running")
    monkeypatch.setattr(device_grants.grp, "getgrnam",
                        lambda name: SimpleNamespace(gr_gid=GROUPS[name]))  # the base's group database
    commands = []
    monkeypatch.setattr(linux.subprocess, "run", lambda command, **kwargs: commands.append(command))
    assert driver.start(environment, uuid4()) == "running"
    [command] = commands
    properties = [command[i + 1] for i, word in enumerate(command) if word == "--property"]
    assert "DeviceAllow=/dev/dri/renderD128 rw" in properties
    assert f"SupplementaryGroups={VIDEO_GID} {RENDER_GID} 10005" in properties
    assert not any("card" in p for p in properties)


def test_the_display_socket_is_the_one_weston_creates():
    unit = configparser.ConfigParser(strict=False, interpolation=None)
    unit.read(REPO / "appliance/systemd/photo-wall-display.service")
    words = shlex.split(unit["Service"]["ExecStart"])
    runtime = next(w.split("=", 1)[1] for w in words if w.startswith("XDG_RUNTIME_DIR="))
    socket = next(w.split("=", 1)[1] for w in words if w.startswith("--socket="))
    assert DISPLAY_SOCKET == f"{runtime}/{socket}"
