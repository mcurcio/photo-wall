"""Exact V2 sealed Debian environment identity, distinct from data-only V1.

A reference describes release bytes, not verification or permission to launch.
CI owns closure resolution; the base never resolves packages or mutable tags.
"""
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from contracts.node_protocol import counter, digest, token


@dataclass(frozen=True, slots=True)
class AppEnvironmentRefV2:
    environment_sha256: str
    size_bytes: int
    deb_sha256: str
    deb_name: str
    deb_version: str
    architecture: str
    dependency_lock_sha256: str
    source_snapshot_sha256: str
    entry_point: str
    base_abi: str
    graphics_abi: str
    plugin_abi: str

    def __post_init__(self) -> None:
        for value in (self.environment_sha256, self.deb_sha256,
                      self.dependency_lock_sha256, self.source_snapshot_sha256):
            digest(value)
        counter(self.size_bytes, 1)
        for value in (self.deb_name, self.architecture, self.base_abi,
                      self.graphics_abi, self.plugin_abi):
            token(value)
        # Debian versions include epoch ':', upstream '+', and revision '~'.
        if not isinstance(self.deb_version, str) or not re.fullmatch(
            r"[0-9][A-Za-z0-9.+:~\-]{0,127}", self.deb_version
        ):
            raise ValueError("invalid_debian_version")
        if not isinstance(self.entry_point, str) or len(self.entry_point) > 256:
            raise ValueError("invalid_environment_entry_point")
        path = PurePosixPath(self.entry_point)
        if (not path.is_absolute() or '..' in path.parts or len(path.parts) < 2
                or str(path) != self.entry_point or self.entry_point.startswith('//')):
            raise ValueError("invalid_environment_entry_point")
        if any(ord(char) < 33 or ord(char) > 126 for char in self.entry_point):
            raise ValueError("invalid_environment_entry_point")
