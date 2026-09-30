"""Inert, base-owned OS command inbox for a future authenticated T1/T2 channel.

There is deliberately no transport, service entry point, or executor call here.
The observational OS agent and the Player application cannot feed this inbox.
Construction of a ``VerifiedLocalSession`` is not authentication: a future
protected verifier must supply it before this module is composed in production.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from appliance.boot_offer import BOOT_HANDOFF, BootOfferError, read_current_handoff
from contracts.os_command import (
    MAX_COMMAND_BYTES,
    ActivateAppCommand,
    LocalCommandContext,
    OsCommandError,
    parse_activate_app_command,
    require_current_command,
)
from contracts.strict_json import loads_object
from uplink.files import write_atomically

KERNEL_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
COMMAND_STATE_DIR = Path("/run/photo-wall/os-command")
MAX_JOURNAL_BYTES = MAX_COMMAND_BYTES + 1024
_DEVICE = re.compile(r"device-[0-9a-f]{64}")
_ABI = re.compile(r"sha256:[0-9a-f]{64}")
_AUDIENCE = re.compile(r"[A-Za-z0-9:/._-]{1,256}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class CommandInboxError(ValueError):
    """Local command receipt was refused without permitting an app effect."""


@dataclass(frozen=True, slots=True)
class VerifiedLocalSession:
    """Facts supplied by a future protected verifier, never a T0 boot offer.

    The credential and trusted transport stay outside this value. A verifier
    must bind them to these facts and hold them in root-only OS state.
    """

    installation_audience: str
    origin: str
    trust_mode: str
    device_id: str
    device_generation: int
    kernel_boot_id: UUID
    offer_id: UUID
    command_session_id: UUID
    base_abi: str
    expires_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.origin, str):
            raise CommandInboxError("os_command_session_unqualified")
        try:
            parsed = urlsplit(self.origin)
            parsed.port  # A malformed port must not be accepted as an origin.
        except ValueError as exc:
            raise CommandInboxError("os_command_session_unqualified") from exc
        if (self.trust_mode not in ("t1", "t2")
                or not isinstance(self.installation_audience, str)
                or _AUDIENCE.fullmatch(self.installation_audience) is None
                or self.installation_audience == "photo-wall-central-t0"
                or not isinstance(self.device_id, str)
                or _DEVICE.fullmatch(self.device_id) is None
                or type(self.device_generation) is not int
                or not 1 <= self.device_generation <= 2**63 - 1
                or not all(isinstance(value, UUID) for value in (
                    self.kernel_boot_id, self.offer_id, self.command_session_id))
                or not isinstance(self.base_abi, str)
                or _ABI.fullmatch(self.base_abi) is None
                or type(self.expires_at) not in (int, float)
                or not math.isfinite(self.expires_at)
                or parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise CommandInboxError("os_command_session_unqualified")


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    command: ActivateAppCommand
    command_sha256: str
    duplicate: bool


class CommandInbox:
    """Validate and journal one immutable attempt; never download or stop an app."""

    def __init__(self, *, state_dir: Path = COMMAND_STATE_DIR,
                 boot_id_path: Path = KERNEL_BOOT_ID,
                 handoff_path: Path = BOOT_HANDOFF,
                 required_uid: int = 0) -> None:
        self.state_dir = state_dir
        self.boot_id_path = boot_id_path
        self.handoff_path = handoff_path
        self.required_uid = required_uid

    def _current_boot(self, session: VerifiedLocalSession) -> None:
        try:
            boot_text = self.boot_id_path.read_text().strip()
            boot_id = UUID(boot_text)
            handoff_stat = self.handoff_path.lstat()
            # On the deployed /etc/photo-wall path these three root-owned
            # directories prevent the unprivileged Player from swapping the
            # handoff or a parent for a symlink between lstat and parsing.
            handoff_parents = tuple(parent.lstat()
                                    for parent in list(self.handoff_path.parents)[:3])
        except (OSError, ValueError) as exc:
            raise CommandInboxError("os_command_boot_unqualified") from exc
        if (str(boot_id) != boot_text or boot_id != session.kernel_boot_id
                or not stat.S_ISREG(handoff_stat.st_mode)
                or handoff_stat.st_uid != self.required_uid
                or stat.S_IMODE(handoff_stat.st_mode) & 0o077
                or any(not stat.S_ISDIR(parent.st_mode)
                       or parent.st_uid != self.required_uid
                       or stat.S_IMODE(parent.st_mode) & 0o022
                       for parent in handoff_parents)):
            raise CommandInboxError("os_command_boot_unqualified")
        try:
            handoff = read_current_handoff(self.handoff_path, boot_text)
        except (BootOfferError, OSError) as exc:
            raise CommandInboxError("os_command_boot_unqualified") from exc
        if (handoff is None or handoff.get("schema") != 2
                or handoff.get("mode") != "offer"
                or handoff.get("offer_id") != str(session.offer_id)):
            raise CommandInboxError("os_command_boot_unqualified")

    def _state_lock(self):
        if os.geteuid() != self.required_uid:
            raise CommandInboxError("os_command_root_required")
        try:
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = self.state_dir.lstat()
            if (not stat.S_ISDIR(directory.st_mode)
                    or directory.st_uid != self.required_uid
                    or stat.S_IMODE(directory.st_mode) & 0o077):
                raise CommandInboxError("os_command_state_untrusted")
            fd = os.open(self.state_dir / "receipt.lock",
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            locked = os.fdopen(fd, "r+b")
            lock_stat = os.fstat(locked.fileno())
            if (not stat.S_ISREG(lock_stat.st_mode)
                    or lock_stat.st_uid != self.required_uid
                    or stat.S_IMODE(lock_stat.st_mode) & 0o077):
                locked.close()
                raise CommandInboxError("os_command_state_untrusted")
            try:
                fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                locked.close()
                raise CommandInboxError("os_command_busy") from exc
            return locked
        except OSError as exc:
            raise CommandInboxError("os_command_state_unavailable") from exc

    def _read(self) -> tuple[ActivateAppCommand, str] | None:
        path = self.state_dir / "receipt.json"
        try:
            current = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CommandInboxError("os_command_state_unavailable") from exc
        if (not stat.S_ISREG(current.st_mode)
                or current.st_uid != self.required_uid
                or stat.S_IMODE(current.st_mode) & 0o077
                or current.st_size > MAX_JOURNAL_BYTES):
            raise CommandInboxError("os_command_state_untrusted")
        try:
            value = loads_object(path.read_bytes(), max_bytes=MAX_JOURNAL_BYTES)
            if (value is None or set(value) != {"schema", "phase", "command", "sha256"}
                    or value["schema"] != 1 or value["phase"] != "received"
                    or not isinstance(value["sha256"], str)
                    or _DIGEST.fullmatch(value["sha256"]) is None
                    or not isinstance(value["command"], dict)):
                raise CommandInboxError("os_command_journal_invalid")
            encoded = json.dumps(value["command"], sort_keys=True,
                                 separators=(",", ":")).encode()
            if hashlib.sha256(encoded).hexdigest() != value["sha256"]:
                raise CommandInboxError("os_command_journal_invalid")
            command = parse_activate_app_command(encoded)
            return command, value["sha256"]
        except (OSError, OsCommandError, TypeError, ValueError) as exc:
            raise CommandInboxError("os_command_journal_invalid") from exc

    def receive(self, raw: bytes, session: VerifiedLocalSession | None, *,
                now_utc: float) -> CommandReceipt:
        """Record receipt before any future fetch; duplicates never start an effect.

        The same-boot pending attempt blocks all later commands until a future
        effect coordinator has reconciled it. Journal damage is never reset here.
        """
        if not isinstance(session, VerifiedLocalSession):
            raise CommandInboxError("os_command_session_unqualified")
        if type(raw) is not bytes:
            raise CommandInboxError("os_command_schema_invalid")
        self._current_boot(session)
        command = parse_activate_app_command(raw)
        require_current_command(command, LocalCommandContext(
            session.installation_audience, session.device_id, session.device_generation,
            session.kernel_boot_id, session.offer_id, session.command_session_id,
            session.base_abi, now_utc, session.expires_at))
        document = loads_object(raw, max_bytes=MAX_COMMAND_BYTES)
        assert document is not None  # The strict parser has already accepted these bytes.
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        with self._state_lock() as locked:
            try:
                previous = self._read()
                if previous is not None:
                    old, old_digest = previous
                    if old.kernel_boot_id != command.kernel_boot_id:
                        raise CommandInboxError("os_command_journal_boot_conflict")
                    if old == command:
                        return CommandReceipt(command, old_digest, True)
                    if command.desired_revision <= old.desired_revision:
                        raise CommandInboxError("os_command_replay_or_conflict")
                    raise CommandInboxError("os_command_pending_attempt")
                body = json.dumps({"schema": 1, "phase": "received", "command": document,
                                   "sha256": digest}, sort_keys=True,
                                  separators=(",", ":")).encode()
                if len(body) > MAX_JOURNAL_BYTES:
                    raise CommandInboxError("os_command_journal_limit")
                try:
                    write_atomically(self.state_dir / "receipt.json", body, mode=0o600)
                except OSError as exc:
                    raise CommandInboxError("os_command_state_unavailable") from exc
                return CommandReceipt(command, digest, False)
            finally:
                fcntl.flock(locked, fcntl.LOCK_UN)
