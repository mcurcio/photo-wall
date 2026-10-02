"""Protected, single-writer boot-scoped JSON storage (no domain imports)."""
from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path
from uuid import UUID, uuid4

from contracts.strict_json import loads_object


class BootStore:
    """Hold a lifetime flock; refuse stale boots, insecure paths and corrupt state.

    A failed write poisons this instance: effects must never follow uncertain storage.
    The parent directory must already be provisioned by the base, owned by owner_uid.
    """

    def __init__(self, directory: Path, *, boot_id: UUID, policy: dict,
                 owner_uid: int = 0, max_bytes: int = 4 * 1024 * 1024):
        if not directory.is_absolute() or directory.is_symlink():
            raise ValueError("journal_path_invalid")
        self.directory = directory
        self.owner_uid = owner_uid
        self.max_bytes = max_bytes
        self.failed = False
        self._check(directory, directory=True)
        self.fd = os.open(directory / "writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                          0o600)
        try:
            self._check_fd(self.fd)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.binding = {"schema": 2, "boot_id": str(boot_id), "policy": policy}
            binding = self.read("binding")
            if binding is None:
                self.write("binding", self.binding)
            elif binding != self.binding:
                raise ValueError("journal_boot_or_policy_mismatch")
        except BaseException:
            os.close(self.fd)
            self.fd = -1
            raise

    def _check(self, path: Path, *, directory: bool = False) -> None:
        info = path.lstat()
        if (info.st_uid != self.owner_uid or info.st_mode & 0o022
                or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
            raise ValueError("journal_ownership_or_mode")

    def _check_fd(self, fd: int) -> None:
        info = os.fstat(fd)
        if (info.st_uid != self.owner_uid or info.st_mode & 0o077
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ValueError("journal_file_ownership_or_mode")

    @staticmethod
    def _name(name: str) -> str:
        if not name or len(name) > 80 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in name):
            raise ValueError("journal_name_invalid")
        return name + ".json"

    def read(self, name: str) -> dict | None:
        if self.failed or self.fd < 0:
            raise ValueError("journal_poisoned")
        try:
            fd = os.open(self.directory / self._name(name), os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            self._check_fd(stream.fileno())
            value = loads_object(stream.read(self.max_bytes + 1), max_bytes=self.max_bytes)
        if value is None:
            raise ValueError("journal_corrupt")
        return value

    def write(self, name: str, value: dict) -> None:
        if self.failed or self.fd < 0:
            raise ValueError("journal_poisoned")
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > self.max_bytes:
            raise ValueError("journal_capacity")
        temporary = self.directory / (".write-" + uuid4().hex)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / self._name(name))
            parent = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except BaseException:
            self.failed = True
            temporary.unlink(missing_ok=True)
            raise

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
