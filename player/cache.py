"""A bounded disposable cache for exact Player media variants.

The filesystem is never an authority source. Every process builds a fresh in-memory
index, every candidate is checked against its content-addressed name, and a requested
variant is returned only after its exact size and SHA-256 digest have been verified.
Pins and LRU metadata deliberately disappear with the process.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from contracts.models import Variant

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_MAX_CHUNK = 1024 * 1024


class CacheError(ValueError):
    """The cache could not accept or validate a requested variant."""


class CacheCapacityError(CacheError):
    """The request cannot fit without evicting a pinned entry."""


@dataclass
class _Entry:
    size: int
    accessed: float


class Cache:
    """A single-process cache with optional reusable, but untrusted, files.

    Omitting ``directory`` gives the cache a process-owned temporary directory. An
    explicit directory may survive a process restart, but that does not make it
    persistent state: files are discovered and content-verified again, while all
    ownership and readiness metadata starts empty.
    """

    def __init__(self, directory: Path | None, max_bytes: int):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self._temporary_directory = (
            tempfile.TemporaryDirectory(prefix="photo-wall-cache-")
            if directory is None
            else None
        )
        selected = self._temporary_directory.name if self._temporary_directory else directory
        self.directory = Path(selected).expanduser().resolve()
        self.max_bytes = max_bytes
        self._lock = RLock()
        self._closed = False
        self._entries: dict[str, _Entry] = {}
        self._pins: dict[str, set[str]] = {}
        self._temporary_bytes = 0
        self._acquisition_active = False
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._recover()

    def _ensure_open(self) -> None:
        if self._closed:
            raise CacheError("cache is closed")

    @staticmethod
    def _validate_digest(sha256: str) -> str:
        if not isinstance(sha256, str) or not _DIGEST.fullmatch(sha256):
            raise CacheError("invalid SHA-256 digest")
        return sha256

    @staticmethod
    def _validate_owner(owner: str) -> str:
        if not isinstance(owner, str) or not owner or len(owner) > 512:
            raise CacheError("owner must be a nonempty string of at most 512 characters")
        return owner

    def _blob_path(self, sha256: str) -> Path:
        return self.directory / f"{sha256}.blob"

    def _verify_file(self, path: Path, sha256: str, size: int | None = None) -> int | None:
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            initial = os.fstat(descriptor)
            if not stat.S_ISREG(initial.st_mode):
                return None
            if initial.st_size <= 0 or (size is not None and initial.st_size != size):
                return None
            digest = hashlib.sha256()
            count = 0
            while chunk := os.read(descriptor, _MAX_CHUNK):
                count += len(chunk)
                if size is not None and count > size:
                    return None
                digest.update(chunk)
            final = os.fstat(descriptor)
            if (
                count != initial.st_size
                or final.st_size != initial.st_size
                or digest.hexdigest() != sha256
            ):
                return None
            return count
        except OSError:
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def _unlink(self, sha256: str) -> None:
        path = self._blob_path(sha256)
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise CacheError(f"cannot remove cache file {path.name}") from error
        self._entries.pop(sha256, None)
        for owner in tuple(self._pins):
            self._pins[owner].discard(sha256)
            if not self._pins[owner]:
                del self._pins[owner]

    def _recover(self) -> None:
        """Rebuild an in-memory index only from independently verified bytes."""
        self._ensure_open()
        for partial in self.directory.glob(".partial-*.tmp"):
            try:
                if partial.is_file() or partial.is_symlink():
                    partial.unlink()
                elif partial.exists():
                    raise CacheError(f"stale temporary is not a file: {partial.name}")
            except OSError as error:
                raise CacheError(f"cannot remove stale temporary {partial.name}") from error
        self._entries.clear()
        self._pins.clear()
        for blob in self.directory.glob("*.blob"):
            sha256 = blob.name[:-5]
            if not _DIGEST.fullmatch(sha256):
                continue
            size = self._verify_file(blob, sha256)
            if size is None:
                self._unlink(sha256)
                continue
            self._entries[sha256] = _Entry(size=size, accessed=time.monotonic())
        self._trim_unpinned()

    def _pinned(self, sha256: str) -> bool:
        return any(sha256 in digests for digests in self._pins.values())

    def _indexed_bytes(self) -> int:
        return sum(entry.size for entry in self._entries.values())

    def _trim_unpinned(self) -> None:
        while self._indexed_bytes() > self.max_bytes:
            candidates = (
                (entry.accessed, sha256)
                for sha256, entry in self._entries.items()
                if not self._pinned(sha256)
            )
            try:
                _, sha256 = min(candidates)
            except ValueError:
                return
            self._unlink(sha256)

    def _evict_for(self, required: int) -> None:
        while self._indexed_bytes() + self._temporary_bytes + required > self.max_bytes:
            candidates = (
                (entry.accessed, sha256)
                for sha256, entry in self._entries.items()
                if not self._pinned(sha256)
            )
            try:
                _, sha256 = min(candidates)
            except ValueError as error:
                raise CacheCapacityError("cache capacity is protected by pins") from error
            self._unlink(sha256)

    def _lookup_valid(self, variant: Variant) -> Path | None:
        sha256 = self._validate_digest(variant.sha256)
        path = self._blob_path(sha256)
        entry = self._entries.get(sha256)
        # A canonical file can appear after startup. It is still eligible only after
        # exact validation against the requested variant.
        if entry is None and (path.is_file() or path.is_symlink()):
            size = self._verify_file(path, sha256, variant.size)
            if size is not None:
                entry = self._entries[sha256] = _Entry(size, time.monotonic())
        if entry is None:
            return None
        if entry.size != variant.size or self._verify_file(path, sha256, variant.size) is None:
            self._unlink(sha256)
            return None
        entry.accessed = time.monotonic()
        return path

    def secure(self, variant: Variant, chunks: Iterable[bytes], pin: str) -> Path:
        """Store complete exact bytes for ``variant`` and pin them for ``pin``."""
        if not isinstance(variant, Variant):
            raise TypeError("variant must be a contracts.models.Variant")
        owner = self._validate_owner(pin)
        sha256 = self._validate_digest(variant.sha256)
        if variant.size > self.max_bytes:
            raise CacheCapacityError("variant is larger than cache capacity")
        with self._lock:
            self._ensure_open()
            existing = self._lookup_valid(variant)
            if existing is not None:
                self._pins.setdefault(owner, set()).add(sha256)
                return existing
            self._evict_for(variant.size)
            path = self._blob_path(sha256)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".partial-", suffix=".tmp", dir=self.directory
            )
            temporary = Path(temporary_name)
            count = 0
            self._temporary_bytes = 0
            self._acquisition_active = True
            try:
                digest = hashlib.sha256()
                with os.fdopen(descriptor, "wb") as stream:
                    for chunk in chunks:
                        if not isinstance(chunk, bytes):
                            raise CacheError("byte iterator yielded a non-bytes value")
                        if count + len(chunk) > variant.size:
                            raise CacheError("byte stream exceeds declared variant size")
                        stream.write(chunk)
                        digest.update(chunk)
                        count += len(chunk)
                        self._temporary_bytes = count
                    stream.flush()
                if count != variant.size:
                    raise CacheError("byte stream is shorter than declared variant size")
                if digest.hexdigest() != sha256:
                    raise CacheError("byte stream SHA-256 does not match variant")
                os.replace(temporary, path)
                self._fsync_directory(self.directory)
                self._entries[sha256] = _Entry(variant.size, time.monotonic())
                self._pins.setdefault(owner, set()).add(sha256)
                return path
            finally:
                self._temporary_bytes = 0
                self._acquisition_active = False
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def path_for(self, variant: Variant) -> Path | None:
        """Return a freshly verified ready path, or repair corruption as a miss."""
        if not isinstance(variant, Variant):
            raise TypeError("variant must be a contracts.models.Variant")
        with self._lock:
            self._ensure_open()
            return self._lookup_valid(variant)

    def pin(self, sha256: str, owner: str) -> None:
        """Pin verified bytes for this process; pins never survive restart."""
        sha256 = self._validate_digest(sha256)
        owner = self._validate_owner(owner)
        with self._lock:
            self._ensure_open()
            entry = self._entries.get(sha256)
            if (
                entry is None
                or self._verify_file(self._blob_path(sha256), sha256, entry.size) is None
            ):
                if entry is not None:
                    self._unlink(sha256)
                raise CacheError("cannot pin a missing or corrupt variant")
            entry.accessed = time.monotonic()
            self._pins.setdefault(owner, set()).add(sha256)

    def release(self, owner: str) -> None:
        owner = self._validate_owner(owner)
        with self._lock:
            self._ensure_open()
            self._pins.pop(owner, None)

    def stats(self) -> dict[str, int]:
        with self._lock:
            self._ensure_open()
            pinned = {sha256 for digests in self._pins.values() for sha256 in digests}
            return {
                "bytes": self._indexed_bytes(),
                "entries": len(self._entries),
                "pinned_bytes": sum(
                    self._entries[item].size for item in pinned if item in self._entries
                ),
                "pinned_entries": sum(item in self._entries for item in pinned),
                "temporary_bytes": self._temporary_bytes,
                "max_bytes": self.max_bytes,
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._entries.clear()
            self._pins.clear()
            self._closed = True
            if self._temporary_directory is not None:
                self._temporary_directory.cleanup()


__all__ = ["Cache", "CacheCapacityError", "CacheError"]
