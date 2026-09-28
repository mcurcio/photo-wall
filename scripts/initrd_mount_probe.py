#!/usr/bin/env python3
"""Run stage 1's REAL mount path out of the BUILT initrd, against a real kernel (CI guard).

v0.9.1 passed every test and failed on the first Pi: `mount -o loop` meant nothing to the
klibc mount(8) initramfs-tools puts in the initrd, and no check ever ran stage 1's mounts with
the initrd's own userland. This does, with no fakes:

  1. unpack the shipped `initrd.img` the way the kernel does -- the boot data (stage 1's code,
     uncompressed) first, then the cached compressed archive over it -- into a directory;
  2. build a tiny squashfs holding one marker file inside that tree, shaped like the base where
     it matters here (merged /usr: `lib -> usr/lib`, and no kernel modules);
  3. chroot into the tree (devtmpfs, proc and sysfs mounted, as initramfs-tools' init does) and
     run `NetbootOps().mount_root` with the initrd's OWN python3 and PATH, so its own
     mount/umount/modprobe: loop attach, squashfs, tmpfs, overlay; then
     `NetbootOps().hand_over_modules`, stage 1's copy of its module tree onto the new root;
  4. pass only if the marker reads back through the merged overlay root, and the initrd's own
     modprobe resolves every display module (verify_netboot_initrd.DISPLAY_MODULES: vc4, v3d)
     against the NEW root for the initrd's kernel version, each file it names present there.

Step 4 asserts resolvability only (`modprobe --show-depends`): this runner boots its own
kernel, not the Pi's, so nothing is loaded. On the Pi the kernel is the initrd's own, so
`uname -r` names the same tree and the base's udev loads the drivers by alias at coldplug.

Afterwards it always unmounts everything under the tree, deepest first, and fails if a loop
device is still bound to the probe image once its mount is gone (AUTOCLEAR). The tree is
deleted only when nothing is mounted under it any more: it holds a devtmpfs, and deleting
through one deletes the host's device nodes.

Needs root, a Linux kernel with loop/squashfs/overlay, and the host's cpio, mksquashfs,
umount, chroot and env, plus the decompressor the cached archive uses (zstd, gzip or xz).
The host must be able to execute the initrd's binaries (base-image.yml: an arm64 runner).
Stdlib only.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from appliance.netboot_init import INITRD_MODULES as STAGE1_MODULES  # noqa: E402
from scripts.build_boot_data import read_archive  # noqa: E402
from scripts.verify_netboot_initrd import DISPLAY_MODULES  # noqa: E402

# initramfs-tools' init: `export PATH=/sbin:/usr/sbin:/bin:/usr/bin`.
INITRAMFS_PATH: Final = "/sbin:/usr/sbin:/bin:/usr/bin"
INITRD_PYTHON: Final = "/usr/bin/python3"
PROBE_DIR: Final = "probe"
PROBE_IMAGE: Final = "stage1-mount-probe.squashfs"
MARKER: Final = "stage1-mount-probe"
PROBE_SECONDS: Final = 120
# Where mkinitramfs puts the kernel's modules, relative to the unpacked initrd: stage 1's own
# INITRD_MODULES, the tree it hands over.
INITRD_MODULES: Final = STAGE1_MODULES.relative_to("/")
MOUNTINFO: Final = Path("/proc/self/mountinfo")
SYS_BLOCK: Final = Path("/sys/block")

# The leading bytes of the cached archive -> the host command that decompresses it to stdout.
DECOMPRESSORS: Final = (
    (b"\x28\xb5\x2f\xfd", ("zstd", "-dcq")),
    (b"\x1f\x8b", ("gzip", "-dc")),
    (b"\xfd7zXZ\x00", ("xz", "-dc")),
)

# Run by the INITRD's python3 inside the chroot: only stage 1's closure is importable there.
# argv: image, rootmnt, marker (relative to rootmnt), the initrd's kernel release, then the
# modules to resolve on the new root. One `resolve` line per module (resolution_violations).
PROBE_PROGRAM: Final = """\
import subprocess
import sys
from pathlib import Path
from appliance.netboot_init import NetbootOps, failure_line
image, rootmnt, marker, release = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
ops = NetbootOps()
try:
    ops.mount_root(image, rootmnt)
    print(ops.hand_over_modules(rootmnt, pet=lambda: None, release=release))
except Exception as error:
    print(failure_line("7", error))
    sys.exit(1)
