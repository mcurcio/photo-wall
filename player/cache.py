"""A bounded disposable cache for Player media variants, kept by (key, digest).

A variant is named by its inputs (`Variant.key`, `<asset_id>.<recipe_id>`), and Central may
re-make it and serve other bytes of the same content. So the cache keeps a variant's bytes under
its name and the digest Central served them with (`Repr-Digest`), checked against that digest
and size (`Content-Length`) as they arrive, never against the plan's. Any whole file held under
a variant's name is that variant.

The filesystem is never an authority source. Every process builds a fresh in-memory index, and
every candidate is checked against the digest in its file name before it is returned. Pins and
LRU metadata deliberately disappear with the process.
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
# `<SHA-256 of the variant's key>.<SHA-256 of the bytes>.blob`: a key can be longer than a file
# name may be, so the file name carries its hash.
_BLOB = re.compile(r"^([a-f0-9]{64})\.([a-f0-9]{64})\.blob$")
_MAX_CHUNK = 1024 * 1024

# One cached file: (the hash of its variant's key, the digest of its bytes).
_Name = tuple[str, str]


class CacheError(ValueError):
    """The cache could not accept or validate a requested variant."""


class CacheCapacityError(CacheError):
    """The request cannot fit without evicting a pinned entry."""


@dataclass
class _Entry:
    size: int
    accessed: float


def _key_of(variant: Variant) -> str:
    if not isinstance(variant, Variant):
        raise TypeError("variant must be a contracts.models.Variant")
    return hashlib.sha256(variant.key.encode()).hexdigest()


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
        self._entries: dict[_Name, _Entry] = {}
        # Each owner's pinned variants, by the hash of their key: a pin keeps every whole file
        # held under that variant's name.
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

    def _blob_path(self, name: _Name) -> Path:
        return self.directory / f"{name[0]}.{name[1]}.blob"

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

    def _unlink(self, name: _Name) -> None:
        path = self._blob_path(name)
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise CacheError(f"cannot remove cache file {path.name}") from error
        self._entries.pop(name, None)

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
            match = _BLOB.fullmatch(blob.name)
            if match is not None:
                self._index((match.group(1), match.group(2)))
        self._trim_unpinned()

    def _index(self, name: _Name) -> None:
        """Index a file found on disk once its bytes match the digest in its name; remove it
        otherwise (a symlink is never followed)."""
        size = self._verify_file(self._blob_path(name), name[1])
        if size is None:
            self._unlink(name)
        else:
            self._entries[name] = _Entry(size=size, accessed=time.monotonic())

    def _pinned(self, name: _Name) -> bool:
        return any(name[0] in keys for keys in self._pins.values())

    def _indexed_bytes(self) -> int:
        return sum(entry.size for entry in self._entries.values())

    def _least_recent_unpinned(self) -> _Name | None:
        least = min(((entry.accessed, name) for name, entry in self._entries.items()
                     if not self._pinned(name)), default=None)
        return None if least is None else least[1]

    def _trim_unpinned(self) -> None:
        while self._indexed_bytes() > self.max_bytes:
            name = self._least_recent_unpinned()
            if name is None:
                return
            self._unlink(name)

    def _evict_for(self, required: int) -> None:
        while self._indexed_bytes() + self._temporary_bytes + required > self.max_bytes:
            name = self._least_recent_unpinned()
            if name is None:
                raise CacheCapacityError("cache capacity is protected by pins")
            self._unlink(name)

    def _lookup_valid(self, variant: Variant) -> Path | None:
        """A whole file held under ``variant``'s name (whichever digest it was served with),
        most recently used first; a damaged one is removed as a miss."""
        key = _key_of(variant)
        # A file can appear after startup. It is still eligible only after verification.
        for blob in self.directory.glob(f"{key}.*.blob"):
            match = _BLOB.fullmatch(blob.name)
            if match is not None and (key, match.group(2)) not in self._entries:
                self._index((key, match.group(2)))
        held = sorted(((entry.accessed, name) for name, entry in self._entries.items()
                       if name[0] == key), reverse=True)
        for _, name in held:
            entry = self._entries[name]
            path = self._blob_path(name)
            if self._verify_file(path, name[1], entry.size) is None:
                self._unlink(name)
                continue
            entry.accessed = time.monotonic()
            return path
        return None

    def secure(self, variant: Variant, chunks: Iterable[bytes], pin: str, *, sha256: str,
               size: int) -> Path:
        """Store the bytes Central served for ``variant`` and pin them for ``pin``: exactly
        ``size`` bytes whose SHA-256 is ``sha256`` (the response's `Content-Length` and
        `Repr-Digest`). A whole file of the variant already held is used instead."""
        owner = self._validate_owner(pin)
        key = _key_of(variant)
        sha256 = self._validate_digest(sha256)
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise CacheError("served size must be a positive integer")
        if size > self.max_bytes:
            raise CacheCapacityError("variant is larger than cache capacity")
        with self._lock:
            self._ensure_open()
            existing = self._lookup_valid(variant)
            if existing is not None:
                self._pins.setdefault(owner, set()).add(key)
                return existing
            self._evict_for(size)
            name = (key, sha256)
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
                        if count + len(chunk) > size:
                            raise CacheError("byte stream exceeds the served size")
                        stream.write(chunk)
                        digest.update(chunk)
                        count += len(chunk)
                        self._temporary_bytes = count
                    stream.flush()
                if count != size:
                    raise CacheError("byte stream is shorter than the served size")
                if digest.hexdigest() != sha256:
                    raise CacheError("byte stream SHA-256 does not match the served digest")
                path = self._blob_path(name)
                os.replace(temporary, path)
                self._fsync_directory(self.directory)
                self._entries[name] = _Entry(size, time.monotonic())
                self._pins.setdefault(owner, set()).add(key)
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
        with self._lock:
            self._ensure_open()
            return self._lookup_valid(variant)

    def pin(self, variant: Variant, owner: str) -> None:
        """Pin a verified whole file of ``variant`` for this process; pins never survive
        restart."""
        owner = self._validate_owner(owner)
        with self._lock:
            self._ensure_open()
            if self._lookup_valid(variant) is None:
                raise CacheError("cannot pin a missing or corrupt variant")
            self._pins.setdefault(owner, set()).add(_key_of(variant))

    def release(self, owner: str) -> None:
        owner = self._validate_owner(owner)
        with self._lock:
            self._ensure_open()
            self._pins.pop(owner, None)

    def stats(self) -> dict[str, int]:
        with self._lock:
            self._ensure_open()
            pinned = [entry for name, entry in self._entries.items() if self._pinned(name)]
            return {
                "bytes": self._indexed_bytes(),
                "entries": len(self._entries),
                "pinned_bytes": sum(entry.size for entry in pinned),
                "pinned_entries": len(pinned),
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
