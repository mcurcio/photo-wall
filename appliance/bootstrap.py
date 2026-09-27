"""Linux mount/device primitives shared by the netboot initramfs (0009).

The old signed-rootfs/boot-ticket bootstrap client (BootConfig, Fetcher,
copy_verified, boot(), the trial watchdog) has been retired. What remains is
the equipment-identity and mount plumbing that appliance/netboot_init.py
subclasses (see NetbootOps) and that player/service.py mirrors for the flashed
device_id derivation.

This module also owns stage 1's LIVENESS keeper (0014 rev 5, design §2.8): a
failed or hung boot must always come back. `arm_watchdog` is called first in
`netboot_init.main()`, before any of that -- not a trial watchdog kept for one
verification, but the one hardware watchdog stage 1 arms and hands over to
systemd at the end of a successful mount (`Keeper.hand_over`).
"""

from __future__ import annotations

import array
import fcntl
import os
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Final, Protocol

from contracts.equipment import READ_CAP, equipment_device_id
from uplink.files import write_atomically

CHUNK = 64 * 1024
ROOT_UID = 0
PI_SERIAL_PATH = "/sys/firmware/devicetree/base/serial-number"


def read_equipment_raw(name: str) -> bytes | None:
    """Read up to `READ_CAP` raw bytes of a hardware-identity file, or None if
    it is absent/unreadable.

    Shared by `LinuxOps.device_id` (every equipment-observation source) and
    `read_pi_serial` (the netboot base fetch's `X-PhotoWall-Serial` header) so
    both read the pi serial-number file the one, capped way -- an oversized
    file is detected by normalization downstream rather than silently
    truncated (see contracts.equipment.READ_CAP)."""
    try:
        with Path(name).open("rb") as stream:
            return stream.read(READ_CAP)
    except OSError:
        return None


def read_pi_serial(path: str = PI_SERIAL_PATH) -> str | None:
    """The Pi's raw hardware serial as a string, trailing NULs/whitespace
    stripped, for the netboot base fetch's `X-PhotoWall-Serial` header.

    Reads the SAME devicetree file `LinuxOps.device_id` hashes into the pi
    `device_id` (via `read_equipment_raw`), but returns the serial verbatim:
    central selects the per-serial base image from the un-hashed serial, while
    `device_id` needs the hashed form. Returns None off a Pi (no such file) so
    the caller can proceed unbound rather than fabricate an identity.

    Distinct from `player.service.read_pi_serial`, which returns raw bytes for
    the device_id hash and adds a `/proc/cpuinfo` fallback: that lives across
    the import boundary (player must not import appliance, and appliance must
    not pull player.service's GTK/GStreamer deps), so the read cannot be
    literally shared; the byte-level normalization it does share lives in
    contracts.equipment."""
    raw = read_equipment_raw(path)
    if raw is None:
        return None
    serial = raw.strip(b"\x00\r\n\t ").decode("ascii", "ignore")
    # Normalize once, here: a serial with an embedded control character (a NUL
    # mid-string, a stray \x07, DEL, ...) is treated as ABSENT, not sanitized
    # in place, so the console log, the X-PhotoWall-Serial header, and the
    # server all see the same safe value (or none) rather than three different
    # strippings of a malformed one.
    if not serial or any(ord(character) < 0x20 or ord(character) == 0x7f for character in serial):
        return None
    return serial


class BootstrapError(ValueError):
    """A fixed diagnostic code; no untrusted command or HTTP output."""


class BootstrapFatal(RuntimeError):
    """Mount cleanup failed, so another root must not be tried this boot."""


class LinuxOps:
    """Linux mount/device operations, injectable for portable state/fault tests."""

    equipment_observations = (
        ("pi", PI_SERIAL_PATH),
        ("dmi", "/sys/class/dmi/id/product_uuid"),
        ("qemu", "/sys/firmware/qemu_fw_cfg/by_name/opt/photo-wall/equipment-id/raw"),
    )

    def __init__(self, run_root: Path = Path("/run/photo-wall")):
        self.run_root = run_root
        self.run_root.mkdir(mode=0o755, parents=True, exist_ok=True)

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
            raw = read_equipment_raw(name)
            if raw is None:
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


