"""scripts/initrd_mount_probe.py, the CI guard that runs stage 1's real mount path out of the
built initrd. Its real run needs root, loop devices and an arm64 kernel (base-image.yml); here
every host command is faked, so these pin the probe's own logic: kernel-order unpacking, the
chroot command, the in-initrd program, the stage-2 module resolution, deepest-first teardown,
the AUTOCLEAR check, and that a tree with anything still mounted under it is never deleted."""

from __future__ import annotations

import gzip
import subprocess
from pathlib import Path

import pytest

import appliance.netboot_init as netboot_module
from appliance.bootstrap import BootstrapError
from scripts import initrd_mount_probe as probe_module
from scripts.build_boot_data import newc_archive
from scripts.initrd_mount_probe import (
    INITRAMFS_PATH,
    MARKER,
    PROBE_IMAGE,
    PROBE_PROGRAM,
    bound_loops,
    chroot_argv,
    decompressor,
    kernel_release,
    mounts_under,
    probe,
    resolution_violations,
    teardown,
    unpack,
)

BOOT_DATA = newc_archive({"usr/lib/python3.13/appliance/bootstrap.py": b"code"})
EARLY = newc_archive({"kernel/x86/microcode/fake.bin": b"ucode"})
CACHED = newc_archive({"usr/bin/mount": b"klibc"})
RELEASE = "6.18.50+rpt-rpi-2712"
# What the in-initrd program prints for a stage-2 module that resolves on the new root.
RESOLVED = ("resolve vc4 exit=0 files=16 missing=0 last=vc4.ko.xz\n"
            "resolve v3d exit=0 files=7 missing=0 last=v3d.ko.xz\n")


def completed(argv, stdout=b"", returncode=0):
    return subprocess.CompletedProcess(argv, returncode, stdout, b"")


@pytest.mark.parametrize("head,expected", [
    (b"\x28\xb5\x2f\xfd\x00\x00", ("zstd", "-dcq")),
    (b"\x1f\x8b\x08\x00\x00\x00", ("gzip", "-dc")),
    (b"\xfd7zXZ\x00", ("xz", "-dc")),
    (b"070701", None),
    (b"\x00\x00\x00\x00", None),
])
def test_decompressor_by_magic(head, expected):
    assert decompressor(head) == expected


# --- unpack: every archive, in the kernel's order ----------------------------------------------

class RecordingRun:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs.get("input")))
        if argv[0] == "gzip":
            return completed(argv, gzip.decompress(kwargs["input"]))
        return completed(argv)


def test_unpack_extracts_boot_data_then_early_archives_then_the_compressed_one(tmp_path):
    initrd = tmp_path / "initrd.img"
    initrd.write_bytes(BOOT_DATA + EARLY + gzip.compress(CACHED))
    run = RecordingRun()
    assert unpack(initrd, tmp_path, run=run) == []
    assert [(argv[0], data) for argv, data in run.calls] == [
        ("cpio", BOOT_DATA), ("cpio", EARLY), ("gzip", gzip.compress(CACHED)), ("cpio", CACHED)]


def test_unpack_refuses_an_initrd_without_boot_data_in_front(tmp_path):
    initrd = tmp_path / "initrd.img"
    initrd.write_bytes(gzip.compress(CACHED))
    assert unpack(initrd, tmp_path, run=RecordingRun())[0].startswith(
        "initrd does not start with the boot-data archive")


def test_unpack_names_an_unknown_compression(tmp_path):
    initrd = tmp_path / "initrd.img"
    initrd.write_bytes(BOOT_DATA + b"LZMA??" + bytes(10))
    assert unpack(initrd, tmp_path, run=RecordingRun()) == [
        f"archive at byte {len(BOOT_DATA)}: unknown compression (leading bytes "
        f"{(b'LZMA??').hex()})"]


# --- the command and the program run inside the initrd -----------------------------------------

def test_the_chroot_runs_the_initrds_own_python_with_inits_path_only(tmp_path):
    argv = chroot_argv(tmp_path, "/probe/i.squashfs", "/probe/root", RELEASE)
    assert argv[:5] == ["env", "-i", f"PATH={INITRAMFS_PATH}", "chroot", str(tmp_path)]
    assert argv[5:9] == ["/usr/bin/python3", "-I", "-c", PROBE_PROGRAM]
    assert argv[9:] == ["/probe/i.squashfs", "/probe/root", MARKER, RELEASE, "vc4", "v3d"]