print("marker=" + (rootmnt / marker).read_text().strip())
for module in sys.argv[5:]:
    shown = subprocess.run(["modprobe", "--dirname", str(rootmnt), "--set-version", release,
                            "--show-depends", module], capture_output=True, text=True)
    files = [line.split()[1] for line in shown.stdout.splitlines()
             if line.startswith("insmod ") and len(line.split()) > 1]
    missing = sum(1 for name in files if not Path(name).is_file())
    last = Path(files[-1]).name if files else "none"
    print(f"resolve {module} exit={shown.returncode} files={len(files)} missing={missing} "
          f"last={last}")
"""

Run = Callable[..., subprocess.CompletedProcess]


def decompressor(head: bytes) -> tuple[str, ...] | None:
    """PURE. The command that decompresses an archive starting with `head`; None if unknown."""
    for magic, command in DECOMPRESSORS:
        if head.startswith(magic):
            return command
    return None


def unpack(initrd: Path, root: Path, *, run: Run = subprocess.run) -> list[str]:
    """Extract `initrd` into `root` as the kernel does: each concatenated archive in order,
    each over the last -- the uncompressed ones (the boot data first; mkinitramfs may add its
    own early archive) and then the one compressed archive that ends the file. The violations
    (empty = unpacked)."""
    data = initrd.read_bytes()
    cpio = ("cpio", "-idmu", "--quiet", "--no-absolute-filenames")
    offset = 0
    while True:
        try:
            _, length = read_archive(data[offset:])
        except ValueError as error:
            if offset == 0:
                return [f"initrd does not start with the boot-data archive: {error}"]
            break
        run(cpio, input=data[offset:offset + length], cwd=root, check=True, capture_output=True)
        offset += length
    if offset == len(data):
        return []
    command = decompressor(data[offset:offset + 6])
    if command is None:
        return [f"archive at byte {offset}: unknown compression "
                f"(leading bytes {data[offset:offset + 6].hex()})"]
    decompressed = run(command, input=data[offset:], check=True, capture_output=True).stdout
    run(cpio, input=decompressed, cwd=root, check=True, capture_output=True)
    return []


def chroot_argv(root: Path, image: str, rootmnt: str, release: str,
                modules: Sequence[str] = DISPLAY_MODULES) -> list[str]:
    """PURE. Stage 1's mount_root and module hand-over under the initrd's python3, with init's
    PATH and nothing else from the host environment."""
    return ["env", "-i", f"PATH={INITRAMFS_PATH}", "chroot", str(root), INITRD_PYTHON, "-I",
            "-c", PROBE_PROGRAM, image, rootmnt, MARKER, release, *modules]


def kernel_release(root: Path) -> str | list[str]:
    """The one kernel release the unpacked initrd carries modules for (the name of its only
    directory under INITRD_MODULES), or the violations."""
    releases = sorted(path.name for path in (root / INITRD_MODULES).glob("*") if path.is_dir())
    if len(releases) != 1:
        return [f"the initrd carries modules for {len(releases)} kernel releases "
                f"({', '.join(releases) or 'none'}), not exactly one"]
    return releases[0]


def resolution_violations(output: str, modules: Sequence[str] = DISPLAY_MODULES) -> list[str]:
    """PURE. The chroot program's `resolve` lines -> the modules that did not resolve on the
    new root: modprobe failed, named no file, named a file the root lacks, or did not end at
    the module itself."""
    results = {fields[1]: dict(field.split("=", 1) for field in fields[2:] if "=" in field)
               for fields in (line.split() for line in output.splitlines())
               if len(fields) > 1 and fields[0] == "resolve"}
    violations = []
    for module in modules:
        result = results.get(module)
        if result is None:
            violations.append(f"{module}: not resolved on the new root (no result)")
        elif (result.get("exit") != "0" or result.get("files", "0") == "0"
              or result.get("missing") != "0"
              or not result.get("last", "").startswith(f"{module}.ko")):
            violations.append(f"{module}: does not resolve on the new root "
                              f"(exit={result.get('exit')} files={result.get('files')} "
                              f"missing={result.get('missing')} last={result.get('last')})")
    return violations


def _unescape(field: str) -> str:
    """A mountinfo path field: octal escapes (\\040 for a space, ...) decoded."""
    out, index = [], 0
    while index < len(field):
        if field[index] == "\\" and field[index + 1:index + 4].isdigit():
            out.append(chr(int(field[index + 1:index + 4], 8)))
            index += 4
        else:
            out.append(field[index])
            index += 1
    return "".join(out)


def mounts_under(mountinfo: str, root: Path) -> list[Path]:
    """PURE. Mount points at or under `root`, in the order they must be unmounted (the reverse
    of the order they were mounted: children before parents)."""
    prefix = str(root).rstrip("/") + "/"
    points = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) > 4:
            point = _unescape(fields[4])
            if point == str(root) or point.startswith(prefix):
                points.append(Path(point))
    return points[::-1]


def bound_loops(sys_block: Path, name: str) -> list[str]:
    """The loop devices (loopN) whose backing file's name is `name`."""
    found = []
    for backing in sorted(sys_block.glob("loop*/loop/backing_file")):
        try:
            text = backing.read_text().strip()
        except OSError:
            continue
        if text.removesuffix(" (deleted)").endswith("/" + name):
            found.append(backing.parent.parent.name)
    return found


