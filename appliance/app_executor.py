"""One base-owned app mutation port for cold boot and future authorized online attempts.

The local journal records intent before service stop. It is volatile across PXE
boots but survives provisioner process restart. A stale or corrupt journal
blocks a new mutation until its earlier effect is reconciled.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from appliance.app_launcher import CONFIG as PLAYER_CONFIG
from appliance.app_launcher import PYTHON as PLAYER_PYTHON
from appliance.app_payload import PayloadError, stage_payload, verify_root
from contracts.player_payload import MAX_EXPANDED_BYTES, MAX_MANIFEST_BYTES, MAX_MEMBERS
from contracts.strict_json import loads_object
from uplink.files import write_atomically

ROOTS = Path("/run/photo-wall/apps")
JOURNAL = Path("/run/photo-wall/app-mutation.json")
LOCK = Path("/run/photo-wall/app-executor.lock")
LEGACY_UNIT_OVERRIDE = Path("/etc/systemd/system/photo-wall-player.service")
EXPECTED_ABI = Path("/usr/lib/photo-wall-bootstrapper/base-abi.txt")
UNIT = "photo-wall-player.service"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_STATES = frozenset({"intent_stop", "stopped", "activated", "start_requested",
                     "committed", "rolled_back", "recovery_required"})
# Provisional admission reserves, not a physical Pi capacity qualification.
DEFAULT_MEMORY_HEADROOM_BYTES = 512 * 1024 * 1024
DEFAULT_FILESYSTEM_HEADROOM_BYTES = 64 * 1024 * 1024
# File contents plus manifest and a conservative allowance for tmpfs pages,
# directory entries, and the temporary root used before atomic publication.
STAGING_ALLOWANCE_BYTES = (MAX_EXPANDED_BYTES + MAX_MANIFEST_BYTES
                           + 2 * MAX_MEMBERS * 4096)


class ExecutorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """A locally verified terminal attempt; no network or Central claim is implied."""

    attempt_id: str
    state: str  # committed or rolled_back
    active_sha256: str


@dataclass(frozen=True, slots=True)
class SelectedSnapshot:
    """A terminal attempt and active pointer observed under one short read lock."""

    attempt_id: str
    state: str
    digest: str


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    filesystem_free_bytes: int
    memory_available_bytes: int


class CapacityProbe(Protocol):
    def snapshot(self, roots: Path) -> CapacitySnapshot: ...


class LinuxCapacityProbe:
    """Observe writable tmpfs space and reclaimable system RAM at admission."""

    def __init__(self, meminfo: Path = Path("/proc/meminfo")) -> None:
        self.meminfo = meminfo

    def snapshot(self, roots: Path) -> CapacitySnapshot:
        try:
            filesystem = os.statvfs(roots)
            raw = self.meminfo.read_bytes()
        except OSError as exc:
            raise ExecutorError("app_capacity_unavailable") from exc
        match = re.search(rb"^MemAvailable:\s*([0-9]+) kB\s*$", raw, re.MULTILINE)
        if match is None:
            raise ExecutorError("app_capacity_unavailable")
        return CapacitySnapshot(filesystem.f_bavail * filesystem.f_frsize,
                                int(match.group(1)) * 1024)


class PlayerService(Protocol):
    def active(self) -> bool: ...
    def running_digest(self) -> str | None: ...
    def stop(self) -> None: ...
    def start(self) -> None: ...


class SystemdPlayer:
    """Bounded PID1 effects; all callers enter through AppExecutor's local lock."""

    def __init__(self, roots: Path = ROOTS, proc_root: Path = Path("/proc")) -> None:
        self.roots, self.proc_root = roots, proc_root

    def active(self) -> bool:
        result = subprocess.run(["systemctl", "is-active", "--quiet", UNIT],
                                check=False, timeout=5)
        if result.returncode == 0:
            return True
        if result.returncode == 3:  # LSB: known inactive/failed unit
            return False
        raise ExecutorError("player_unit_state_unknown")

    def running_digest(self) -> str | None:
        """Read MainPID's actual argv; a pointer and an active unit do not prove a process."""
        try:
            shown = subprocess.run(["systemctl", "show", "--value", "--property=MainPID", UNIT],
                                   capture_output=True, text=True, check=False, timeout=5)
            pid_text = shown.stdout.strip()
            if shown.returncode != 0 or not pid_text.isdecimal() or int(pid_text) <= 0:
                return None
            raw = (self.proc_root / pid_text / "cmdline").read_bytes()
        except (OSError, subprocess.SubprocessError):
            return None
        if not 0 < len(raw) <= 4096 or not raw.endswith(b"\x00"):
            return None
        argv = raw.split(b"\x00")
        if (len(argv) != 7 or argv[:3] != [PLAYER_PYTHON.encode(), b"-I", b"-B"]
                or argv[4:] != [b"--config", PLAYER_CONFIG.encode(), b""]):
            return None
        try:
            app_path = argv[3].decode("ascii")
        except UnicodeError:
            return None
        match = re.fullmatch(re.escape(str(self.roots)) + r"/([0-9a-f]{64})/app", app_path)
        return match.group(1) if match is not None else None

    def stop(self) -> None:
        subprocess.run(["systemctl", "stop", UNIT], check=True, timeout=150)
        if self.active():
            raise ExecutorError("player_stop_unconfirmed")

    def start(self) -> None:
        subprocess.run(["systemctl", "start", UNIT], check=True, timeout=90)
        if not self.active():
            raise ExecutorError("player_start_unconfirmed")