# --- Stage-1 liveness (0014 rev 5, design §2.8) -----------------------------
#
# One rule: every segment of the boot cycle has one owner that resets the Pi
# if it stops, and every failure exit keeps that owner armed. Stage 1's owner
# is the hardware watchdog, armed first in `netboot_init.main()` (124s),
# petted by every phase line, every base block (`Keeper.paced`) and the FAILED
# line, and handed to systemd (`Keeper.hand_over`) once, as stage 1's last act
# after a successful mount. Neither `WatchdogDevice` nor `Keeper` exposes a way
# to disarm the hardware: a close without the magic 'V' write leaves it
# running, which is the point (an unhandled crash must still reset the Pi).

WATCHDOG_DEVICES: Final = (Path("/dev/watchdog0"), Path("/dev/watchdog"))
STAGE1_WATCHDOG_TIMEOUT: Final = 124  # s: about twice the longest inter-pet wait (60s), masking-safe
STAGE1_BUDGET: Final = 600            # s from arming; pets after this are refused
HANDOVER_DROPIN: Final = Path("/run/systemd/system.conf.d/90-photo-wall-watchdog.conf")
RUNTIME_WATCHDOG_SECONDS: Final = 30  # systemd pets every 15s
REBOOT_WATCHDOG_SECONDS: Final = 300  # systemd-shutdown and the kernel's reboot path
DRIVER_DEFAULT_TIMEOUT: Final = 15    # what an open with no WDIOC_SETTIMEOUT arms (photowall_restart)
WATCHDOG_TIMEOUTS: Final = (STAGE1_WATCHDOG_TIMEOUT, RUNTIME_WATCHDOG_SECONDS,
                            REBOOT_WATCHDOG_SECONDS, DRIVER_DEFAULT_TIMEOUT)
MIN_MASKED_SECONDS: Final = 12

WDIOC_SETTIMEOUT: Final = 0xC0045706
WDIOC_KEEPALIVE: Final = 0x80045705


def masked_hardware_seconds(timeout: int) -> int:
    """PURE. What a Pi watchdog driver that MASKS instead of clamping programs
    into the hardware for `timeout`: some older Raspberry Pi kernel branches
    take only the low 4 bits of the requested seconds instead of clamping a
    long timeout to the hardware's 16s ceiling, so (for example) 120s becomes
    an 8s hardware timeout. Every value in WATCHDOG_TIMEOUTS must clear
    MIN_MASKED_SECONDS on either driver variant, so the kernel's ~8s hardware
    pings keep a margin whatever kernel is staged."""
    return ((timeout << 16) & 0xFFFFF) >> 16


# (what the running kernel exposes, the value that means "on", the cmdline parameter)
KERNEL_LIVENESS: Final[tuple[tuple[Path, str, str], ...]] = (
    (Path("/sys/module/watchdog/parameters/stop_on_reboot"), "0", "watchdog.stop_on_reboot=0"),
    (Path("/proc/sys/kernel/hung_task_panic"), "1", "hung_task_panic=1"),
)


def read_text(path: Path) -> str | None:
    """The stripped text of `path`, or None if it does not exist or cannot be
    read. Shared default reader for `missing_kernel_liveness`."""
    try:
        return path.read_text().strip()
    except OSError:
        return None


def missing_kernel_liveness(checks: Sequence[tuple[Path, str, str]] = KERNEL_LIVENESS,
                            read: Callable[[Path], str | None] = read_text) -> list[str]:
    """The cmdline parameters (the third element of each `checks` row) whose
    effect the running kernel does not show -- an unreadable path counts as
    missing. Stage 1 logs one note naming them and boots on regardless."""
    return [parameter for path, expected, parameter in checks if read(path) != expected]


class WatchdogDevice(Protocol):
    """The kernel watchdog API, deliberately with no way to stop it."""

    def set_timeout(self, seconds: int) -> int: ...  # WDIOC_SETTIMEOUT; returns what the driver accepted

    def keepalive(self) -> None: ...                 # WDIOC_KEEPALIVE

    def release(self) -> None: ...                   # close WITHOUT the magic 'V': the hardware keeps counting


