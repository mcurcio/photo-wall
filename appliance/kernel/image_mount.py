"""Read-only image mounts made by PID1 (E2c): stdlib only.

A release root is a squashfs image in the image pool, mounted read-only at its root directory
by a transient mount unit PID1 starts (`systemd-mount --collect`), so the mount lives in the
host namespace and propagates into every sandbox that needs it; a mount made in the stager's
own namespace would be invisible to the host (errata E-E2C-CUT-6). Nothing here unmounts: the
store is RAM and shutdown unmounts it.

A mount is read-only only when its per-mount options (mountinfo field 6) say `ro` AND its loop
device's sysfs `ro` is 1. squashfs's superblock is always read-only, so the superblock options
(and `findmnt`'s OPTIONS) say `ro` even for a writable mount; libmount also reuses an existing
loop for the same backing file, inheriting its writability (errata E-E2C-DR-4).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

IMAGE_MOUNT_OPTIONS: Final[tuple[str, ...]] = ("ro", "nodev", "nosuid", "loop")
IMAGE_FSTYPE: Final = "squashfs"
# A mount whose device is not a loop has no backing file: it is not an image, and it never
# equals a pool path (always absolute).
NO_IMAGE: Final = Path()
SYSTEMD_MOUNT: Final = "/usr/bin/systemd-mount"
MOUNT_TIMEOUT_SECONDS: Final = 60


@dataclass(frozen=True, slots=True)
class MountedImage:
    where: Path
    image: Path          # the loop's backing file; NO_IMAGE when the device is not a loop
    fstype: str
    read_only: bool      # per-mount `ro` and the loop's sysfs `ro` is 1


class ImageMounter(Protocol):
    def mounted(self, where: Path) -> MountedImage | None:
        """The mount at `where` in the caller's namespace (the topmost), or None."""

    def mount(self, image: Path, where: Path) -> MountedImage:
        """Mount `image` read-only at `where` through PID1, or return it already so mounted."""


def _unescape(field: str) -> str:
    """mountinfo escapes space, tab, newline and backslash as three octal digits."""
    out, index = [], 0
    while index < len(field):
        code = field[index + 1:index + 4]
        if field[index] == "\\" and len(code) == 3 and code.isdigit():
            out.append(chr(int(code, 8)))
            index += 4
        else:
            out.append(field[index])
            index += 1
    return "".join(out)


def _is(state: MountedImage | None, image: Path) -> bool:
    return (state is not None and state.fstype == IMAGE_FSTYPE and state.read_only
            and state.image == image)


class SystemdImageMounter:
    def __init__(self, *, mountinfo: Path = Path("/proc/self/mountinfo"),
                 sys_dev_block: Path = Path("/sys/dev/block")) -> None:
        self.mountinfo, self.sys_dev_block = mountinfo, sys_dev_block

    def mounted(self, where: Path) -> MountedImage | None:
        found = None
        for line in self.mountinfo.read_text().splitlines():
            fields = line.split()
            if len(fields) < 10 or "-" not in fields[6:] or _unescape(fields[4]) != str(where):
                continue
            found = fields  # a later line is mounted over an earlier one at the same point
        if found is None:
            return None
        fstype = found[found.index("-", 6) + 1]
        device = self.sys_dev_block / found[2]
        try:
            image = Path((device / "loop/backing_file").read_text().rstrip("\n"))
        except FileNotFoundError:
            return MountedImage(where, NO_IMAGE, fstype, False)
        try:
            loop_ro = (device / "ro").read_text().strip() == "1"
        except FileNotFoundError:
            loop_ro = False
        return MountedImage(where, image, fstype, "ro" in found[5].split(",") and loop_ro)

    def mount(self, image: Path, where: Path) -> MountedImage:
        current = self.mounted(where)
        if current is not None:
            if _is(current, image):
                return current
            raise ValueError("image_mount_conflict")  # never unmounted here
        argv = [SYSTEMD_MOUNT, "--no-ask-password", "--collect", "--type=" + IMAGE_FSTYPE,
                "--options=" + ",".join(IMAGE_MOUNT_OPTIONS), str(image), str(where)]
        try:
            result = subprocess.run(argv, env={"PATH": "/usr/bin", "LANG": "C"},
                                    capture_output=True, timeout=MOUNT_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError("image_mount_failed") from error
        if result.returncode != 0:
            raise ValueError("image_mount_failed")
        state = self.mounted(where)
        if state is None or not _is(state, image):
            raise ValueError("image_mount_not_visible")
        return state
