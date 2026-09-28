"""Stage 1's mounts (`appliance/bootstrap.py` LinuxOps.mount_root): kernel options only, the
base's loop device attached by stage 1 itself, and a failing helper's cause on the FAILED line.

v0.9.1 on a Pi 5 failed phase 7 with "squashfs: Unknown parameter 'loop'": the initrd's
mount(8) is klibc's, which has no `-o loop` and hands the option to the kernel. These tests pin
the class, not the instance: every stage-1 mount goes through `LinuxOps.mount`, whose option
allowlist refuses anything the kernel itself does not take, and the loop device is attached
with the loop driver's ioctls (faked here; the real kernel path is
scripts/initrd_mount_probe.py in CI)."""

from __future__ import annotations

import contextlib
import errno
import os
import sys
from pathlib import Path

import pytest

import appliance.bootstrap as bootstrap
from appliance.bootstrap import (
    KERNEL_MOUNT_OPTIONS,
    LO_FLAGS_AUTOCLEAR,
    LO_FLAGS_READ_ONLY,
    LOOP_CONFIG,
    LOOP_CONFIGURE,
    LOOP_CTL_GET_FREE,
    LOOP_INFO64,
    LOOP_SET_FD,
    LOOP_SET_STATUS64,
    STDERR_TAIL,
    BootstrapError,
    LinuxOps,
    attached_loop,
    one_line,
)

LOOP = Path("/dev/loop7")


class RecordingOps(LinuxOps):
    """Real LinuxOps.mount/mount_root; `command` and the loop seam recorded, not run."""

    def __init__(self, run_root, *, fail_on=None):
        super().__init__(run_root)
        self.events = []
        self.fail_on = fail_on

    def command(self, *argv, timeout=30):
        self.events.append(("command", argv))
        if self.fail_on is not None and self.fail_on in argv:
            raise BootstrapError("boot_command", "mount: Invalid argument")
        return b""

    @contextlib.contextmanager
    def loop_device(self, image):
        self.events.append(("attach", image))
        try:
            yield LOOP
        finally:
            self.events.append(("release", image))


def mounts(events):
    return [argv for kind, argv in events if kind == "command" and argv[0] == "mount"]


def test_mount_root_mounts_the_attached_loop_device_with_kernel_options_only(tmp_path):
    ops = RecordingOps(tmp_path / "run")
    image = tmp_path / "base.squashfs"
    ops.mount_root(image, tmp_path / "root")
    run = tmp_path / "run"
    assert mounts(ops.events)[0] == ("mount", "-t", "squashfs", "-o", "ro,nodev", str(LOOP),
                                     str(run / "lower"))
    for argv in mounts(ops.events):
        names = {option.split("=", 1)[0] for option in argv[4].split(",")}
        assert names <= KERNEL_MOUNT_OPTIONS, argv
        assert "loop" not in names, argv
    # The loop hold is released right after the squashfs mount, before anything else.
    kinds = [event[0] if event[0] != "command" else event[1][2] for event in ops.events]
    assert kinds == ["attach", "squashfs", "release", "tmpfs", "overlay"]


def test_the_loop_hold_is_released_when_the_squashfs_mount_fails(tmp_path):
    ops = RecordingOps(tmp_path / "run", fail_on="squashfs")
    with pytest.raises(BootstrapError, match="boot_command"):
        ops.mount_root(tmp_path / "base.squashfs", tmp_path / "root")
    assert [event[0] for event in ops.events] == ["attach", "command", "release"]


@pytest.mark.parametrize("options", ["loop,ro,nodev", "ro,offset=4096", "ro,nofail",
                                     "x-systemd.automount", "defaults"])
def test_a_userland_only_mount_option_is_refused_before_any_helper_runs(tmp_path, options):
    ops = RecordingOps(tmp_path / "run")
    with pytest.raises(BootstrapError, match="boot_mount_option") as raised:
        ops.mount("squashfs", str(LOOP), tmp_path / "lower", options)
    assert raised.value.detail
    assert ops.events == []


def test_ram_mounts_tmpfs_through_the_same_allowlist(tmp_path):
    ops = RecordingOps(tmp_path / "run")
    ops.ram()
    assert mounts(ops.events) == [("mount", "-t", "tmpfs", "-o", "mode=0700,size=1100M,nodev,nosuid",
                                   "tmpfs", str(tmp_path / "run" / "ram"))]


# --- the loop seam: modprobe when the driver is not loaded ----------------------------------

@pytest.mark.parametrize("present", [True, False])
def test_loop_device_loads_the_driver_only_when_loop_control_is_absent(tmp_path, monkeypatch,
                                                                       present):
    control = tmp_path / "loop-control"
    if present:
        control.write_bytes(b"")
    monkeypatch.setattr(bootstrap, "LOOP_CONTROL", control)
    monkeypatch.setattr(bootstrap, "attached_loop", lambda image: contextlib.nullcontext(LOOP))
    commands = []

    class Ops(LinuxOps):
        def command(self, *argv, timeout=30):
            commands.append(argv)
            return b""

    with Ops(tmp_path / "run").loop_device(tmp_path / "image") as device:
        assert device == LOOP
    assert commands == ([] if present else [("modprobe", "loop")])


# --- attached_loop over a faked loop driver ---------------------------------------------------