class _RealWatchdogDevice:
    """The real `WatchdogDevice`: a character-device fd. `release` only closes
    the fd -- it never writes 'V' and never issues WDIOS_DISABLECARD, so the
    hardware keeps counting after we let go of it."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = str(path)
        self._fd = fd

    def set_timeout(self, seconds: int) -> int:
        accepted = array.array("i", [seconds])
        fcntl.ioctl(self._fd, WDIOC_SETTIMEOUT, accepted, True)
        return accepted[0]

    def keepalive(self) -> None:
        fcntl.ioctl(self._fd, WDIOC_KEEPALIVE, array.array("i", [0]), True)

    def release(self) -> None:
        os.close(self._fd)


def open_watchdog(paths: Sequence[Path] = WATCHDOG_DEVICES) -> WatchdogDevice | None:
    """The first path in `paths` that opens as a character device (write-only,
    close-on-exec), else None."""
    for path in paths:
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            if not stat.S_ISCHR(os.fstat(fd).st_mode):
                os.close(fd)
                continue
        except OSError:
            os.close(fd)
            continue
        return _RealWatchdogDevice(path, fd)
    return None


class Keeper(Protocol):
    """What stage 1 pets. Returned only by `arm_watchdog`."""

    def pet(self) -> None: ...

    def paced(self, blocks: Iterable[bytes]) -> Iterator[bytes]:
        """Yield each block unchanged and pet after each one."""

    def hand_over(self) -> None:
        """Write HANDOVER_DROPIN (atomic, mode 0644), pet once, release the
        device. Called once, after the base is mounted, as stage 1's last
        act."""

    @property
    def summary(self) -> str: ...  # "armed device=/dev/watchdog0 timeout=124s" | "unarmed reason=no_device"


def handover_dropin() -> bytes:
    """PURE. Rendered from RUNTIME_WATCHDOG_SECONDS and REBOOT_WATCHDOG_SECONDS."""
    return (f"[Manager]\nRuntimeWatchdogSec={RUNTIME_WATCHDOG_SECONDS}s\n"
            f"RebootWatchdogSec={REBOOT_WATCHDOG_SECONDS}s\n").encode("ascii")


class _Keeper:
    """The real Keeper: arm_watchdog's return value."""

    def __init__(self, *, device: WatchdogDevice | None, accepted: int | None, reason: str | None,
                 dropin: Path, deadline: float, monotonic: Callable[[], float]) -> None:
        self._device = device
        self._accepted = accepted
        self._reason = reason
        self._dropin = dropin
        self._deadline = deadline
        self._monotonic = monotonic

    def pet(self) -> None:
        if self._device is None:
            return
        if self._monotonic() >= self._deadline:
            return
        self._device.keepalive()

    def paced(self, blocks: Iterable[bytes]) -> Iterator[bytes]:
        for block in blocks:
            yield block
            self.pet()

    def hand_over(self) -> None:
        write_atomically(self._dropin, handover_dropin(), mode=0o644)
        if self._device is not None:
            self._device.keepalive()
            self._device.release()

    @property
    def summary(self) -> str:
        if self._device is None:
            return f"unarmed reason={self._reason}"
        path = getattr(self._device, "path", "device")
        return f"armed device={path} timeout={self._accepted}s"


def arm_watchdog(*, device: WatchdogDevice | None, timeout: int = STAGE1_WATCHDOG_TIMEOUT,
                 budget: float = STAGE1_BUDGET, dropin: Path = HANDOVER_DROPIN,
                 monotonic: Callable[[], float] = time.monotonic) -> Keeper:
    """Set the timeout and pet once. No device, or a timeout the driver
    refuses to set (OSError), gives an unarmed Keeper: `pet` does nothing, and
    `hand_over` still writes the drop-in, so systemd arms the watchdog in
    stage 2. After `budget` seconds `pet` does nothing, so stage 1 cannot
    outlive it."""
    deadline = monotonic() + budget
    if device is None:
        return _Keeper(device=None, accepted=None, reason="no_device", dropin=dropin,
                       deadline=deadline, monotonic=monotonic)
    try:
        accepted = device.set_timeout(timeout)
    except OSError:
        return _Keeper(device=None, accepted=None, reason="set_timeout", dropin=dropin,
                       deadline=deadline, monotonic=monotonic)
    device.keepalive()
    return _Keeper(device=device, accepted=accepted, reason=None, dropin=dropin,
                   deadline=deadline, monotonic=monotonic)