def expected_abi(path: Path = EXPECTED_ABI) -> str:
    try:
        value = path.read_text().strip()
    except OSError as exc:
        raise ExecutorError("base_abi_unavailable") from exc
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ExecutorError("base_abi_invalid")
    return value


class AppExecutor:
    """One locked mutation and deterministic restart repair, independent of OS telemetry."""

    def __init__(self, *, roots: Path = ROOTS, journal: Path = JOURNAL,
                 lock: Path = LOCK, legacy_override: Path = LEGACY_UNIT_OVERRIDE,
                 service: PlayerService | None = None,
                 capacity_probe: CapacityProbe | None = None,
                 protected_memory_bytes: int = DEFAULT_MEMORY_HEADROOM_BYTES,
                 protected_filesystem_bytes: int = DEFAULT_FILESYSTEM_HEADROOM_BYTES) -> None:
        if (type(protected_memory_bytes) is not int or protected_memory_bytes < 0
                or type(protected_filesystem_bytes) is not int
                or protected_filesystem_bytes < 0):
            raise ValueError("protected_capacity_invalid")
        self.roots, self.journal, self.lock = roots, journal, lock
        self.legacy_override = legacy_override
        self.service = service or SystemdPlayer(roots)
        self.capacity_probe = capacity_probe or LinuxCapacityProbe()
        self.protected_memory_bytes = protected_memory_bytes
        self.protected_filesystem_bytes = protected_filesystem_bytes

    def _require_capacity(self, *, staging_bytes: int = 0,
                          archive_copy_bytes: int = 0) -> None:
        """Refuse a healthy-app stop unless both writable space and RAM retain headroom.

        The pre-stage check covers a complete extra expanded root plus a possible
        in-memory archive copy. The second check uses no speculative growth and
        runs after both exact roots have been verified, immediately before intent.
        These are admission bounds; the physical peak still needs measurement.
        """
        snapshot = self.capacity_probe.snapshot(self.roots)
        if (type(snapshot.filesystem_free_bytes) is not int
                or snapshot.filesystem_free_bytes <
                self.protected_filesystem_bytes + staging_bytes):
            raise ExecutorError("app_filesystem_capacity_insufficient")
        if (type(snapshot.memory_available_bytes) is not int
                or snapshot.memory_available_bytes <
                self.protected_memory_bytes + staging_bytes + archive_copy_bytes):
            raise ExecutorError("app_memory_capacity_insufficient")

    def _read(self, path: Path) -> dict | None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ExecutorError("executor_state_unavailable") from exc
        value = loads_object(raw, max_bytes=2048)
        if value is None:
            raise ExecutorError("executor_state_invalid")
        return value

    def _active(self) -> str | None:
        value = self._read(self.roots / "active.json")
        if value is None:
            return None
        digest = value.get("sha256")
        if (set(value) != {"schema", "sha256"} or value["schema"] != 1
                or not isinstance(digest, str) or _SHA256.fullmatch(digest) is None):
            raise ExecutorError("active_root_invalid")
        return digest

    def _write_active(self, digest: str | None) -> None:
        path = self.roots / "active.json"
        if digest is None:
            path.unlink(missing_ok=True)
        else:
            write_atomically(path, json.dumps({"schema": 1, "sha256": digest},
                                              sort_keys=True).encode(), mode=0o644)

    def _journal(self) -> dict | None:
        value = self._read(self.journal)
        if value is None:
            return None
        if (set(value) != {"schema", "attempt_id", "target", "fallback", "state"}
                or value["schema"] != 1 or value["state"] not in _STATES
                or not isinstance(value["attempt_id"], str)
                or _UUID.fullmatch(value["attempt_id"]) is None
                or not isinstance(value["target"], str)
                or _SHA256.fullmatch(value["target"]) is None
                or (value["fallback"] is not None
                    and (not isinstance(value["fallback"], str)
                         or _SHA256.fullmatch(value["fallback"]) is None))
                or (value["state"] == "rolled_back" and value["fallback"] is None)):
            raise ExecutorError("executor_journal_invalid")
        return value

    def _write_journal(self, value: Mapping[str, object], state: str) -> None:
        write_atomically(self.journal, json.dumps({**value, "state": state},
                                                sort_keys=True).encode(), mode=0o600)

    def _lock(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock.open("a+b")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise ExecutorError("executor_busy") from exc
        return handle

    def _terminal_selection(self) -> SelectedSnapshot:
        """Parse one selected terminal journal while the caller holds a lock."""
        if self.legacy_override.exists() or self.legacy_override.is_symlink():
            raise ExecutorError("legacy_app_unverified")
        row = self._journal()
        if row is None:
            raise ExecutorError("executor_journal_missing")
        if row["state"] not in ("committed", "rolled_back"):
            raise ExecutorError("executor_mutation_unsettled")
        selected = row["target"] if row["state"] == "committed" else row["fallback"]
        if selected is None or self._active() != selected:
            raise ExecutorError("selected_root_unconfirmed")
        return SelectedSnapshot(row["attempt_id"], row["state"], selected)

    def selected_snapshot(self) -> SelectedSnapshot:
        """Read selection under a nonblocking shared lock, then release it.

        Root hashing and PID1 sampling belong outside this brief critical section.
        A caller compares two snapshots to reject a concurrent activation.
        """
        try:
            lock = self.lock.open("rb")
        except FileNotFoundError as exc:
            raise ExecutorError("executor_not_initialized") from exc
        with lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ExecutorError("executor_busy") from exc
            try:
                return self._terminal_selection()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _restore(self, row: dict, *, abi: str) -> None:
        """Complete or roll back one interrupted attempt before a new admission."""
        target, fallback = row["target"], row["fallback"]
        if row["state"] == "recovery_required" and fallback is None:
            raise ExecutorError("cold_attempt_requires_operator")
        try:
            verify_root(self.roots / target, expected_abi=abi)
            target_valid = True
        except PayloadError:
            target_valid = False
            if fallback is None:
                self._write_journal(row, "recovery_required")
                raise ExecutorError("target_and_fallback_unavailable") from None
        if fallback is not None and not (row["state"] == "committed" and target_valid):
            verify_root(self.roots / fallback, expected_abi=abi)
        if (target_valid and row["state"] not in ("rolled_back", "recovery_required")
                and self._active() == target
                and self.service.running_digest() == target):
            self._write_journal(row, "committed")
            return
        if (fallback is not None and row["state"] in (
                "intent_stop", "stopped", "rolled_back", "recovery_required")
                and self._active() == fallback
                and self.service.running_digest() == fallback):
            self._write_journal(row, "rolled_back")
            return
        if self.service.active():
            observed = self.service.running_digest()
            if observed is None:
                raise ExecutorError("player_process_unconfirmed")
            # An earlier sample may have raced PID1's state. Recheck the now
            # identified process before deciding that a stop is necessary.
            if (target_valid and row["state"] not in ("rolled_back", "recovery_required")
                    and self._active() == target and observed == target):
                self._write_journal(row, "committed")
                return
            if (fallback is not None and row["state"] in (
                    "intent_stop", "stopped", "rolled_back", "recovery_required")
                    and self._active() == fallback and observed == fallback):
                self._write_journal(row, "rolled_back")
                return
        # Stop waits for PID1's previous stop/start job and kills any surviving Player.
        self.service.stop()
        chosen = (target if row["state"] == "committed" and target_valid else
                  fallback if fallback is not None else target)
        self._write_active(chosen)
        self._write_journal(row, "activated")
        self._write_journal(row, "start_requested")
        try:
            self.service.start()
        except (OSError, subprocess.SubprocessError, ExecutorError):
            started = False
        else:
            started = self.service.running_digest() == chosen
        if not started and chosen == target and fallback is not None:
            # A once-committed target can fail on a later service restart. The
            # prior exact root is still a valid local repair, with no network.
            verify_root(self.roots / fallback, expected_abi=abi)
            self.service.stop()
            chosen = fallback
            self._write_active(chosen)
            self._write_journal(row, "activated")
            self._write_journal(row, "start_requested")
            try:
                self.service.start()
            except (OSError, subprocess.SubprocessError, ExecutorError):
                started = False
            else:
                started = self.service.running_digest() == chosen
        if not started:
            self._write_journal(row, "recovery_required")
            raise ExecutorError("player_recovery_unconfirmed")
        self._write_journal(row, "committed" if chosen == target else "rolled_back")

    def _recover_locked(self, *, abi: str) -> RecoveryResult | None:
        row = self._journal()
        if row is None:
            return None
        if row["state"] in ("committed", "rolled_back"):
            selected = row["target"] if row["state"] == "committed" else row["fallback"]
            assert selected is not None
            verify_root(self.roots / selected, expected_abi=abi)
            if self._active() == selected and self.service.running_digest() == selected:
                return RecoveryResult(row["attempt_id"], row["state"], selected)
        self._restore(row, abi=abi)
        final = self._journal()
        assert final is not None
        state = final["state"]
        if state not in ("committed", "rolled_back"):
            raise ExecutorError("executor_recovery_incomplete")
        selected = final["target"] if state == "committed" else final["fallback"]
        if (selected is None or self._active() != selected
                or self.service.running_digest() != selected):
            raise ExecutorError("executor_recovery_unconfirmed")
        verify_root(self.roots / selected, expected_abi=abi)
        return RecoveryResult(final["attempt_id"], state, selected)

    def recover(self, *, expected_base_abi: str) -> RecoveryResult | None:
        """Repair the previous local attempt before network fetch or new admission.

        A provisioner can call this at process start, even when Central is unavailable.
        Only the executor's volatile journal, staged roots and actual service are used.
        """
        if (not isinstance(expected_base_abi, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_base_abi) is None):
            raise ExecutorError("base_abi_invalid")
        with self._lock() as locked:
            try:
                return self._recover_locked(abi=expected_base_abi)
            except (PayloadError, OSError, subprocess.SubprocessError) as exc:
                raise ExecutorError(str(exc) if isinstance(exc, PayloadError)
                                    else "executor_effect_failed") from exc
            finally:
                fcntl.flock(locked, fcntl.LOCK_UN)

    def activate(self, payload: bytes, *, sha256: str, size: int, base_abi: str,
                 attempt_id: str, expected_base_abi: str) -> str:
        """Preflight exact target/fallback, then journal, stop, switch and start once."""
        if (_UUID.fullmatch(attempt_id) is None or base_abi != expected_base_abi):
            raise ExecutorError("executor_attempt_or_abi_invalid")
        if self.roots.is_symlink():
            raise ExecutorError("payload_roots_invalid")
        self.roots.mkdir(parents=True, exist_ok=True)
        self.roots.chmod(0o755)
        with self._lock() as locked:
            try:
                if self.legacy_override.exists() or self.legacy_override.is_symlink():
                    raise ExecutorError("legacy_unit_override")
                previous = self._recover_locked(abi=expected_base_abi)
                if previous is not None:
                    if previous.attempt_id == attempt_id:
                        row = self._journal()
                        assert row is not None
                        if row["target"] != sha256:
                            raise ExecutorError("attempt_digest_conflict")
                        selected = previous.active_sha256
                        if (selected is None or self._active() != selected
                                or self.service.running_digest() != selected):
                            raise ExecutorError("attempt_outcome_unconfirmed")
                        verify_root(self.roots / selected, expected_abi=expected_base_abi)
                        return previous.state
                fallback = self._active()
                if self.service.active():
                    running = self.service.running_digest()
                    if running is None:
                        raise ExecutorError("player_process_unconfirmed")
                    if fallback is None or running != fallback:
                        raise ExecutorError("unmanaged_player_active")
                    # A prior refusal may have left a fully published root. The
                    # stage port re-verifies it without extracting or copying the
                    # archive, so retries need only the protected headroom.
                    already_staged = (_SHA256.fullmatch(sha256) is not None
                                      and (self.roots / sha256).exists())
                    self._require_capacity(
                        staging_bytes=0 if already_staged else STAGING_ALLOWANCE_BYTES,
                        archive_copy_bytes=0 if already_staged else size)
                target = stage_payload(payload, sha256=sha256, size=size,
                                       expected_abi=expected_base_abi, roots=self.roots)
                if fallback is not None:
                    verify_root(self.roots / fallback, expected_abi=expected_base_abi)
                if self.service.active():
                    running = self.service.running_digest()
                    if running is None:
                        raise ExecutorError("player_process_unconfirmed")
                    if fallback is None or running != fallback:
                        raise ExecutorError("unmanaged_player_active")
                    self._require_capacity()
                row = {"schema": 1, "attempt_id": attempt_id, "target": target.name,
                       "fallback": fallback}
                self._write_journal(row, "intent_stop")
                self.service.stop()
                self._write_journal(row, "stopped")
                self._write_active(target.name)
                self._write_journal(row, "activated")
                self._write_journal(row, "start_requested")
                try:
                    self.service.start()
                    if self.service.running_digest() != target.name:
                        raise ExecutorError("player_process_unconfirmed")
                except (OSError, subprocess.SubprocessError, ExecutorError) as exc:
                    self.service.stop()
                    if fallback is None:
                        self._write_active(None)
                        self._write_journal(row, "recovery_required")
                        raise ExecutorError("candidate_start_failed") from exc
                    self._write_active(fallback)
                    self._write_journal(row, "activated")
                    self._write_journal(row, "start_requested")
                    try:
                        self.service.start()
                        if self.service.running_digest() != fallback:
                            raise ExecutorError("fallback_process_unconfirmed")
                    except (OSError, subprocess.SubprocessError, ExecutorError) as repair:
                        self._write_journal(row, "recovery_required")
                        raise ExecutorError("fallback_recovery_failed") from repair
                    self._write_journal(row, "rolled_back")
                    return "rolled_back"
                self._write_journal(row, "committed")
                return "committed"
            except (PayloadError, OSError, subprocess.SubprocessError) as exc:
                raise ExecutorError(str(exc) if isinstance(exc, PayloadError)
                                    else "executor_effect_failed") from exc
            finally:
                fcntl.flock(locked, fcntl.LOCK_UN)