def modprobe_shows(rootmnt, *, missing_file=False, unknown=()):
    """`modprobe --show-depends` as kmod answers it, over a real tree under `rootmnt`."""
    def run(argv, **kwargs):
        module = argv[-1]
        assert argv[:6] == ["modprobe", "--dirname", str(rootmnt), "--set-version", RELEASE,
                            "--show-depends"]
        if module in unknown:
            return subprocess.CompletedProcess(argv, 1, "", f"modprobe: FATAL: {module}\n")
        tree = rootmnt / "lib/modules" / RELEASE / "kernel"
        tree.mkdir(parents=True, exist_ok=True)
        lines = []
        for name in ("drm.ko.xz", f"{module}.ko.xz"):
            (tree / name).write_bytes(b"")
            if missing_file and name == f"{module}.ko.xz":
                (tree / name).unlink()
            lines.append(f"insmod {tree / name} \n")
        return subprocess.CompletedProcess(argv, 0, "".join(lines), "")
    return run


def run_program(monkeypatch, capsys, mount_root, tmp_path, *, shows=None):
    rootmnt = tmp_path / "root"
    handed = []
    monkeypatch.setattr(netboot_module.NetbootOps, "__init__", lambda self: None)
    monkeypatch.setattr(netboot_module.NetbootOps, "mount_root", mount_root)
    monkeypatch.setattr(netboot_module.NetbootOps, "hand_over_modules",
                        lambda self, rootmnt, *, pet, release=None: handed.append(release)
                        or f"modules={release} files=1 bytes=1")
    monkeypatch.setattr("subprocess.run", shows or modprobe_shows(rootmnt))
    monkeypatch.setattr("sys.argv", ["-c", str(tmp_path / "i.squashfs"), str(rootmnt), MARKER,
                                     RELEASE, "vc4", "v3d"])
    code = 0
    try:
        exec(compile(PROBE_PROGRAM, "<probe>", "exec"), {"__name__": "__main__"})
    except SystemExit as exit_:
        code = exit_.code
    return code, capsys.readouterr().out


def test_the_program_reads_the_marker_back_through_the_mounted_root(tmp_path, monkeypatch,
                                                                    capsys):
    def mount_root(self, image, rootmnt):
        rootmnt.mkdir()
        (rootmnt / MARKER).write_text("0123abcd\n")

    assert run_program(monkeypatch, capsys, mount_root, tmp_path) == (
        0, f"modules={RELEASE} files=1 bytes=1\nmarker=0123abcd\n"
           "resolve vc4 exit=0 files=2 missing=0 last=vc4.ko.xz\n"
           "resolve v3d exit=0 files=2 missing=0 last=v3d.ko.xz\n")


def test_the_program_reports_a_module_the_new_root_lacks(tmp_path, monkeypatch, capsys):
    def mount_root(self, image, rootmnt):
        rootmnt.mkdir()
        (rootmnt / MARKER).write_text("0123abcd\n")

    rootmnt = tmp_path / "root"
    code, out = run_program(monkeypatch, capsys, mount_root, tmp_path,
                            shows=modprobe_shows(rootmnt, missing_file=True, unknown=("v3d",)))
    assert code == 0
    assert out.splitlines()[-2:] == ["resolve vc4 exit=0 files=2 missing=1 last=vc4.ko.xz",
                                     "resolve v3d exit=1 files=0 missing=0 last=none"]
    assert resolution_violations(out) == [
        "vc4: does not resolve on the new root (exit=0 files=2 missing=1 last=vc4.ko.xz)",
        "v3d: does not resolve on the new root (exit=1 files=0 missing=0 last=none)"]


def test_a_program_that_never_resolved_a_module_is_a_violation():
    assert resolution_violations(RESOLVED) == []
    assert resolution_violations(RESOLVED.splitlines()[0]) == [
        "v3d: not resolved on the new root (no result)"]
    assert resolution_violations("resolve vc4 exit=0 files=3 missing=0 last=drm.ko.xz\n"
                                 + RESOLVED.splitlines()[1]) == [
        "vc4: does not resolve on the new root (exit=0 files=3 missing=0 last=drm.ko.xz)"]


