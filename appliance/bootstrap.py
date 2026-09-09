"""Stateless initramfs: central selects a signed immutable root copied into RAM."""

from __future__ import annotations

import argparse
import array
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import ssl
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from contracts.release import (
    MAX_MANIFEST_BYTES,
    MAX_ROOTFS_BYTES,
    BootRequest,
    BootTicket,
    Release,
    configuration_digest,
)

CHUNK = 64 * 1024
PLAYER_UID = 10001
PLAYER_GID = 10001
ROOT_UID = 0
WDIOC_SETTIMEOUT = 0xC0045706
TRIAL_WATCHDOG_SECONDS = 210


class BootstrapError(ValueError):
    """A fixed diagnostic code; no untrusted command or HTTP output."""


class BootstrapFatal(RuntimeError):
    """Mount cleanup failed, so another root must not be tried this boot."""


def _json(data: bytes) -> dict:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise BootstrapError("boot_configuration")
            result[key] = value
        return result

    try:
        result = json.loads(data, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise BootstrapError("boot_configuration") from None


def read_regular(path: Path, maximum: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
            raise BootstrapError("boot_file")
        result = bytearray()
        while block := os.read(fd, min(CHUNK, maximum + 1 - len(result))):
            result.extend(block)
            if len(result) > maximum:
                raise BootstrapError("boot_file")
        after = os.fstat(fd)
        if (len(result) != before.st_size or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns):
            raise BootstrapError("boot_file")
        return bytes(result)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class BootConfig:
    release_origin: str
    time_server: str
    boot_abi: str
    configuration_sha256: str
    directory: Path

    def __post_init__(self):
        try:
            url = urlsplit(self.release_origin)
            valid = (url.scheme == "https" and url.hostname and url.port != 0
                     and not url.username and not url.password and url.path in ("", "/")
                     and not url.query and not url.fragment and "\\" not in self.release_origin
                     and len(self.release_origin) <= 2048
                     and not any(ord(c) <= 32 for c in self.release_origin))
        except (ValueError, TypeError):
            valid = False
        if (not valid or not isinstance(self.time_server, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", self.time_server)
                or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                       for value in (self.boot_abi, self.configuration_sha256))):
            raise BootstrapError("boot_configuration")

    @classmethod
    def load(cls, directory: Path) -> BootConfig:
        files = {name: read_regular(directory / name, 1024**2) for name in (
            "public.json", "bootstrap.json", "ca.pem", "release.pub.pem")}
        boot = _json(files["bootstrap.json"])
        policy = _json(read_regular(directory / "boot-policy.json", MAX_MANIFEST_BYTES))
        if (set(boot) != {"schema", "release_origin", "time_server"}
                or type(boot["schema"]) is not int or boot["schema"] != 1
                or set(policy) != {"schema", "boot_abi", "configuration_sha256"}
                or type(policy["schema"]) is not int or policy["schema"] != 1
                or policy["configuration_sha256"] != configuration_digest(files)):
            raise BootstrapError("boot_configuration")
        return cls(boot["release_origin"], boot["time_server"], policy["boot_abi"],
                   policy["configuration_sha256"], directory)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BootstrapError("boot_redirect")


class Fetcher:
    """One bootstrap acquisition deadline, no proxies/redirects, exact body bounds."""

    def __init__(self, config: BootConfig, *, seconds: float = 120, opener=None,
                 monotonic=time.monotonic):
        if not 0 < seconds <= 120:
            raise BootstrapError("boot_deadline")
        self.config, self.monotonic = config, monotonic
        self.deadline = monotonic() + seconds
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(
                cafile=str(config.directory / "ca.pem"))))

    @contextlib.contextmanager
    def _deadline(self):
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise BootstrapError("boot_deadline")

        def expired(_signal, _frame):
            raise BootstrapError("boot_deadline")

        # Initramfs bootstrap is single threaded. This interrupts a slow-drip
        # header/body read, where an idle socket timeout alone is insufficient.
        previous = signal.signal(signal.SIGALRM, expired)
        old_timer = signal.setitimer(signal.ITIMER_REAL, remaining)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, *old_timer)
            signal.signal(signal.SIGALRM, previous)

    def _chunks(self, path: str, maximum: int, body: bytes | None = None):
        if type(maximum) is not int or not 0 < maximum <= MAX_ROOTFS_BYTES:
            raise BootstrapError("boot_limit")
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise BootstrapError("boot_deadline")
        request = urllib.request.Request(self.config.release_origin.rstrip("/")
                                         + path, data=body,
                                         headers={"Accept-Encoding": "identity",
                                                  "Content-Type": "application/json"})
        total = 0
        try:
            with self._deadline(), self.opener.open(request, timeout=min(10, remaining)) as response:
                if response.status != 200:
                    raise BootstrapError("boot_http")
                length = response.headers.get("Content-Length")
                if (length is not None and (not re.fullmatch(r"[0-9]{1,12}", length)
                                           or not 0 < int(length) <= maximum)):
                    raise BootstrapError("boot_limit")
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise BootstrapError("boot_encoding")
                while block := response.read(CHUNK):
                    total += len(block)
                    if total > maximum:
                        raise BootstrapError("boot_limit")
                    if self.monotonic() >= self.deadline:
                        raise BootstrapError("boot_deadline")
                    yield block
                if not total or (length is not None and total != int(length)):
                    raise BootstrapError("boot_truncated")
        except BootstrapError:
            raise
        except (OSError, urllib.error.URLError, ValueError):
            raise BootstrapError("boot_network") from None

    def chunks(self, name: str, maximum: int):
        if (name not in ("release.json", "release.sig")
                and not re.fullmatch(r"rootfs-[a-f0-9]{64}\.squashfs", name)):
            raise BootstrapError("boot_artifact_name")
        yield from self._chunks("/appliance/" + name, maximum)

    def ticket(self, request: BootRequest) -> BootTicket:
        # A lost HTTP response must replay the same once-only trial consumption.
        for attempt in range(3):
            try:
                payload = b"".join(self._chunks("/v1/bootstrap/boot", 2 * MAX_MANIFEST_BYTES,
                                               request.encode()))
                ticket = BootTicket.decode(payload)
                if (ticket.device_id, ticket.boot_id, ticket.request_id) != (
                        request.device_id, request.boot_id, request.request_id):
                    raise BootstrapError("boot_ticket_mismatch")
                return ticket
            except BootstrapError as error:
                if error.args[0] not in ("boot_network", "boot_truncated") or attempt == 2:
                    raise
        raise BootstrapError("boot_network")

    def read(self, name: str, maximum: int) -> bytes:
        return b"".join(self.chunks(name, maximum))