class FakeLoopDriver:
    """fcntl.ioctl for /dev/loop-control and /dev/loopN; files stand in for the nodes."""

    def __init__(self, image, *, free=(3,), configure_errors=()):
        self.image = image
        self.free = list(free)
        self.configure_errors = list(configure_errors)
        self.calls = []
        self.configured = None

    def ioctl(self, fd, request, arg=0, *rest):
        self.calls.append(request)
        if request == LOOP_CTL_GET_FREE:
            return self.free.pop(0)
        if request == LOOP_CONFIGURE:
            if self.configure_errors:
                raise OSError(self.configure_errors.pop(0), "fake")
            backing, _, info, *_ = LOOP_CONFIG.unpack(arg)
            self._record(backing, LOOP_INFO64.unpack(info))
            return 0
        if request == LOOP_SET_FD:
            self.backing = arg
            return 0
        if request == LOOP_SET_STATUS64:
            self._record(self.backing, LOOP_INFO64.unpack(arg))
            return 0
        raise AssertionError(f"unexpected ioctl {request:#x}")

    def _record(self, backing, info):
        # The backing fd must still be open, on the image, while the driver takes it.
        assert os.fstat(backing).st_ino == self.image.stat().st_ino
        self.configured = {"flags": info[8], "name": info[9].rstrip(b"\0")}


@pytest.fixture
def loop_nodes(tmp_path):
    (tmp_path / "loop-control").write_bytes(b"")
    for number in range(3, 6):
        (tmp_path / f"loop{number}").write_bytes(b"")
    image = tmp_path / "base.squashfs"
    image.write_bytes(b"hsqs")
    return tmp_path, image


def open_fds():
    return set(os.listdir("/dev/fd")) if Path("/dev/fd").is_dir() else set()


def test_struct_layouts_match_linux_loop_h():
    assert (LOOP_INFO64.size, LOOP_CONFIG.size) == (232, 304)


def test_attached_loop_configures_read_only_autoclear_and_releases_its_hold(loop_nodes,
                                                                            monkeypatch):
    directory, image = loop_nodes
    driver = FakeLoopDriver(image)
    monkeypatch.setattr(bootstrap.fcntl, "ioctl", driver.ioctl)
    before = open_fds()
    with attached_loop(image, control=directory / "loop-control") as node:
        assert node == directory / "loop3"
        assert driver.configured == {"flags": LO_FLAGS_READ_ONLY | LO_FLAGS_AUTOCLEAR,
                                     "name": os.fsencode(str(image))[:63]}
    assert driver.calls == [LOOP_CTL_GET_FREE, LOOP_CONFIGURE]
    assert open_fds() == before     # control, backing and device fds all closed


def test_attached_loop_falls_back_to_set_fd_and_status_on_an_old_kernel(loop_nodes,
                                                                        monkeypatch):
    directory, image = loop_nodes
    driver = FakeLoopDriver(image, configure_errors=[errno.EINVAL])
    monkeypatch.setattr(bootstrap.fcntl, "ioctl", driver.ioctl)
    with attached_loop(image, control=directory / "loop-control") as node:
        assert node == directory / "loop3"
    assert driver.calls == [LOOP_CTL_GET_FREE, LOOP_CONFIGURE, LOOP_SET_FD, LOOP_SET_STATUS64]
    assert driver.configured["flags"] & LO_FLAGS_AUTOCLEAR


def test_attached_loop_retries_a_device_taken_under_it(loop_nodes, monkeypatch):
    directory, image = loop_nodes
    driver = FakeLoopDriver(image, free=(3, 4), configure_errors=[errno.EBUSY])
    monkeypatch.setattr(bootstrap.fcntl, "ioctl", driver.ioctl)
    before = open_fds()
    with attached_loop(image, control=directory / "loop-control") as node:
        assert node == directory / "loop4"
    assert open_fds() == before


def test_attached_loop_names_the_errno_when_there_is_no_loop_driver(tmp_path):
    image = tmp_path / "base.squashfs"
    image.write_bytes(b"hsqs")
    with pytest.raises(BootstrapError, match="boot_loop") as raised:
        with attached_loop(image, control=tmp_path / "absent" / "loop-control"):
            pass
    assert raised.value.detail == "ENOENT"


# --- command(): the helper's own words on failure (R9) ----------------------------------------

def test_a_failing_helper_carries_its_stderr_tail(tmp_path):
    script = ("import sys; sys.stderr.write('noise\\n' * 100 + "
              "'mount: mounting /dev/loop0 on /run/photo-wall/lower failed: Invalid argument\\n');"
              " sys.exit(32)")
    with pytest.raises(BootstrapError) as raised:
        LinuxOps(tmp_path / "run").command(sys.executable, "-c", script)
    assert str(raised.value) == "boot_command"
    assert raised.value.detail.endswith(
        "mount: mounting /dev/loop0 on /run/photo-wall/lower failed: Invalid argument")
    assert len(raised.value.detail) <= STDERR_TAIL


def test_a_silent_failing_helper_names_its_exit_status(tmp_path):
    with pytest.raises(BootstrapError) as raised:
        LinuxOps(tmp_path / "run").command(sys.executable, "-c", "raise SystemExit(3)")
    assert raised.value.detail == f"{sys.executable}: exit 3"


def test_a_helper_that_cannot_start_names_the_errno(tmp_path):
    with pytest.raises(BootstrapError) as raised:
        LinuxOps(tmp_path / "run").command(str(tmp_path / "no-such-helper"))
    assert raised.value.detail == f"{tmp_path / 'no-such-helper'}: ENOENT"


def test_one_line_keeps_the_tail_printable_and_on_one_line():
    assert one_line(b"a\x1b[31mb\r\n\tc\xff  d") == "a?[31mb c? d"
    assert one_line(b"x" * 300 + b"END") == "x" * (STDERR_TAIL - 3) + "END"
