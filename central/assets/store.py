"""The cache disk: open, check, measure and install asset files. Filesystem only.

The disk decides whether we have a file; the Asset record decides what it is. Every method here
blocks, so async callers run it in a thread. Opens go through `artifact_io.open_regular`
(`O_NOFOLLOW` + `fstat` + regular-file), so a symlink, fifo or device is never served.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from central.artifact_io import HardenedOpenError, open_regular
from central.assets.layout import TEMP_PREFIX, CacheLayout
from central.kernel.assets import AssetKey, AssetReady

_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class OpenedFile:
    fd: int  # the caller owns (closes) it
    size: int


class CacheStore:
    """Tests run it on `tmp_path`."""

    __slots__ = ("_layout",)

    def __init__(self, layout: CacheLayout) -> None:
        self._layout = layout

    @property
    def layout(self) -> CacheLayout:
        return self._layout

    def open(self, key: AssetKey, facts: AssetReady) -> OpenedFile | None:
        """The asset's file opened read-only when it is a regular file of `facts.size`, else None."""
        try:
            fd, metadata = open_regular(self._layout.path(key), expected_size=facts.size)
        except HardenedOpenError:
            return None
        return OpenedFile(fd=fd, size=metadata.st_size)

    def present(self, key: AssetKey, facts: AssetReady) -> bool:
        """`lstat` only: a regular file (not a symlink) whose size matches `facts`."""
        try:
            metadata = os.lstat(self._layout.path(key))
        except (FileNotFoundError, NotADirectoryError):
            return False
        return stat.S_ISREG(metadata.st_mode) and metadata.st_size == facts.size

    def temp_path(self, key: AssetKey) -> Path:
        """A unique, non-existent path in the kind's directory (created if absent).

        The name is random per call, so two attempts never share a temp; the writer still creates
        it `O_EXCL`, so a collision fails loudly instead of sharing bytes.
        """
        directory = self._layout.directory(key.kind)
        directory.mkdir(parents=True, exist_ok=True)
        while True:
            candidate = directory / f"{TEMP_PREFIX}{secrets.token_hex(16)}"
            if not os.path.lexists(candidate):
                return candidate

    def measure(self, path: Path) -> AssetReady:
        """Streaming sha256 + size of a regular file.

        Raises `HardenedOpenError` when `path` is absent or not a regular file, and `ValueError`
        (from `AssetReady`) when it is empty.
        """
        fd, _ = open_regular(path)
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "rb") as source:
            while chunk := source.read(_CHUNK):
                size += len(chunk)
                digest.update(chunk)
        return AssetReady(size=size, sha256=digest.hexdigest())

    def install(self, temp: Path, key: AssetKey) -> None:
        """Atomically rename `temp` onto the asset's path, then fsync the directory."""
        final = self._layout.path(key)
        os.replace(temp, final)
        directory = os.open(final.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def discard(self, path: Path) -> None:
        Path(path).unlink(missing_ok=True)