def teardown(root: Path, *, run: Run = subprocess.run,
             mountinfo: Path = MOUNTINFO) -> list[str]:
    """Unmount everything at or under `root`, deepest first. The violations: what is still
    mounted afterwards (then the tree must NOT be deleted)."""
    for point in mounts_under(mountinfo.read_text(), root):
        run(["umount", str(point)], check=False, capture_output=True)
    return [f"still mounted after teardown: {point}"
            for point in mounts_under(mountinfo.read_text(), root)]


def probe(initrd: Path, work: Path, *, run: Run = subprocess.run,
          mountinfo: Path = MOUNTINFO,
          sys_block: Path = SYS_BLOCK) -> list[str]:
    """Unpack, mount, read the marker, tear down. The violations (empty = pass)."""
    root = work / "root"
    root.mkdir(parents=True)
    violations = unpack(initrd, root, run=run)
    if violations:
        return violations
    release = kernel_release(root)
    if isinstance(release, list):
        return release
    token = secrets.token_hex(8)
    source = work / "source"
    (source / "usr/lib").mkdir(parents=True)
    (source / "lib").symlink_to("usr/lib")        # the base is merged-/usr
    (source / MARKER).write_text(token + "\n")
    probe_dir = root / PROBE_DIR
    probe_dir.mkdir()
    run(["mksquashfs", str(source), str(probe_dir / PROBE_IMAGE), "-noappend", "-no-progress"],
        check=True, capture_output=True)
    # The loop/squashfs/overlay drivers: the chroot's modprobe has no modules for THIS kernel.
    run(["modprobe", "-a", "loop", "squashfs", "overlay"], check=False, capture_output=True)
    try:
        for fstype, target in (("devtmpfs", "dev"), ("proc", "proc"), ("sysfs", "sys")):
            (root / target).mkdir(exist_ok=True)
            run(["mount", "-t", fstype, fstype, str(root / target)], check=True,
                capture_output=True)
        result = run(chroot_argv(root, f"/{PROBE_DIR}/{PROBE_IMAGE}", f"/{PROBE_DIR}/root",
                                 release),
                     check=False, capture_output=True, text=True, timeout=PROBE_SECONDS)
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        if result.returncode != 0:
            violations.append(f"stage-1 mount_root failed in the initrd (exit "
                              f"{result.returncode}): {result.stdout.strip()}")
        elif f"marker={token}" not in result.stdout.splitlines():
            violations.append("the marker did not read back through the overlay root")
        else:
            violations += resolution_violations(result.stdout)
    finally:
        violations += teardown(root, run=run, mountinfo=mountinfo)
        for device in bound_loops(sys_block, PROBE_IMAGE):
            violations.append(f"/dev/{device} still bound to the probe image after its "
                              "unmount (not AUTOCLEAR)")
            run(["losetup", "-d", f"/dev/{device}"], check=False, capture_output=True)
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("initrd", type=Path, help="the shipped initrd.img (boot data + cached)")
    parser.add_argument("--work", type=Path,
                        help="an empty or absent directory to work in (default: a temp dir)")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("must run as root (loop devices, mounts, chroot)")
    work = args.work or Path(tempfile.mkdtemp(prefix="initrd-mount-probe-"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        violations = probe(args.initrd, work)
    except subprocess.CalledProcessError as error:
        stderr = error.stderr or b""
        detail = (stderr.decode("utf-8", "replace") if isinstance(stderr, bytes)
                  else stderr).strip()
        violations = [f"{' '.join(map(str, error.cmd[:2]))} failed: "
                      f"{detail or f'exit {error.returncode}'}"]
    if not mounts_under(MOUNTINFO.read_text(), work):
        shutil.rmtree(work, ignore_errors=True)
    else:
        violations.append(f"left {work} in place: something is still mounted under it")
    for violation in violations:
        print(violation)
    if violations:
        print("FAIL: stage 1's mount path or module hand-over does not work from the built "
              f"initrd ({len(violations)} violation(s))")
        return 1
    print("OK: stage 1 mounted a squashfs through the built initrd's own userland, and "
          f"{', '.join(DISPLAY_MODULES)} resolve on the new root from the modules it handed over")
    return 0


if __name__ == "__main__":
    sys.exit(main())
