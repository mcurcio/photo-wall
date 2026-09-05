"""A bounded, restart-safe cache for exact Player media variants.

The cache deliberately knows nothing about where bytes came from.  Callers supply a
validated :class:`contracts.models.Variant` and an iterator of bytes; readiness is
reported only after the complete byte stream is durable and indexed.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from threading import RLock

from contracts.models import Variant

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_MAX_CHUNK = 1024 * 1024


class CacheError(ValueError):
    """The cache could not accept or validate a requested variant."""


class CacheCapacityError(CacheError):
    """The request cannot fit without evicting a pinned entry."""


class Cache:
    """A single-process bounded byte cache.

    All public operations use one re-entrant lock.  This is intentional: Player
    download workers may call the cache concurrently, and serializing publication,
    eviction, and pin changes makes the aggregate byte bound straightforward.
    """

    def __init__(self, directory: Path, max_bytes: int):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self.directory = Path(directory).expanduser().resolve()
        self.max_bytes = max_bytes
        self._lock = RLock()
        self._closed = False
        self._temporary_bytes = 0
        self._acquisition_active = False
        self.directory.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(
            self.directory / "cache.sqlite3",
            isolation_level=None,
            check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._configure_database()
            self._create_schema()
            self._recover()

    def _configure_database(self) -> None:
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=5000")

    def _create_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS blobs (
                sha256 TEXT PRIMARY KEY,
                size INTEGER NOT NULL CHECK (size > 0),
                filename TEXT NOT NULL,
                accessed REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pins (
                sha256 TEXT NOT NULL REFERENCES blobs(sha256) ON DELETE CASCADE,
                owner TEXT NOT NULL,
                PRIMARY KEY (sha256, owner)
            );
            CREATE INDEX IF NOT EXISTS blobs_lru ON blobs(accessed, sha256);
            CREATE INDEX IF NOT EXISTS pins_owner ON pins(owner);
            """
        )

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
        # sha256 has been checked against the strict digest grammar by every caller.
        return self.directory / f"{sha256}.blob"

    @staticmethod
    def _is_regular(path: Path) -> bool:
        try:
            return not path.is_symlink() and path.is_file()
        except OSError:
            return False

    def _verify_file(self, path: Path, sha256: str, size: int) -> bool:
        if size <= 0 or not self._is_regular(path):
            return False
        try:
            if path.stat().st_size != size:
                return False
            digest = hashlib.sha256()
            count = 0
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(_MAX_CHUNK)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > size:
                        return False
                    digest.update(chunk)
            return count == size and digest.hexdigest() == sha256 and path.stat().st_size == size
        except (OSError, ValueError):
            return False

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _unlink_blob(self, sha256: str) -> None:
        path = self._blob_path(sha256)
        try:
            # unlinking a symlink removes the link itself and never follows it.
            path.unlink(missing_ok=True)
        except OSError as exc:
            # Keep the index and its accounting intact when the owned file cannot be
            # removed.  Discounting an undeleted file could violate the byte bound.
            raise CacheError(f"cannot remove cache file {path.name}") from exc

    def _delete_row(self, sha256: str) -> None:
        self._unlink_blob(sha256)
        self._db.execute("DELETE FROM blobs WHERE sha256 = ?", (sha256,))

    def _delete_invalid_row(self, sha256: str) -> None:
        """Delete malformed index metadata without interpreting its digest as a path."""
        self._db.execute("DELETE FROM blobs WHERE sha256 = ?", (sha256,))

    def _recover(self) -> None:
        """Repair interrupted publications and invalid rows before serving reads."""
        self._ensure_open()
        for partial in self.directory.glob(".partial-*.tmp"):
            try:
                if partial.is_file() or partial.is_symlink():
                    partial.unlink()
                elif partial.exists():
                    raise CacheError(f"stale temporary is not a file: {partial.name}")
            except OSError as exc:
                raise CacheError(f"cannot remove stale temporary {partial.name}") from exc

        rows = self._db.execute("SELECT sha256, size, filename FROM blobs").fetchall()
        indexed: set[str] = set()
        for row in rows:
            try:
                sha256 = self._validate_digest(row["sha256"])
                size = int(row["size"])
            except (CacheError, TypeError, ValueError):
                sha256 = str(row["sha256"])
                self._delete_invalid_row(sha256)
                continue
            path = self._blob_path(sha256)
            # A lower quota on restart creates pressure; it must not invalidate valid
            # pinned content that an active execution still requires.
            if not self._verify_file(path, sha256, size):
                self._delete_row(sha256)
                continue
            # Older/corrupt metadata cannot control filesystem paths.  The canonical
            # path is authoritative and is written back before the row is usable.
            if row["filename"] != path.name:
                self._db.execute(
                    "UPDATE blobs SET filename = ? WHERE sha256 = ?", (path.name, sha256)
                )
            indexed.add(sha256)

        # A rename can precede the SQLite commit.  Such a canonical orphan is never
        # ready and is safe to remove; arbitrary files in the cache directory are left
        # alone because they are outside this module's ownership.
        for blob in self.directory.glob("*.blob"):
            if not blob.is_file() and not blob.is_symlink():
                if blob.exists():
                    raise CacheError(f"cache blob is not a file: {blob.name}")
                continue
            if not blob.name.endswith(".blob"):
                continue
            sha256 = blob.name[:-5]
            if _DIGEST.fullmatch(sha256) and sha256 not in indexed:
                try:
                    blob.unlink()
                except OSError as exc:
                    raise CacheError(f"cannot remove unindexed blob {blob.name}") from exc
        self._trim_unpinned()
        self._fsync_directory(self.directory)

    def _begin(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

    def _rollback(self) -> None:
        if self._db.in_transaction:
            self._db.rollback()

    def _commit(self) -> None:
        self._db.commit()

    def _lookup_valid(self, variant: Variant) -> Path | None:
        sha256 = self._validate_digest(variant.sha256)
        row = self._db.execute(
            "SELECT sha256, size, filename FROM blobs WHERE sha256 = ?", (sha256,)
        ).fetchone()
        if row is None:
            orphan = self._blob_path(sha256)
            if orphan.is_file() or orphan.is_symlink():
                self._unlink_blob(sha256)
            return None
        path = self._blob_path(sha256)
        if int(row["size"]) != variant.size or not self._verify_file(path, sha256, variant.size):
            self._begin()
            try:
                self._delete_row(sha256)
                self._commit()
            except BaseException:
                self._rollback()
                raise
            return None
        return path

    def _touch_and_pin(self, sha256: str, owner: str) -> Path:
        path = self._blob_path(sha256)
        self._begin()
        try:
            row = self._db.execute("SELECT size FROM blobs WHERE sha256 = ?", (sha256,)).fetchone()
            if row is None or not self._verify_file(path, sha256, int(row["size"])):
                if row is not None:
                    self._delete_row(sha256)
                self._commit()
                raise CacheError("variant is not ready")
            self._db.execute(
                "UPDATE blobs SET accessed = ? WHERE sha256 = ?", (time.time(), sha256)
            )
            self._db.execute(
                "INSERT OR IGNORE INTO pins(sha256, owner) VALUES (?, ?)", (sha256, owner)
            )
            self._commit()
            return path
        except BaseException:
            self._rollback()
            raise

    def _indexed_bytes(self) -> int:
        row = self._db.execute("SELECT COALESCE(SUM(size), 0) AS total FROM blobs").fetchone()
        return int(row["total"])

    def _trim_unpinned(self) -> None:
        """Reduce restart pressure where possible without dropping required bytes."""
        while self._indexed_bytes() > self.max_bytes:
            row = self._db.execute(
                """
                SELECT b.sha256
                FROM blobs AS b
                WHERE NOT EXISTS (SELECT 1 FROM pins AS p WHERE p.sha256 = b.sha256)
                ORDER BY b.accessed ASC, b.sha256 ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return
            self._begin()
            try:
                self._delete_row(row["sha256"])
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def _evict_for(self, required: int) -> None:
        while self._indexed_bytes() + self._temporary_bytes + required > self.max_bytes:
            row = self._db.execute(
                """
                SELECT b.sha256
                FROM blobs AS b
                WHERE NOT EXISTS (SELECT 1 FROM pins AS p WHERE p.sha256 = b.sha256)
                ORDER BY b.accessed ASC, b.sha256 ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                raise CacheCapacityError("cache capacity is protected by pins")
            self._begin()
            try:
                self._delete_row(row["sha256"])
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def _cleanup_target_orphan(self, sha256: str) -> None:
        path = self._blob_path(sha256)
        if path.is_file() or path.is_symlink():
            self._unlink_blob(sha256)

    def secure(self, variant: Variant, chunks: Iterable[bytes], pin: str) -> Path:
        """Store exact bytes for ``variant`` and pin them for ``pin``."""
        if not isinstance(variant, Variant):
            raise TypeError("variant must be a contracts.models.Variant")
        owner = self._validate_owner(pin)
        sha256 = self._validate_digest(variant.sha256)
        if variant.size > self.max_bytes:
            raise CacheCapacityError("variant is larger than cache capacity")

        with self._lock:
            self._ensure_open()
            if not self._acquisition_active:
                self._recover()
            path = self._lookup_valid(variant)
            if path is not None:
                return self._touch_and_pin(sha256, owner)
            self._cleanup_target_orphan(sha256)
            self._evict_for(variant.size)

            fd, temporary_name = tempfile.mkstemp(
                prefix=".partial-", suffix=".tmp", dir=self.directory
            )
            temporary = Path(temporary_name)
            count = 0
            self._temporary_bytes = 0
            self._acquisition_active = True
            try:
                digest = hashlib.sha256()
                with os.fdopen(fd, "wb") as stream:
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
                    os.fsync(stream.fileno())
                if count != variant.size:
                    raise CacheError("byte stream is shorter than declared variant size")
                if digest.hexdigest() != sha256:
                    raise CacheError("byte stream SHA-256 does not match variant")

                # The durable publication precedes readiness indexing.  If indexing
                # fails, leave the orphan for restart repair rather than reporting it.
                os.replace(temporary, self._blob_path(sha256))
                self._fsync_directory(self.directory)
                self._temporary_bytes = 0
                self._begin()
                try:
                    self._db.execute(
                        "INSERT OR REPLACE INTO blobs(sha256, size, filename, accessed) "
                        "VALUES (?, ?, ?, ?)",
                        (sha256, variant.size, self._blob_path(sha256).name, time.time()),
                    )
                    self._db.execute(
                        "INSERT OR IGNORE INTO pins(sha256, owner) VALUES (?, ?)",
                        (sha256, owner),
                    )
                    self._commit()
                except BaseException:
                    self._rollback()
                    raise
                return self._blob_path(sha256)
            finally:
                self._temporary_bytes = 0
                self._acquisition_active = False
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def path_for(self, variant: Variant) -> Path | None:
        """Return a verified ready path, repairing an invalid entry as a miss."""
        if not isinstance(variant, Variant):
            raise TypeError("variant must be a contracts.models.Variant")
        self._validate_digest(variant.sha256)
        with self._lock:
            self._ensure_open()
            path = self._lookup_valid(variant)
            if path is None:
                return None
            self._begin()
            try:
                self._db.execute(
                    "UPDATE blobs SET accessed = ? WHERE sha256 = ?",
                    (time.time(), variant.sha256),
                )
                self._commit()
            except BaseException:
                self._rollback()
                raise
            return path

    def pin(self, sha256: str, owner: str) -> None:
        """Pin an already-ready digest for an explicit owner."""
        sha256 = self._validate_digest(sha256)
        owner = self._validate_owner(owner)
        with self._lock:
            self._ensure_open()
            row = self._db.execute("SELECT size FROM blobs WHERE sha256 = ?", (sha256,)).fetchone()
            if row is None or not self._verify_file(self._blob_path(sha256), sha256, int(row["size"])):
                if row is not None:
                    self._begin()
                    try:
                        self._delete_row(sha256)
                        self._commit()
                    except BaseException:
                        self._rollback()
                        raise
                raise CacheError("cannot pin a missing or corrupt variant")
            self._begin()
            try:
                self._db.execute(
                    "INSERT OR IGNORE INTO pins(sha256, owner) VALUES (?, ?)", (sha256, owner)
                )
                self._db.execute(
                    "UPDATE blobs SET accessed = ? WHERE sha256 = ?", (time.time(), sha256)
                )
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def release(self, owner: str) -> None:
        """Release every pin owned by ``owner``; expiration is caller-managed."""
        owner = self._validate_owner(owner)
        with self._lock:
            self._ensure_open()
            self._begin()
            try:
                self._db.execute("DELETE FROM pins WHERE owner = ?", (owner,))
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def stats(self) -> dict[str, int]:
        """Return byte and pin counts, including any active temporary bytes."""
        with self._lock:
            self._ensure_open()
            if not self._acquisition_active:
                self._recover()
            row = self._db.execute(
                """
                SELECT
                    COUNT(*) AS entries,
                    COALESCE(SUM(size), 0) AS bytes,
                    COALESCE(SUM(CASE WHEN EXISTS (
                        SELECT 1 FROM pins AS p WHERE p.sha256 = b.sha256
                    ) THEN size ELSE 0 END), 0) AS pinned_bytes,
                    COALESCE(SUM(CASE WHEN EXISTS (
                        SELECT 1 FROM pins AS p WHERE p.sha256 = b.sha256
                    ) THEN 1 ELSE 0 END), 0) AS pinned_entries
                FROM blobs AS b
                """
            ).fetchone()
            return {
                "bytes": int(row["bytes"]),
                "entries": int(row["entries"]),
                "pinned_bytes": int(row["pinned_bytes"]),
                "pinned_entries": int(row["pinned_entries"]),
                "temporary_bytes": self._temporary_bytes,
                "max_bytes": self.max_bytes,
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:
                pass
            self._db.close()
            self._closed = True


__all__ = ["Cache", "CacheCapacityError", "CacheError"]