def test_the_kernel_release_is_the_initrds_only_module_tree(tmp_path):
    assert kernel_release(tmp_path) == [
        "the initrd carries modules for 0 kernel releases (none), not exactly one"]
    (tmp_path / "usr/lib/modules" / RELEASE).mkdir(parents=True)
    assert kernel_release(tmp_path) == RELEASE
    (tmp_path / "usr/lib/modules/6.1.0-other").mkdir()
    assert kernel_release(tmp_path) == [
        f"the initrd carries modules for 2 kernel releases (6.1.0-other, {RELEASE}), "
        "not exactly one"]


def test_the_program_prints_stage_1s_own_failed_line(tmp_path, monkeypatch, capsys):
    def mount_root(self, image, rootmnt):
        raise BootstrapError("boot_command", "mount: Invalid argument")

    assert run_program(monkeypatch, capsys, mount_root, tmp_path) == (
        1, "FAILED phase=7 code=boot_command detail=mount: Invalid argument\n")


# --- teardown and the AUTOCLEAR check ----------------------------------------------------------

def mountinfo_line(number, point):
    return f"{number} 1 0:{number} / {point} rw,relatime shared:1 - tmpfs tmpfs rw\n"


def test_mounts_under_lists_children_first_and_decodes_escapes(tmp_path):
    root = tmp_path / "root"
    text = "".join([mountinfo_line(1, "/"), mountinfo_line(2, f"{root}/dev"),
                    mountinfo_line(3, f"{root}2/other"),
                    mountinfo_line(4, f"{root}/run/photo-wall/lower"),
                    mountinfo_line(5, f"{root}/probe/my\\040root")])
    assert mounts_under(text, root) == [root / "probe/my root", root / "run/photo-wall/lower",
                                        root / "dev"]


class FakeHost:
    """mount/umount against a mountinfo file; the chroot answers as the initrd would."""

    def __init__(self, tmp_path, *, chroot_output=None, chroot_code=0, stuck=(), loops=()):
        self.mountinfo = tmp_path / "mountinfo"
        self.mountinfo.write_text(mountinfo_line(1, "/"))
        self.sys_block = tmp_path / "sys-block"
        for number, backing in loops:
            (self.sys_block / f"loop{number}/loop").mkdir(parents=True)
            (self.sys_block / f"loop{number}/loop/backing_file").write_text(backing + "\n")
        self.chroot_output, self.chroot_code, self.stuck = chroot_output, chroot_code, stuck
        self.calls = []
        self.work = tmp_path / "work"

    def __call__(self, argv, **kwargs):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        if argv[0] == "cpio":
            (Path(kwargs["cwd"]) / "usr/lib/modules" / RELEASE).mkdir(parents=True,
                                                                      exist_ok=True)
        if argv[0] == "gzip":
            return completed(argv, gzip.decompress(kwargs["input"]))
        if argv[0] == "mount":
            with self.mountinfo.open("a") as stream:
                stream.write(mountinfo_line(len(self.calls) + 10, argv[-1]))
        if argv[0] == "umount" and argv[1] not in self.stuck:
            lines = self.mountinfo.read_text().splitlines(keepends=True)
            self.mountinfo.write_text("".join(line for line in lines
                                              if line.split()[4] != argv[1]))
        if argv[0] == "env":
            root = Path(argv[4])
            for point in ("probe/root", "run/photo-wall/lower", "run/photo-wall/overlay"):
                self(["mount", "-t", "x", "x", str(root / point)])
            token = (self.work / "source" / MARKER).read_text().strip()
            output = (self.chroot_output if self.chroot_output is not None
                      else f"marker={token}\n{RESOLVED}")
            return subprocess.CompletedProcess(argv, self.chroot_code, output, "")
        return completed(argv)

    def probe(self, initrd):
        return probe(initrd, self.work, run=self, mountinfo=self.mountinfo,
                     sys_block=self.sys_block)


@pytest.fixture
def initrd(tmp_path):
    path = tmp_path / "initrd.img"
    path.write_bytes(BOOT_DATA + gzip.compress(CACHED))
    return path


