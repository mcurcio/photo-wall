"""Private appliance identity; failure never silently replaces an existing key."""

from __future__ import annotations

import base64
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from contracts.enrollment import Enrollment, OutputReport, Persistence, enrollment_message


@dataclass(frozen=True)
class Identity:
    _key: Ed25519PrivateKey = field(repr=False)
    persistence: Persistence = "durable"
    fault: str | None = None

    @property
    def public_key(self) -> str:
        return self._key.public_key().public_bytes_raw().hex()

    def enrollment(self, nonce: str, outputs: tuple[OutputReport, ...]) -> Enrollment:
        signature = self._key.sign(enrollment_message(nonce, outputs, self.persistence))
        return Enrollment(public_key=self.public_key, nonce=nonce,
                          signature=base64.b64encode(signature).decode(), outputs=outputs,
                          persistence=self.persistence)


def _private(info: os.stat_result, directory: bool = False) -> bool:
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    return (kind(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600))


def _read_key(directory: int) -> Ed25519PrivateKey:
    fd = os.open("identity.key", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        if not _private(os.fstat(fd)):
            raise ValueError("identity_permissions")
        data = os.read(fd, 33)
        if len(data) != 32:
            raise ValueError("identity_format")
        return Ed25519PrivateKey.from_private_bytes(data)
    finally:
        os.close(fd)


def _probe_storage(directory: int):
    name = ".identity-probe-" + secrets.token_hex(12)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory)
    try:
        if os.write(fd, b"1") != 1:
            raise OSError("short storage probe")
        os.fsync(fd)
    finally:
        os.close(fd)
        os.unlink(name, dir_fd=directory)
    os.fsync(directory)


def load_identity(state_dir: Path) -> Identity:
    """Use an existing owned 0700 volume, otherwise one volatile key per caller.

    The exclusive hard-link publish cannot replace a concurrent creator's key.
    A corrupt, unsafe or inaccessible existing key remains untouched for repair.
    """
    directory = None
    temporary = None
    key = None
    try:
        directory = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if not _private(os.fstat(directory), directory=True):
            raise ValueError("state_permissions")
        try:
            key = _read_key(directory)
        except FileNotFoundError:
            key = Ed25519PrivateKey.generate()
            temporary = ".identity-" + secrets.token_hex(12)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            try:
                os.fchmod(fd, 0o600)
                data = key.private_bytes_raw()
                if os.write(fd, data) != len(data):
                    raise OSError("short identity write")
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(temporary, "identity.key", src_dir_fd=directory,
                        dst_dir_fd=directory, follow_symlinks=False)
                os.fsync(directory)
            except FileExistsError:
                key = _read_key(directory)
        _probe_storage(directory)
        return Identity(key)
    except (OSError, ValueError):
        return Identity(key or Ed25519PrivateKey.generate(), "volatile", "identity_storage")
    finally:
        if directory is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
            os.close(directory)