def copy_verified(chunks, release: Release, destination: Path) -> None:
    """Verify the exact immutable root while copying it into this boot's RAM."""
    total, digest = 0, hashlib.sha256()
    created = False
    try:
        with destination.open("xb") as output:
            created = True
            for block in chunks:
                if not isinstance(block, bytes) or not 0 < len(block) <= CHUNK:
                    raise BootstrapError("boot_chunk")
                total += len(block)
                if total > release.rootfs_size:
                    raise BootstrapError("boot_limit")
                digest.update(block)
                output.write(block)
            if total != release.rootfs_size or digest.hexdigest() != release.rootfs_sha256:
                raise BootstrapError("boot_integrity")
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def file_chunks(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ROOTFS_BYTES:
            raise BootstrapError("boot_file")
        while block := os.read(fd, CHUNK):
            yield block
    finally:
        os.close(fd)


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

    def boot_id(self) -> str:
        # procfs exposes a regular file with st_size=0, unlike artifact files.
        with Path("/proc/sys/kernel/random/boot_id").open("rb") as stream:
            value = stream.read(128).decode().strip()
        if not re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", value):
            raise BootstrapError("boot_identity")
        return value

    def device_id(self) -> str:
        # Pi firmware serial; DMI UUID and the explicit QEMU fixture observation
        # are equivalent fixed equipment identifiers.
        # Neither MAC/IP nor a freshly generated session key is equipment identity.
        for kind, name in self.equipment_observations:
            try:
                with Path(name).open("rb") as stream:
                    raw = stream.read(257).strip(b"\x00\r\n ").lower()
                if not raw or len(raw) > 256 or not re.fullmatch(rb"[a-z0-9-]+", raw):
                    continue
                return "device-" + hashlib.sha256(kind.encode() + b":" + raw).hexdigest()
            except OSError:
                continue
        raise BootstrapError("boot_equipment_identity")

    def time_ready(self, server: str) -> None:
        Path("/run/chrony").mkdir(mode=0o755, exist_ok=True)
        self.command("chronyd", "-q", "-u", "root", "-t", "20", "-f", "/dev/null", "server " + server + " iburst",
                     timeout=25)
        if time.time() < 1_735_689_600:
            raise BootstrapError("boot_time")

    def ram(self) -> Path:
        path = self.run_root / "ram"
        path.mkdir(mode=0o700)
        self.command("mount", "-t", "tmpfs", "-o", "mode=0700,size=1100M,nodev,nosuid", "tmpfs", str(path))
        return path

    def arm_trial_watchdog(self) -> None:
        """Start the kernel watchdog before any candidate bytes are trusted.

        The boot command line forces watchdog drivers into nowayout mode.  We
        deliberately keep the descriptor open until bootstrap exits; systemd
        then opens the same device and becomes its userspace keeper.
        """
        for path in ("/dev/watchdog0", "/dev/watchdog"):
            fd = None
            try:
                fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                info = os.fstat(fd)
                if not stat.S_ISCHR(info.st_mode):
                    os.close(fd)
                    continue
                timeout = array.array("i", [TRIAL_WATCHDOG_SECONDS])
                fcntl.ioctl(fd, WDIOC_SETTIMEOUT, timeout, True)
                os.write(fd, b"\0")
                self._watchdog_fd = fd
                return
            except OSError:
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
        raise BootstrapError("boot_watchdog")

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


def boot(config: BootConfig, rootmnt: Path, *, ops=None,
         fetcher_factory=Fetcher, verify=None) -> dict:
    from appliance.updates import verify_release

    ops, verify = ops or LinuxOps(), verify or verify_release
    request = BootRequest(ops.device_id(), ops.boot_id(), secrets.token_hex(24))
    ops.time_ready(config.time_server)
    fetcher = fetcher_factory(config)
    ticket = fetcher.ticket(request)
    if (ticket.device_id, ticket.boot_id, ticket.request_id) != (
            request.device_id, request.boot_id, request.request_id):
        raise BootstrapError("boot_ticket_mismatch")
    if ticket.trial:
        # Trial consumption is already durable centrally. Never enter a
        # candidate root unless the trusted initramfs has established the
        # reboot backstop that systemd will take over.
        ops.arm_trial_watchdog()
    release = verify(ticket.manifest.encode(), base64.b64decode(ticket.signature, validate=True),
                     config.directory / "release.pub.pem", config.boot_abi,
                     config.configuration_sha256)
    if release.release_id != ticket.release_id:
        raise BootstrapError("boot_release_mismatch")
    ram = ops.ram()
    image = ram / release.rootfs_name
    copy_verified(fetcher.chunks(release.rootfs_name, release.rootfs_size), release, image)
    report = dict(schema=2, boot_id=request.boot_id, device_id=request.device_id,
                  ticket_id=ticket.ticket_id, release_id=release.release_id,
                  trial=ticket.trial, persistence="volatile", fault=None)
    report_path = ops.run_root / "boot.json"
    try:
        report_path.write_bytes((json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode())
        if os.geteuid() == ROOT_UID:
            os.chown(report_path, ROOT_UID, PLAYER_GID)
        report_path.chmod(0o640)
        ops.mount_root(image, rootmnt)
    except BaseException:
        image.unlink(missing_ok=True)
        report_path.unlink(missing_ok=True)
        raise
    return report


def main():
    from appliance.updates import UpdateError

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rootmnt", type=Path, default=Path("/root"))
    args = parser.parse_args()
    try:
        boot(BootConfig.load(Path("/etc/photo-wall")), args.rootmnt)
    except (UpdateError, ValueError, OSError, BootstrapFatal):
        raise SystemExit("photo-wall: boot_failed") from None


if __name__ == "__main__":
    main()
