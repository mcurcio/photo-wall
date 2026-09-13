"""Linux mount/device primitives shared by the netboot initramfs (0009).

The old signed-rootfs/boot-ticket bootstrap client (BootConfig, Fetcher,
copy_verified, boot(), the trial watchdog) has been retired. What remains is
the equipment-identity and mount plumbing that appliance/netboot_init.py
subclasses (see NetbootOps) and that player/service.py mirrors for the flashed
device_id derivation.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

from contracts.equipment import READ_CAP, equipment_device_id

CHUNK = 64 * 1024
ROOT_UID = 0


class BootstrapError(ValueError):
    """A fixed diagnostic code; no untrusted command or HTTP output."""


class BootstrapFatal(RuntimeError):
    """Mount cleanup failed, so another root must not be tried this boot."""


class LinuxOps:
    """Linux mount/device operations, injectable for portable state/fault tests."""

    equipment_observations = (
        ("pi", "/sys/firmware/devicetree/base/serial-number"),
        ("dmi", "/sys/class/dmi/id/product_uuid"),
        ("qemu", "/sys/firmware/qemu_fw_cfg/by_name/opt/photo-wall/equipment-id/raw"),
    )

    def __init__(self, run_root: Path = Path("/run/photo-wall")):
        self.run_root = run_root
        self.run_root.mkdir(mode=0o755, parents=True, exist_ok=True)
        self._watchdog_fd: int | None = None

    def command(self, *argv: str, timeout: int = 30) -> bytes:
        with tempfile.TemporaryFile() as output:
            try:
                result = subprocess.run(argv, stdout=output, stderr=subprocess.DEVNULL,
                                        timeout=timeout, check=False)
                if result.returncode:
                    raise BootstrapError("boot_command")
                output.seek(0)
                data = output.read(CHUNK + 1)
                if len(data) > CHUNK:
                    raise BootstrapError("boot_command_limit")
                return data
            except (OSError, subprocess.TimeoutExpired):
                raise BootstrapError("boot_command") from None

    def device_id(self) -> str:
        # Pi firmware serial; DMI UUID and the explicit QEMU fixture observation
        # are equivalent fixed equipment identifiers.
        # Neither MAC/IP nor a freshly generated session key is equipment identity.
        # Normalization + hash live in contracts.equipment so the flashed-image
        # fallback (player/service.py hardware_boot_context) derives the SAME
        # device_id for the SAME Pi (0008: device_id is the immutable serial).
        for kind, name in self.equipment_observations:
            try:
                with Path(name).open("rb") as stream:
                    raw = stream.read(READ_CAP)
            except OSError:
                continue
            candidate = equipment_device_id(kind, raw)
            if candidate is not None:
                return candidate
        raise BootstrapError("boot_equipment_identity")

    def ram(self) -> Path:
        path = self.run_root / "ram"
        path.mkdir(mode=0o700)
        self.command("mount", "-t", "tmpfs", "-o", "mode=0700,size=1100M,nodev,nosuid", "tmpfs", str(path))
        return path

    def _prepare_root(self, rootmnt: Path) -> None:
        """Make the merged root traversable, and verify its protected owner."""
        rootmnt.chmod(0o755)
        info = rootmnt.stat()
        if (stat.S_IMODE(info.st_mode) != 0o755
                or (os.geteuid() == ROOT_UID and info.st_uid != ROOT_UID)):
            raise BootstrapError("root_permissions")

    def mount_root(self, image: Path, rootmnt: Path) -> None:
        lower, writable = self.run_root / "lower", self.run_root / "overlay"
        lower.mkdir(mode=0o755, exist_ok=True)
        writable.mkdir(mode=0o700, exist_ok=True)
        rootmnt.mkdir(parents=True, exist_ok=True)
        mounted = []
        try:
            self.command("mount", "-t", "squashfs", "-o", "loop,ro,nodev", str(image), str(lower))
            mounted.append(lower)
            self.command("mount", "-t", "tmpfs", "-o", "size=512M,mode=0700,nodev,nosuid", "tmpfs", str(writable))
            mounted.append(writable)
            # OverlayFS exposes the upper root's traversal mode at the merged
            # root. Keep its contents private while leaving the root traversable
            # for non-root system services in the selected userspace.
            upper = writable / "upper"
            work = writable / "work"
            upper.mkdir(mode=0o755)
            upper.chmod(0o755)
            work.mkdir(mode=0o700)
            work.chmod(0o700)
            options = f"lowerdir={lower},upperdir={upper},workdir={work}"
            self.command("mount", "-t", "overlay", "-o", options, "overlay", str(rootmnt))
            mounted.append(rootmnt)
            self._prepare_root(rootmnt)
        except BaseException:
            clean = True
            for path in reversed(mounted):
                try:
                    self.command("umount", str(path))
                except (OSError, BootstrapError):
                    clean = False
            if not clean:
                raise BootstrapFatal("boot_cleanup") from None
            raise
