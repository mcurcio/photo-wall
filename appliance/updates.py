"""Signed local userspace A/B slots. No network, formatting or Player cache writes."""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from contracts.release import MAX_MANIFEST_BYTES, MAX_ROOTFS_BYTES, Release

OPENSSL = "/usr/bin/openssl"
STATE_OWNER_UID = 0  # Tests on non-root development hosts explicitly substitute their fixture owner.
MARKER = b"photo-wall-state-v1\n"
MARGIN = 64*1024**2
SLOT_FILES = {"manifest.json", "manifest.sig", "rootfs.squashfs"}


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
                   boot_abi: str, configuration_sha256: str) -> Release:
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
        release.require_compatible(boot_abi, configuration_sha256)
        return release
    except ValueError as error:
        raise UpdateError(str(error)) from error


@dataclass(frozen=True)
class BootSelection:
    release: Release
    path: Path
    slot: str
    trial: bool
    boot_id: str


def _record(value) -> bool:
    return (isinstance(value, dict) and set(value) == {"slot", "release_id"}
            and value["slot"] in ("A", "B") and isinstance(value["release_id"], str)
            and re.fullmatch(r"[a-f0-9]{64}", value["release_id"]) is not None)


def _boot_id(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", value):
        raise UpdateError("invalid_boot_id")


class SlotStore:
    def __init__(self, state_root: Path, public_key: Path, boot_abi: str,
                 configuration_sha256: str):
        self.state_root, self.public_key = Path(state_root), Path(public_key)
        self.boot_abi, self.configuration_sha256 = boot_abi, configuration_sha256
        for value in (boot_abi, configuration_sha256):
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise UpdateError("invalid_compatibility_identity")
        _directory(self.state_root)
        info = self.state_root.lstat()
        marker = (self.state_root/".photo-wall-state-v1").lstat()
        if (info.st_uid != STATE_OWNER_UID or stat.S_IMODE(info.st_mode) not in (0o755, 0o711)
                or marker.st_uid != STATE_OWNER_UID or stat.S_IMODE(marker.st_mode) & 0o222):
            raise UpdateError("invalid_state_ownership")
        if _regular(self.state_root/".photo-wall-state-v1", len(MARKER)) != MARKER:
            raise UpdateError("unowned_state")
        self.root = self.state_root/"updates"
        try:
            self.root.mkdir(mode=0o700)
            _fsync_dir(self.state_root)
        except FileExistsError:
            pass
        _directory(self.root, private=True)

    @contextmanager
    def _locked(self):
        _directory(self.root, private=True)
        fd = os.open(self.root/"lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid()):
                raise UpdateError("invalid_lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise UpdateError("update_busy") from error
            yield
        finally:
            os.close(fd)

    def _state(self) -> dict:
        path = self.root/"state.json"
        if not path.exists() and not path.is_symlink():
            return dict(schema=1, active=None, pending=None, selected=None, rejected=[])
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        try:
            state = json.loads(_regular(path, 8192), object_pairs_hook=unique)
            if (not isinstance(state, dict)
                    or set(state) != {"schema", "active", "pending", "selected", "rejected"}
                    or type(state["schema"]) is not int or state["schema"] != 1):
                raise ValueError
            active, pending, selected = state["active"], state["pending"], state["selected"]
            if active is not None and not _record(active):
                raise ValueError
            if pending is not None and (not isinstance(pending, dict)
                    or set(pending) != {"slot", "release_id", "consumed"}
                    or type(pending["consumed"]) is not bool
                    or not _record({k: pending[k] for k in ("slot", "release_id")})):
                raise ValueError
            if selected is not None:
                if (not isinstance(selected, dict)
                        or set(selected) != {"slot", "release_id", "boot_id", "trial", "accepted"}
                        or type(selected["trial"]) is not bool or type(selected["accepted"]) is not bool
                        or not _record({k: selected[k] for k in ("slot", "release_id")})):
                    raise ValueError
                _boot_id(selected["boot_id"])
            if not isinstance(state["rejected"], list) or len(state["rejected"]) > 2:
                raise ValueError
            if active and pending and (active["slot"] == pending["slot"]
                                       or active["release_id"] == pending["release_id"]):
                raise ValueError
            if selected:
                owner = active if selected["accepted"] else pending
                if (owner is None or not all(selected[k] == owner[k] for k in ("slot", "release_id"))
                        or (not selected["accepted"] and (not selected["trial"] or not pending["consumed"]))):
                    raise ValueError
            for rejected in state["rejected"]:
                if (not isinstance(rejected, dict)
                        or set(rejected) != {"slot", "release_id", "boot_id"}
                        or not _record({k: rejected[k] for k in ("slot", "release_id")})):
                    raise ValueError
                _boot_id(rejected["boot_id"])
            return state
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError) as error:
            raise UpdateError("invalid_state") from error

    def _save(self, state: dict) -> None:
        temporary = self.root/"state.tmp"
        if temporary.exists() or temporary.is_symlink():
            _regular(temporary, 8192)
            temporary.unlink()
        try:
            _private_write(temporary, (json.dumps(state, sort_keys=True, separators=(",", ":"))+"\n").encode())
            os.replace(temporary, self.root/"state.json")
            _fsync_dir(self.root)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _clear_slot(self, slot: str) -> None:
        path = self.root/slot
        if not path.exists() and not path.is_symlink():
            return
        _directory(path, private=True)
        entries = {entry.name for entry in path.iterdir()}
        if not entries <= SLOT_FILES:
            raise UpdateError("unexpected_slot_file")
        for entry in path.iterdir():
            info = entry.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UpdateError("invalid_slot_file")
        for entry in path.iterdir():
            entry.unlink()
        path.rmdir()
        _fsync_dir(self.root)

    def _verify_slot(self, record: dict) -> BootSelection:
        slot = record["slot"]
        directory = self.root/slot
        _directory(directory, private=True)
        release = verify_release(_regular(directory/"manifest.json", MAX_MANIFEST_BYTES),
                                 _regular(directory/"manifest.sig", 64), self.public_key,
                                 self.boot_abi, self.configuration_sha256)
        if release.release_id != record["release_id"]:
            raise UpdateError("slot_release_mismatch")
        path = directory/"rootfs.squashfs"
        digest, total = hashlib.sha256(), 0
        with _reader(path, release.rootfs_size) as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size != release.rootfs_size):
                raise UpdateError("invalid_rootfs")
            while data := stream.read(1024**2):
                total += len(data)
                if total > release.rootfs_size:
                    raise UpdateError("invalid_rootfs")
                digest.update(data)
        if total != release.rootfs_size or digest.hexdigest() != release.rootfs_sha256:
            raise UpdateError("invalid_rootfs")
        return BootSelection(release, path, slot, False, "")

    def stage(self, manifest: bytes, signature: bytes, chunks: Iterable[bytes]) -> Release:
        with self._locked():
            state = self._state()
            selected = state["selected"]
            if selected and selected["trial"] and not selected["accepted"]:
                raise UpdateError("trial_selected")
            release = verify_release(manifest, signature, self.public_key,
                                     self.boot_abi, self.configuration_sha256)
            if state["active"] and state["active"]["release_id"] == release.release_id:
                self._verify_slot(state["active"])
                return release
            self._clear_slot("incoming")
            if shutil.disk_usage(self.root).free < release.rootfs_size+MARGIN:
                raise UpdateError("insufficient_space")
            incoming = self.root/"incoming"
            incoming.mkdir(mode=0o700)
            try:
                fd = os.open(incoming/"rootfs.squashfs",
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                digest, total = hashlib.sha256(), 0
                with os.fdopen(fd, "wb") as stream:
                    for chunk in chunks:
                        if not isinstance(chunk, bytes):
                            raise UpdateError("invalid_chunk")
                        total += len(chunk)
                        if total > release.rootfs_size:
                            raise UpdateError("rootfs_size_mismatch")
                        stream.write(chunk)
                        digest.update(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if total != release.rootfs_size or digest.hexdigest() != release.rootfs_sha256:
                    raise UpdateError("rootfs_integrity")
                _private_write(incoming/"manifest.json", manifest)
                _private_write(incoming/"manifest.sig", signature)
                _fsync_dir(incoming)
                inactive = "B" if state["active"] and state["active"]["slot"] == "A" else "A"
                self._clear_slot(inactive)
                os.replace(incoming, self.root/inactive)
                _fsync_dir(self.root)
                state["pending"] = dict(slot=inactive, release_id=release.release_id, consumed=False)
                self._save(state)
                return release
            finally:
                self._clear_slot("incoming")

    def select_boot(self, boot_id: str) -> BootSelection | None:
        _boot_id(boot_id)
        with self._locked():
            state = self._state()
            state["rejected"] = [r for r in state["rejected"] if r["boot_id"] == boot_id]
            selected = state["selected"]
            candidates = []
            if selected and selected["boot_id"] == boot_id:
                candidates.append((selected, selected["trial"] and not selected["accepted"]))
            pending = state["pending"]
            if pending and not pending["consumed"]:
                candidates.append((pending, True))
            if state["active"]:
                candidates.append((state["active"], False))
            for record, trial in candidates:
                if any(r["slot"] == record["slot"] and r["release_id"] == record["release_id"]
                       for r in state["rejected"]):
                    continue
                try:
                    result = self._verify_slot(record)
                except (OSError, UpdateError):
                    if trial and state["pending"]:
                        state["pending"]["consumed"] = True
                    continue
                if trial:
                    state["pending"]["consumed"] = True
                accepted = bool(state["active"] and state["active"]["slot"] == record["slot"]
                                and state["active"]["release_id"] == record["release_id"])
                state["selected"] = dict(slot=result.slot, release_id=result.release.release_id,
                                         boot_id=boot_id, trial=trial, accepted=accepted)
                self._save(state)  # Trial consumed before publishing path to bootstrap.
                return BootSelection(result.release, result.path, result.slot, trial, boot_id)
            state["selected"] = None
            self._save(state)
            return None

    def reject_boot(self, release_id: str, boot_id: str) -> bool:
        with self._locked():
            state = self._state()
            selected = state["selected"]
            rejection = next((r for r in state["rejected"] if r["release_id"] == release_id
                              and r["boot_id"] == boot_id), None)
            if rejection and selected is None:
                return False
            if not selected or (selected["release_id"], selected["boot_id"]) != (release_id, boot_id):
                raise UpdateError("selection_mismatch")
            state["rejected"] = [r for r in state["rejected"] if r["boot_id"] == boot_id]
            state["rejected"].append(dict(slot=selected["slot"], release_id=release_id, boot_id=boot_id))
            state["selected"] = None
            self._save(state)
            return True

    def mark_good(self, release_id: str, boot_id: str) -> bool:
        with self._locked():
            state = self._state()
            selected = state["selected"]
            if (not selected or (not selected["trial"] and not selected["accepted"])
                    or (selected["release_id"], selected["boot_id"]) != (release_id, boot_id)):
                raise UpdateError("trial_mismatch")
            if selected["accepted"]:
                return False
            self._verify_slot(selected)
            state["active"] = dict(slot=selected["slot"], release_id=release_id)
            state["pending"] = None
            selected["accepted"] = True
            self._save(state)
            return True


class HealthGate:
    """Continuous sampled health; gaps, stale samples and identity changes reset."""
    def __init__(self, boot_id: str, duration: float = 30, max_age: float = 2):
        self.boot_id, self.duration, self.max_age = boot_id, duration, max_age
        self.since = self.checked = self.sample = self.identity = None

    def observe(self, value: dict, now: float) -> bool:
        sampled = value.get("sampled_monotonic")
        identity = value.get("player_id"), value.get("authority_epoch")
        valid = (value.get("boot_id") == self.boot_id and value.get("healthy") is True
                 and value.get("persistence") == "durable"
                 and isinstance(identity[0], str)
                 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identity[0])
                 and type(identity[1]) is int and identity[1] > 0
                 and type(sampled) in (int, float) and math.isfinite(sampled)
                 and 0 <= now-sampled <= self.max_age)
        continuous = (valid and self.checked is not None and 0 <= now-self.checked <= self.max_age
                      and identity == self.identity and sampled is not None
                      and self.sample is not None and sampled >= self.sample)
        if not valid:
            self.since = self.identity = self.sample = None
        else:
            if not continuous or self.since is None:
                self.since = now
            self.identity, self.sample = identity, sampled
        self.checked = now
        return bool(valid and self.since is not None and now-self.since >= self.duration)


def _linux_boot_id() -> str:
    value = _regular(Path("/proc/sys/kernel/random/boot_id"), 64).decode().strip()
    if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", value):
        raise UpdateError("invalid_linux_boot_id")
    return value


def _boot_report(path: Path, boot_id: str, *, require_trial: bool = True) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate report field")
            result[key] = value
        return result

    try:
        with _reader(path, 4096) as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != STATE_OWNER_UID or stat.S_IMODE(info.st_mode) != 0o600:
                raise UpdateError("invalid_boot_report_ownership")
            data = stream.read(4097)
            if len(data) > 4096:
                raise UpdateError("invalid_boot_report")
        report = json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise UpdateError("invalid_boot_report") from error
    if (not isinstance(report, dict)
            or set(report) != {"schema", "boot_id", "release_id", "slot", "trial", "persistence", "fault"}
            or type(report["schema"]) is not int or report["schema"] != 1
            or report["boot_id"] != boot_id
            or not isinstance(report["release_id"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", report["release_id"])
            or report["slot"] not in ("A", "B") or type(report["trial"]) is not bool
            or (require_trial and report["trial"] is not True)
            or report["persistence"] != "durable" or report["fault"] is not None):
        raise UpdateError("boot_report_mismatch")
    return report


def validate_boot_report(path: Path, release_id: str, boot_id: str) -> None:
    """Bind service health to the successfully mounted rootfs reported by bootstrap."""
    if _boot_report(path, boot_id)["release_id"] != release_id:
        raise UpdateError("boot_report_mismatch")


def accept_trial(store: SlotStore, release_id: str, *,
                 boot_report: Path = Path("/run/photo-wall/boot.json"),
                 health_report: Path = Path("/run/photo-wall/player/service-health.json")) -> bool:
    """Accept only this actual boot's reported trial after 30 seconds of observed health."""
    boot_id = _linux_boot_id()
    validate_boot_report(boot_report, release_id, boot_id)
    gate = HealthGate(boot_id)
    deadline = time.monotonic()+180
    while time.monotonic() < deadline:
        try:
            health = json.loads(_regular(health_report, 4096))
            if not isinstance(health, dict):
                health = {}
        except (UpdateError, ValueError, RecursionError):
            health = {}
        sampled_at = time.monotonic()
        if sampled_at >= deadline:
            break
        if gate.observe(health, sampled_at):
            if _linux_boot_id() != boot_id:
                raise UpdateError("boot_changed")
            validate_boot_report(boot_report, release_id, boot_id)
            confirmed_at = time.monotonic()
            if confirmed_at >= deadline:
                break
            # Report/boot reads can themselves stall; never promote from an aged health sample.
            if not gate.observe(health, confirmed_at):
                continue
            return store.mark_good(release_id, boot_id)
        time.sleep(min(.25, max(0, deadline-time.monotonic())))
    raise UpdateError("healthy_trial_interval_not_met")


def accept_current(state_root: Path, config_dir: Path = Path("/etc/photo-wall"), *,
                   boot_report: Path = Path("/run/photo-wall/boot.json"),
                   health_report: Path = Path("/run/photo-wall/player/service-health.json")) -> bool:
    """Derive this boot's release and trust policy; never select or stage another release."""
    from appliance.bootstrap import BootConfig

    config = BootConfig.load(config_dir)
    boot_id = _linux_boot_id()
    report = _boot_report(boot_report, boot_id, require_trial=False)
    store = SlotStore(state_root, config.directory / "release.pub.pem", config.boot_abi,
                      config.configuration_sha256)
    if not report["trial"]:
        # Bootstrap reports an accepted active slot on an ordinary restart. It
        # is already authenticated and must not be subjected to the trial
        # health wait or mutate update state again. Require the protected
        # report to agree with the current accepted selection before no-oping.
        with store._locked():
            state = store._state()
            active, selected = state["active"], state["selected"]
            if (not active or not selected or not selected["accepted"]
                    or selected["trial"]
                    or selected["boot_id"] != boot_id
                    or any(selected[key] != report[key] for key in ("slot", "release_id"))
                    or any(active[key] != report[key] for key in ("slot", "release_id"))):
                raise UpdateError("boot_report_mismatch")
        return False
    return accept_trial(store, report["release_id"], boot_report=boot_report,
                        health_report=health_report)


def rollback_current_allowed(state_root: Path, config_dir: Path = Path("/etc/photo-wall"), *,
                             boot_report: Path = Path("/run/photo-wall/boot.json")) -> bool:
    """Permit a reboot only for a failed durable trial with a verified fallback.

    This is a read-only predicate over update state. The lock protects the
    decision from staging or acceptance while the two slot contents are
    authenticated; it never selects, rejects or promotes a release.
    """
    from appliance.bootstrap import BootConfig

    config = BootConfig.load(config_dir)
    boot_id = _linux_boot_id()
    report = _boot_report(boot_report, boot_id)
    store = SlotStore(state_root, config.directory / "release.pub.pem", config.boot_abi,
                      config.configuration_sha256)
    with store._locked():
        state = store._state()
        selected, pending, active = state["selected"], state["pending"], state["active"]
        if (not selected or selected["boot_id"] != boot_id or not selected["trial"]
                or selected["accepted"] or report["release_id"] != selected["release_id"]
                or report["slot"] != selected["slot"]
                or not pending or not pending["consumed"]
                or pending["slot"] != selected["slot"]
                or pending["release_id"] != selected["release_id"]
                or not active or active["slot"] == selected["slot"]
                or active["release_id"] == selected["release_id"]):
            return False
        fallback = store._verify_slot(active)
        return (fallback.release.release_id == active["release_id"]
                and fallback.release.release_id != selected["release_id"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--public-key", type=Path)
    parser.add_argument("--boot-abi")
    parser.add_argument("--configuration-sha256")
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage")
    for name in ("manifest", "signature", "rootfs"):
        stage.add_argument("--"+name, required=True, type=Path)
    commands.add_parser("select")
    for command in ("reject", "mark-good"):
        commands.add_parser(command).add_argument("--release-id", required=True)
    current = commands.add_parser("accept-current")
    current.add_argument("--config-dir", type=Path, default=Path("/etc/photo-wall"))
    rollback = commands.add_parser("rollback-current-allowed")
    rollback.add_argument("--config-dir", type=Path, default=Path("/etc/photo-wall"))
    args = parser.parse_args()
    explicit = args.public_key, args.boot_abi, args.configuration_sha256
    if args.command == "accept-current":
        if any(value is not None for value in explicit):
            parser.error("accept-current derives trust policy from --config-dir; no overrides")
        boot_id = _linux_boot_id()
        accepted = accept_current(args.state_root, args.config_dir)
        # Public completion evidence follows the real health gate and durable
        # promotion. No credentials, paths, or health-report contents are logged.
        print(json.dumps(dict(event="photo-wall-trial-acceptance", boot_id=boot_id,
                              accepted=accepted)), flush=True)
        return
    if args.command == "rollback-current-allowed":
        if any(value is not None for value in explicit):
            parser.error("rollback-current-allowed derives trust policy from --config-dir; no overrides")
        try:
            boot_id = _linux_boot_id()
            allowed = rollback_current_allowed(args.state_root, args.config_dir)
        except (UpdateError, OSError, ValueError):
            raise SystemExit(1) from None
        if allowed:
            # The recovery service logs this only after authenticating both the
            # failed trial and its fallback, before requesting its own reboot.
            print(json.dumps(dict(event="photo-wall-rollback-allowed", boot_id=boot_id,
                                  allowed=True)), flush=True)
        raise SystemExit(0 if allowed else 1)
    if any(value is None for value in explicit):
        parser.error("this command requires --public-key, --boot-abi and --configuration-sha256")
    store = SlotStore(args.state_root, args.public_key, args.boot_abi, args.configuration_sha256)
    if args.command == "stage":
        with _reader(args.rootfs, MAX_ROOTFS_BYTES) as stream:
            release = store.stage(_regular(args.manifest, MAX_MANIFEST_BYTES),
                                  _regular(args.signature, 64), iter(lambda: stream.read(1024**2), b""))
        print(release.release_id)
        return
    boot_id = _linux_boot_id()
    if args.command == "select":
        selection = store.select_boot(boot_id)
        print(json.dumps(None if selection is None else dict(
            release_id=selection.release.release_id, path=str(selection.path), slot=selection.slot,
            trial=selection.trial, boot_id=selection.boot_id)))
    elif args.command == "reject":
        store.reject_boot(args.release_id, boot_id)
    else:
        accept_trial(store, args.release_id)


if __name__ == "__main__":
    main()