def test_a_probe_that_reads_the_marker_passes_and_leaves_nothing_mounted(tmp_path, initrd):
    host = FakeHost(tmp_path)
    assert host.probe(initrd) == []
    umounts = [call[1] for call in host.calls if call[0] == "umount"]
    root = host.work / "root"
    assert umounts == [str(root / point) for point in (
        "run/photo-wall/overlay", "run/photo-wall/lower", "probe/root", "sys", "proc", "dev")]
    assert mounts_under(host.mountinfo.read_text(), host.work) == []


def test_a_failing_mount_root_fails_the_probe_with_stage_1s_line(tmp_path, initrd):
    host = FakeHost(tmp_path, chroot_code=1,
                    chroot_output="FAILED phase=7 code=boot_command detail=mount: Invalid "
                                  "argument\n")
    assert host.probe(initrd) == [
        "stage-1 mount_root failed in the initrd (exit 1): FAILED phase=7 code=boot_command "
        "detail=mount: Invalid argument"]


def test_the_chroot_is_given_the_initrds_kernel_release(tmp_path, initrd):
    host = FakeHost(tmp_path)
    host.probe(initrd)
    [chroot] = [call for call in host.calls if call[0] == "env"]
    assert chroot[-3:] == [RELEASE, "vc4", "v3d"]
    assert (host.work / "source/lib").readlink() == Path("usr/lib")   # the base is merged-/usr


def test_a_stage_2_module_that_does_not_resolve_fails_the_probe(tmp_path, initrd):
    class Unresolved(FakeHost):
        def __call__(self, argv, **kwargs):
            result = super().__call__(argv, **kwargs)
            if str(argv[0]) == "env":
                result.stdout = result.stdout.replace("resolve v3d exit=0",
                                                      "resolve v3d exit=1")
            return result

    assert Unresolved(tmp_path).probe(initrd) == [
        "v3d: does not resolve on the new root (exit=1 files=7 missing=0 last=v3d.ko.xz)"]


def test_a_wrong_marker_fails_the_probe(tmp_path, initrd):
    host = FakeHost(tmp_path, chroot_output="marker=something-else\n")
    assert host.probe(initrd) == ["the marker did not read back through the overlay root"]


def test_a_loop_device_left_bound_fails_the_probe_and_is_detached(tmp_path, initrd):
    host = FakeHost(tmp_path, loops=[(4, f"/tmp/work/root/probe/{PROBE_IMAGE}"),
                                     (5, "/var/lib/snapd/snaps/core.snap")])
    assert host.probe(initrd) == [
        "/dev/loop4 still bound to the probe image after its unmount (not AUTOCLEAR)"]
    assert ["losetup", "-d", "/dev/loop4"] in host.calls


def test_bound_loops_matches_the_probe_image_by_name(tmp_path):
    host = FakeHost(tmp_path, loops=[(0, f"/w/probe/{PROBE_IMAGE} (deleted)"),
                                     (1, f"/w/probe/not-{PROBE_IMAGE}")])
    assert bound_loops(host.sys_block, PROBE_IMAGE) == ["loop0"]


def test_teardown_reports_what_stays_mounted(tmp_path):
    root = tmp_path / "root"
    host = FakeHost(tmp_path, stuck=(str(root / "dev"),))
    host(["mount", "-t", "devtmpfs", "devtmpfs", str(root / "dev")])
    assert teardown(root, run=host, mountinfo=host.mountinfo) == [
        f"still mounted after teardown: {root / 'dev'}"]


def test_main_keeps_a_tree_with_anything_still_mounted_under_it(tmp_path, monkeypatch, capsys):
    """Deleting through a devtmpfs deletes the host's device nodes."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "keep").write_text("")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(mountinfo_line(1, "/") + mountinfo_line(2, f"{work}/root/dev"))
    monkeypatch.setattr(probe_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(probe_module, "probe", lambda initrd, work: [])
    monkeypatch.setattr(probe_module, "MOUNTINFO", mountinfo)
    assert probe_module.main([str(tmp_path / "initrd.img"), "--work", str(work)]) == 1
    assert (work / "keep").exists()
    assert "something is still mounted under it" in capsys.readouterr().out
