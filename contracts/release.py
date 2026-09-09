"""Small stdlib-only signed release format shared by initramfs and userspace.

Signatures cover canonical manifest bytes, not a reconstructed shell command.
The detached signature is exactly 64 raw Ed25519 bytes. Callers authenticate it
using the deployment public key before trusting the parsed fields.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Mapping

MAX_MANIFEST_BYTES = 8192
MAX_ROOTFS_BYTES = 1024**3
_DIGEST = re.compile(r"[a-f0-9]{64}")


def configuration_digest(files: Mapping[str, bytes]) -> str:
    """Bind the same four common public inputs without recursive derived policy."""
    names = {"public.json", "bootstrap.json", "ca.pem", "release.pub.pem"}
    if set(files) != names or any(not isinstance(value, bytes) or not 0 < len(value) <= 1024**2
                                 for value in files.values()):
        raise ValueError("invalid_public_configuration")
    inventory = [(name, len(files[name]), hashlib.sha256(files[name]).hexdigest())
                 for name in sorted(names)]
    return hashlib.sha256(json.dumps(inventory, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Release:
    revision: str
    boot_abi: str
    configuration_sha256: str
    rootfs_sha256: str
    rootfs_size: int
    schema: int = 1

    def __post_init__(self):
        if (type(self.schema) is not int or self.schema != 1
                or not isinstance(self.revision, str) or not re.fullmatch(r"[a-f0-9]{40}", self.revision)
                or any(not isinstance(value, str) or not _DIGEST.fullmatch(value) for value in (
                    self.boot_abi, self.configuration_sha256, self.rootfs_sha256))
                or type(self.rootfs_size) is not int or not 0 < self.rootfs_size <= MAX_ROOTFS_BYTES):
            raise ValueError("invalid_release")

    def encode(self) -> bytes:
        return (json.dumps(asdict(self), sort_keys=True, separators=(",", ":")) + "\n").encode()

    @property
    def release_id(self) -> str:
        return hashlib.sha256(self.encode()).hexdigest()

    @property
    def rootfs_name(self) -> str:
        return f"rootfs-{self.rootfs_sha256}.squashfs"

    def require_compatible(self, boot_abi: str, configuration_sha256: str):
        if self.boot_abi != boot_abi or self.configuration_sha256 != configuration_sha256:
            raise ValueError("release_incompatible")

    @classmethod
    def decode(cls, payload: bytes) -> Release:
        if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_MANIFEST_BYTES:
            raise ValueError("invalid_release")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate_release_field")
                result[key] = value
            return result

        try:
            value = json.loads(payload, object_pairs_hook=pairs)
            if not isinstance(value, dict):
                raise ValueError
            result = cls(**value)
            if payload != result.encode():
                raise ValueError
            return result
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ValueError("invalid_release") from None


@dataclass(frozen=True)
class BootRequest:
    """One physical boot's retry identity. All fields are generated/read in RAM."""

    device_id: str
    boot_id: str
    request_id: str

    def __post_init__(self):
        if (not isinstance(self.device_id, str)
                or not re.fullmatch(r"device-[a-f0-9]{64}", self.device_id)
                or not isinstance(self.boot_id, str)
                or not re.fullmatch(r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}", self.boot_id)
                or not isinstance(self.request_id, str)
                or not re.fullmatch(r"[a-f0-9]{48}", self.request_id)):
            raise ValueError("invalid_boot_request")

    def encode(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class BootTicket:
    """Central selection; ticket_id is an opaque enrollment/health capability.

    The TLS response binds selection to a device/boot/request. The detached
    signature independently authenticates the immutable release before parsing.
    """

    ticket_id: str
    device_id: str
    boot_id: str
    request_id: str
    release_id: str
    manifest: str
    signature: str
    trial: bool
    schema: int = 2

    def __post_init__(self):
        BootRequest(self.device_id, self.boot_id, self.request_id)
        if (not isinstance(self.ticket_id, str) or not re.fullmatch(r"[a-f0-9]{48}", self.ticket_id)
                or not isinstance(self.release_id, str) or not _DIGEST.fullmatch(self.release_id)
                or type(self.trial) is not bool or type(self.schema) is not int or self.schema != 2
                or not isinstance(self.manifest, str) or not 0 < len(self.manifest.encode()) <= MAX_MANIFEST_BYTES
                or not isinstance(self.signature, str)):
            raise ValueError("invalid_boot_ticket")
        try:
            if len(base64.b64decode(self.signature, validate=True)) != 64:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("invalid_boot_ticket") from None

    def encode(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()

    @classmethod
    def decode(cls, payload: bytes) -> BootTicket:
        if not isinstance(payload, bytes) or not 0 < len(payload) <= 2 * MAX_MANIFEST_BYTES:
            raise ValueError("invalid_boot_ticket")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("invalid_boot_ticket")
                result[key] = value
            return result
        try:
            return cls(**json.loads(payload, object_pairs_hook=pairs))
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ValueError("invalid_boot_ticket") from None
