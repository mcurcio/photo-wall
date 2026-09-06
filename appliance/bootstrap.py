"""Bounded stdlib initramfs bootstrap; mounts only existing owned Player state."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
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

from contracts.release import MAX_MANIFEST_BYTES, MAX_ROOTFS_BYTES, Release, configuration_digest

MARKER = b"photo-wall-state-v1\n"
CHUNK = 64 * 1024
PLAYER_UID = 10001
PLAYER_GID = 10001
ROOT_UID = 0


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

    def chunks(self, name: str, maximum: int):
        if (name not in ("release.json", "release.sig")
                and not re.fullmatch(r"rootfs-[a-f0-9]{64}\.squashfs", name)):
            raise BootstrapError("boot_artifact_name")
        if type(maximum) is not int or not 0 < maximum <= MAX_ROOTFS_BYTES:
            raise BootstrapError("boot_limit")
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise BootstrapError("boot_deadline")
        request = urllib.request.Request(self.config.release_origin.rstrip("/")
                                         + "/appliance/" + name,
                                         headers={"Accept-Encoding": "identity"})
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

    def read(self, name: str, maximum: int) -> bytes:
        return b"".join(self.chunks(name, maximum))


def copy_verified(chunks, release: Release, destination: Path) -> None:
    """Reverify the exact RAM copy, even when the slot already verified its source."""
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

    def __init__(self, run_root: Path = Path("/run/photo-wall"), *,
                 state_mount: Path = Path("/run/photo-wall-state")):
        self.run_root = run_root
        self.state_mount = state_mount
        # Keep the root-written boot report protected by its parent directory;
        # the Player publishes health below the separate player-owned child.
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

    def boot_id(self) -> str:
        # procfs exposes a regular file with st_size=0, unlike artifact files.
        with Path("/proc/sys/kernel/random/boot_id").open("rb") as stream:
            value = stream.read(128).decode().strip()
        if not re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", value):
            raise BootstrapError("boot_identity")
        return value

    def devices(self, label: str) -> list[str]:
        try:
            values = self.command("blkid", "-t", "LABEL=" + label, "-o", "device").decode().splitlines()
        except BootstrapError:
            return []
        if len(values) > 32 or any(not re.fullmatch(r"/dev/[A-Za-z0-9_./-]{1,120}", value)
                                   or ".." in value.split("/") for value in values):
            raise BootstrapError("boot_devices")
        return sorted(set(values))

    def state(self) -> Path | None:
        valid = []
        for index, device in enumerate(self.devices("PWSTATE")):
            probe = self.run_root / ("probe-" + str(index))
            probe.mkdir(mode=0o700)
            mounted = False
            try:
                self.command("mount", "-t", "ext4", "-o", "ro,noload,nodev,nosuid,noexec", device, str(probe))
                mounted = True
                marker = probe / ".photo-wall-state-v1"
                marker_info = marker.lstat()
                if (read_regular(marker, len(MARKER)) == MARKER and marker_info.st_uid == ROOT_UID
                        and not stat.S_IMODE(marker_info.st_mode) & 0o222):
                    valid.append(device)
            except (OSError, BootstrapError):
                pass
            finally:
                if mounted:
                    self.command("umount", str(probe))
                probe.rmdir()
        if len(valid) > 1:
            raise BootstrapError("state_ambiguous")
        if not valid:
            return None
        state = self.state_mount
        state.mkdir(mode=0o755, exist_ok=True)
        self.command("mount", "-t", "ext4", "-o", "rw,nodev,nosuid,noexec", valid[0], str(state))
        try:
            marker = state / ".photo-wall-state-v1"
            marker_info = marker.lstat()
            if (read_regular(marker, len(MARKER)) != MARKER or marker_info.st_uid != ROOT_UID
                    or stat.S_IMODE(marker_info.st_mode) & 0o222):
                raise BootstrapError("state_marker")
            root_info = state.stat()
            if root_info.st_uid != ROOT_UID or stat.S_IMODE(root_info.st_mode) not in (0o755, 0o711):
                raise BootstrapError("state_permissions")
            player = state / "player"
            if not player.exists() and not player.is_symlink():
                # Publish only a fully configured directory. A crash before rename
                # leaves a private empty temporary directory, never a wrong-owner
                # player directory that would disable persistence on every retry.
                pending = Path(tempfile.mkdtemp(prefix=".player-", dir=state))
                try:
                    fd = os.open(pending, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fchown(fd, PLAYER_UID, PLAYER_GID)
                        os.fchmod(fd, 0o700)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    pending.rename(player)
                    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                finally:
                    if pending.exists():
                        pending.rmdir()
            info = player.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != PLAYER_UID
                    or info.st_gid != PLAYER_GID or stat.S_IMODE(info.st_mode) != 0o700):
                raise BootstrapError("state_permissions")
            # A failed fsync/write must not be misreported as durable persistence.
            with tempfile.TemporaryFile(dir=state) as probe:
                probe.write(b"state-check\n")
                probe.flush()
                os.fsync(probe.fileno())
            return state
        except BaseException:
            self.command("umount", str(state))
            raise

    def common(self) -> Path | None:
        devices = self.devices("PWBOOT")
        if len(devices) != 1:
            return None
        path = self.run_root / "common"
        path.mkdir(mode=0o700)
        try:
            self.command("mount", "-t", "vfat", "-o", "ro,nodev,nosuid,noexec", devices[0], str(path))
            return path / "appliance"
        except BootstrapError:
            return None

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

    def _prepare_root(self, rootmnt: Path) -> None:
        """Make the merged root traversable, and verify its protected owner."""
        rootmnt.chmod(0o755)
        info = rootmnt.stat()
        if (stat.S_IMODE(info.st_mode) != 0o755
                or (os.geteuid() == ROOT_UID and info.st_uid != ROOT_UID)):
            raise BootstrapError("root_permissions")

    def mount_root(self, image: Path, rootmnt: Path, state: Path | None) -> None:
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
            if state is not None:
                target = rootmnt / "var/lib/photo-wall"
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
                self.command("mount", "--bind", str(state), str(target))
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


def boot(config: BootConfig, rootmnt: Path, *, ops=None, store_factory=None,
         fetcher_factory=Fetcher, verify=None) -> dict:
    from appliance.updates import SlotStore, UpdateError, verify_release

    ops = ops or LinuxOps()
    store_factory, verify = store_factory or SlotStore, verify or verify_release
    boot_id = ops.boot_id()
    state, store, fault = None, None, None
    try:
        state = ops.state()
        if state is not None:
            store = store_factory(state, config.directory / "release.pub.pem", config.boot_abi,
                                  config.configuration_sha256)
        else:
            fault = "state_missing"
    except UpdateError:
        fault, store = "update_storage", None
    except (OSError, ValueError):
        fault, state = "state_unusable", None
    ram = ops.ram()

    def mount(release, chunks=None, selection=None):
        image = ram / release.rootfs_name
        if chunks is not None:
            copy_verified(chunks, release, image)
        report = dict(schema=1, boot_id=boot_id, release_id=release.release_id,
                      slot=selection.slot if selection else None,
                      trial=selection.trial if selection else False,
                      persistence="durable" if state is not None else "volatile", fault=fault)
        report_path = ops.run_root / "boot.json"
        report_path.write_bytes((json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode())
        report_path.chmod(0o600)
        try:
            ops.mount_root(image, rootmnt, state)
        except BaseException:
            image.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)
            raise
        return report

    if store is not None:
        for _ in range(2):
            try:
                selection = store.select_boot(boot_id)
            except (UpdateError, OSError, ValueError):
                fault, store = "update_storage", None
                break
            if selection is None:
                break
            try:
                return mount(selection.release, file_chunks(selection.path), selection)
            except (OSError, ValueError):
                try:
                    store.reject_boot(selection.release.release_id, boot_id)
                except (UpdateError, OSError, ValueError):
                    fault, store = "update_storage", None
                    break
    common, release = ops.common(), None
    for source in ("disk", "network"):
        if source == "disk" and common is None:
            continue
        try:
            if source == "disk":
                payload = read_regular(common / "release.json", MAX_MANIFEST_BYTES)
                signature = read_regular(common / "release.sig", 64)
            else:
                ops.time_ready(config.time_server)
                fetcher = fetcher_factory(config)
                payload = fetcher.read("release.json", MAX_MANIFEST_BYTES)
                signature = fetcher.read("release.sig", 64)
            candidate = verify(payload, signature, config.directory / "release.pub.pem",
                               config.boot_abi, config.configuration_sha256)
            chunks = (file_chunks(common / candidate.rootfs_name) if source == "disk"
                      else fetcher.chunks(candidate.rootfs_name, candidate.rootfs_size))
            copy_verified(chunks, candidate, ram / candidate.rootfs_name)
            release = candidate
            break
        except (UpdateError, OSError, ValueError):
            if source == "network":
                raise
    if release is None:
        raise BootstrapError("boot_common")
    selection = None
    if store is not None:
        try:
            store.stage(payload, signature, file_chunks(ram / release.rootfs_name))
            selection = store.select_boot(boot_id)
            if selection is None or selection.release != release:
                raise BootstrapError("boot_selection")
        except (UpdateError, OSError, ValueError):
            fault, selection = "update_storage", None
    return mount(release, selection=selection)


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
