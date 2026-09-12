"""Signed release verification and volatile trial watchdog; no local update slots."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from contracts.release import MAX_MANIFEST_BYTES, Release

OPENSSL = "/usr/bin/openssl"

class UpdateError(RuntimeError):
    pass


@contextmanager
def _reader(path: Path, limit: int):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                raise UpdateError("invalid_file")
            yield stream
    except OSError as error:
        raise UpdateError("unreadable_file") from error


def _regular(path: Path, limit: int) -> bytes:
    with _reader(path, limit) as stream:
        data = stream.read(limit+1)
        if len(data) > limit:
            raise UpdateError("oversized_file")
        return data


def _directory(path: Path, *, private: bool = False) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or (private and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700)):
        raise UpdateError("invalid_directory")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def verify_release(manifest: bytes, signature: bytes, public_key: Path,
                   boot_abi: str) -> Release:
    """Authenticate exact bytes using the pinned OpenSSL before parsing fields."""
    if (not isinstance(manifest, bytes) or not 0 < len(manifest) <= MAX_MANIFEST_BYTES
            or not isinstance(signature, bytes) or len(signature) != 64):
        raise UpdateError("invalid_signed_manifest")
    key = _regular(Path(public_key), 8192)
    try:
        lines = key.strip().splitlines()
        if lines[0] != b"-----BEGIN PUBLIC KEY-----" or lines[-1] != b"-----END PUBLIC KEY-----":
            raise ValueError
        der = base64.b64decode(b"".join(lines[1:-1]), validate=True)
        if len(der) != 44 or not der.startswith(bytes.fromhex("302a300506032b6570032100")):
            raise ValueError
    except (ValueError, IndexError) as error:
        raise UpdateError("invalid_ed25519_public_key") from error
    with tempfile.TemporaryDirectory(prefix="photo-wall-verify-") as directory:
        root = Path(directory)
        for name, data in (("manifest", manifest), ("signature", signature), ("key", key)):
            _private_write(root/name, data)
        try:
            result = subprocess.run(
                [OPENSSL, "pkeyutl", "-verify", "-pubin", "-inkey", str(root/"key"),
                 "-rawin", "-in", str(root/"manifest"), "-sigfile", str(root/"signature")],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UpdateError("signature_verifier_failed") from error
        if result.returncode:
            raise UpdateError("invalid_signature")
    try:
        release = Release.decode(manifest)
        release.require_compatible(boot_abi)
        return release
    except ValueError as error:
        raise UpdateError(str(error)) from error


def _accepted_current_health(boot_id: str, value: dict, now: float) -> bool:
    """Central owns the sustained-health policy; this only checks a fresh ack."""
    sampled = value.get("sampled_monotonic")
    player_id, epoch = value.get("player_id"), value.get("authority_epoch")
    return bool(value.get("boot_id") == boot_id and value.get("healthy") is True
                and value.get("release_accepted") is True
                and isinstance(player_id, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", player_id)
                and type(epoch) is int and epoch > 0
                and type(sampled) in (int, float) and math.isfinite(sampled)
                and 0 <= now - sampled <= 2)


def _linux_boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def boot_report(path: Path, boot_id: str) -> dict:
    """Read only the protected report from this RAM boot, never old disk state."""
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise UpdateError("invalid_boot_report")
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise UpdateError("invalid_boot_report")
            value[key] = item
        return value
    try:
        value = json.loads(_regular(path, MAX_MANIFEST_BYTES), object_pairs_hook=pairs)
        if (value.get("schema") != 2 or value.get("boot_id") != boot_id
                or type(value.get("trial")) is not bool
                or not re.fullmatch(r"[a-f0-9]{48}", value.get("ticket_id", ""))
                or not re.fullmatch(r"[a-f0-9]{64}", value.get("release_id", ""))):
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError):
        raise UpdateError("invalid_boot_report") from None


class TrialWatchdog:
    """Signal one reboot after a failed trial. The next release is central policy.

    Reboot is an injected effect; no library or test invokes systemctl. Accepted
    boots are never rebooted by this update watchdog. Nothing survives a reboot.
    """
    def __init__(self, boot_id: str, *, trial: bool, started: float,
                 signal_reboot: Callable[[str], None], timeout: float = 180):
        if not math.isfinite(started) or not math.isfinite(timeout) or timeout <= 0:
            raise UpdateError("invalid_watchdog")
        self.boot_id, self.trial, self.started = boot_id, trial, started
        self.signal_reboot, self.timeout = signal_reboot, timeout
        self.finished = not trial

    def observe(self, health: dict, now: float) -> bool:
        if self.finished:
            return True
        if not math.isfinite(now) or now < self.started:
            raise UpdateError("invalid_watchdog_clock")
        if _accepted_current_health(self.boot_id, health, now):
            self.finished = True
        elif now - self.started >= self.timeout:
            self.finished = True
            self.signal_reboot("trial_health_timeout")
        return self.finished


def watch_current(*, report_path: Path = Path("/run/photo-wall/boot.json"),
                  health_path: Path = Path("/run/photo-wall/player/service-health.json"),
                  signal_reboot: Callable[[str], None], monotonic=time.monotonic,
                  sleep=time.sleep, boot_id: str | None = None) -> None:
    identity = boot_id or _linux_boot_id()
    report = boot_report(report_path, identity)
    watchdog = TrialWatchdog(identity, trial=report["trial"], started=monotonic(),
                             signal_reboot=signal_reboot)
    while not watchdog.finished:
        try:
            health = json.loads(_regular(health_path, 65536))
            if not isinstance(health, dict):
                health = {}
        except (ValueError, UpdateError):
            health = {}
        watchdog.observe(health, monotonic())
        if not watchdog.finished:
            sleep(.5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("watch-current", "reboot-required"))
    arguments = parser.parse_args()
    report = Path("/run/photo-wall/boot.json")
    marker = Path("/run/photo-wall/reboot.json")
    boot_id = _linux_boot_id()
    if arguments.operation == "reboot-required":
        try:
            current = boot_report(report, boot_id)
            requested = json.loads(_regular(marker, MAX_MANIFEST_BYTES))
            allowed = current["trial"] and requested == {
                "boot_id": boot_id, "ticket_id": current["ticket_id"], "reason": "trial_health_timeout"}
        except (OSError, ValueError, UpdateError):
            allowed = False
        raise SystemExit(0 if allowed else 1)
    failed = []
    watch_current(signal_reboot=failed.append, boot_id=boot_id)
    if failed:
        current = boot_report(report, boot_id)
        _private_write(marker, json.dumps({"boot_id": boot_id, "ticket_id": current["ticket_id"],
                                          "reason": failed[0]}).encode())
        print(json.dumps(dict(event="photo-wall-trial-reboot-required", boot_id=boot_id,
                              ticket_sha256=hashlib.sha256(current["ticket_id"].encode()).hexdigest(),
                              release_id=current["release_id"], reason=failed[0])), flush=True)
        # systemd OnFailure owns the actual reboot, outside this testable seam.
        raise SystemExit(1)


if __name__ == "__main__":
    main()
