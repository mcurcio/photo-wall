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
        """The asset's file opened read-only when it is a regular file of `facts.size`, else None.

        A kind stored by digest is also hash-checked in full before it is returned: a file whose
        bytes do not match `facts` is discarded and answers None (a miss the caller re-makes)."""
        path = self._layout.path(key, facts)
        try:
            fd, metadata = open_regular(path, expected_size=facts.size)
        except HardenedOpenError:
            if key.kind.stored_by_digest:
                self.discard(path)  # absent stays absent; a wrong file at the name goes
            return None
        if key.kind.stored_by_digest and not self._hash_matches(fd, facts):
            os.close(fd)
            self.discard(path)
            return None
        return OpenedFile(fd=fd, size=metadata.st_size)

    @staticmethod
    def _hash_matches(fd: int, facts: AssetReady) -> bool:
        """The whole file's sha256 and size equal `facts`; the offset is rewound after."""
        digest = hashlib.sha256()
        size = 0
        os.lseek(fd, 0, os.SEEK_SET)
        while chunk := os.read(fd, _CHUNK):
            size += len(chunk)
            digest.update(chunk)
        os.lseek(fd, 0, os.SEEK_SET)
        return size == facts.size and digest.hexdigest() == facts.sha256

    def verified(self, key: AssetKey, facts: AssetReady) -> bool:
        """Whether the asset's file holds exactly `facts` (a full hash; a bad file is discarded
        for a kind stored by digest, as `open`)."""
        opened = self.open(key, facts)
        if opened is None:
            return False
        try:
            return key.kind.stored_by_digest or self._hash_matches(opened.fd, facts)
        finally:
            os.close(opened.fd)

    def present(self, key: AssetKey, facts: AssetReady) -> bool:
        """`lstat` only: a regular file (not a symlink) whose size matches `facts`."""
        try:
            metadata = os.lstat(self._layout.path(key, facts))
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
        self._sync_directory(final)

    def install_by_digest(self, temp: Path, key: AssetKey, facts: AssetReady) -> None:
        """Place `temp` (measured as `facts`) at the digest-named path without ever overwriting
        a path: a file already there that holds `facts` is the same bytes, so `temp` is dropped;
        one that does not is corrupt and is removed first. `temp` is gone afterwards."""
        final = self._layout.path(key, facts)
        try:
            os.link(temp, final)
        except FileExistsError:
            if not self.verified(key, facts):  # removed it: link into the freed name
                os.link(temp, final)
        finally:
            self.discard(temp)
        self._sync_directory(final)

    @staticmethod
    def _sync_directory(final: Path) -> None:
        directory = os.open(final.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def discard(self, path: Path) -> None:
        Path(path).unlink(missing_ok=True)
