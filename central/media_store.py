"""Single-writer durable blobs and authenticated, bounded local read leases."""

from __future__ import annotations

import fcntl
import hashlib
import math
import os
import re
import secrets
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from psycopg.types.json import Jsonb

from central.db import MEDIA_LOCK
from central.execution_ports import ExecutionMediaAuthorization
from central.installation_ports import InstallationSessions
from central.installation_repository import PostgresInstallationRepository
from central.media_repository import JobLease, MediaRepository
from central.registry import RegistryError
from contracts.models import PlayerConfiguration, Variant
from media.prepare import PreparedMedia

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_READ_SECONDS = 120
_TRANSFER_SECONDS = 150
_SCAN_LIMIT = 20000


class MediaStoreError(RegistryError):
    pass


@dataclass(frozen=True)
class StagingPaths:
    directory: Path
    original: Path
    variant: Path


@dataclass(frozen=True)
class RecoveryReport:
    jobs: int = 0
    corrupt: int = 0
    orphans: int = 0
    pending: int = 0


@dataclass(frozen=True)
class GCReport:
    removed: int
    accounted_bytes: int
    pressure: bool


class ReadLease:
    """A descriptor closes before its conservative database transfer pin expires."""

    def __init__(self, store, fd: int, variant: Variant, owner: str, start: tuple[float, float]):
        self.variant, self._store, self._fd, self._owner = variant, store, fd, owner
        self._start, self._remaining = start, variant.size
        self._lock = threading.RLock()
        self._released = False
        self._initial_stat = os.fstat(fd)
        self._timer = threading.Timer(
            max(0.001, _READ_SECONDS - (time.monotonic() - start[0])), self.close
        )
        self._timer.daemon = True
        self._timer.start()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def read(self, size: int = 65536) -> bytes:
        if type(size) is not int or not 1 <= size <= 65536:
            raise MediaStoreError("media_read_limit", 400)
        with self._lock:
            if self._fd is None:
                if self._remaining == 0:
                    return b""
                raise MediaStoreError("media_read_closed", 410)
            try:
                self._store._deadline(self._start)
                current = os.fstat(self._fd)
                if (current.st_size, current.st_mtime_ns) != (
                    self._initial_stat.st_size,
                    self._initial_stat.st_mtime_ns,
                ):
                    raise MediaStoreError("media_corrupt", 503)
                data = os.read(self._fd, min(size, self._remaining))
                if not data and self._remaining:
                    raise MediaStoreError("media_corrupt", 503)
                self._remaining -= len(data)
                if not self._remaining:
                    self.close()
                return data
            except (OSError, MediaStoreError) as error:
                self.close()
                if isinstance(error, MediaStoreError) and error.code == "media_corrupt":
                    self._store._mark_corrupt(self.variant.sha256)
                if isinstance(error, MediaStoreError):
                    raise
                raise MediaStoreError("media_io", 503) from None

    def close(self):
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            self._timer.cancel()
            if not self._released:
                # The descriptor is gone even if PostgreSQL is temporarily unavailable.
                try:
                    with self._store.repository.transaction() as conn:
                        conn.execute("DELETE FROM media_references WHERE owner=%s", (self._owner,))
                    self._released = True
                except Exception:
                    pass  # Expiry retains a conservative pin; close must not mask response failure.


class MediaStore:
    def __init__(
        self,
        repository: MediaRepository,
        root: Path,
        *,
        installation: InstallationSessions | None = None,
        execution: ExecutionMediaAuthorization | None = None,
    ):
        self.repository, self.root = repository, Path(root).absolute()
        self.db, self.clock = repository.db, repository.clock
        self.installation = installation or PostgresInstallationRepository(self.clock)
        self.execution = execution
        self._lock_fd: int | None = None
        self._writer_pid: int | None = None
        self._mutation = threading.RLock()

    @staticmethod
    def _directory(path: Path):
        path.mkdir(mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise MediaStoreError("media_path", 503)
        path.chmod(0o700)

    @contextmanager
    def worker_lock(self):
        if self._lock_fd is not None:
            raise MediaStoreError("media_writer_active", 503)
        fd = None
        try:
            self._directory(self.root)
            fd = os.open(self.root / ".worker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise MediaStoreError("media_path", 503)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MediaStoreError("media_writer_active", 503) from None
            self._lock_fd, self._writer_pid = fd, os.getpid()
            for name in ("blobs", "staging"):
                self._directory(self.root / name)
            self.recover()
            yield self
        except OSError:
            raise MediaStoreError("media_io", 503) from None
        finally:
            self._lock_fd, self._writer_pid = None, None
            if fd is not None:
                os.close(fd)

    @contextmanager
    def _writer(self):
        with self._mutation:
            if self._lock_fd is None or self._writer_pid != os.getpid():
                raise MediaStoreError("media_writer_required", 503)
            try:
                yield
            except OSError:
                raise MediaStoreError("media_io", 503) from None

    def _paths(self, lease: JobLease) -> StagingPaths:
        if not all(_NAME.fullmatch(name) for name in (lease.job_id, lease.attempt_token)):
            raise MediaStoreError("media_path", 400)
        directory = self.root / "staging" / lease.job_id / lease.attempt_token
        return StagingPaths(directory, directory / "original", directory / "variant")

    def _blob(self, digest: str) -> Path:
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise MediaStoreError("media_digest", 400)
        return self.root / "blobs" / digest

    def staging(self, lease: JobLease) -> StagingPaths:
        with self._writer(), self.repository.transaction() as conn:
            row = self.repository.checked_job(conn, lease)
            if row["state"] != "running" or row["reserved_bytes"] != lease.reserved_bytes:
                raise MediaStoreError("stale_job")
            paths = self._paths(lease)
            self._directory(paths.directory.parent)
            self._directory(paths.directory)
            return paths

    def _start(self) -> tuple[float, float]:
        now = self.clock.monotonic()
        if not math.isfinite(now):
            raise MediaStoreError("clock_invalid", 503)
        return time.monotonic(), now

    def _deadline(self, start: tuple[float, float]):
        now = self.clock.monotonic()
        if not math.isfinite(now) or now < start[1]:
            raise MediaStoreError("clock_invalid", 503)
        if max(time.monotonic() - start[0], now - start[1]) >= _READ_SECONDS:
            raise MediaStoreError("media_read_timeout", 504)

    def _open(self, path: Path) -> int:
        """Walk every store directory using nonsymlink directory descriptors."""
        relative = path.relative_to(self.root)
        if not relative.parts or any(part in (".", "..") for part in relative.parts):
            raise MediaStoreError("media_path", 503)
        parent = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in relative.parts[:-1]:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            return os.open(
                relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
        finally:
            os.close(parent)

    def _hash(self, fd: int, maximum: int, start=None) -> tuple[int, str, str]:
        start = start or self._start()
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
            raise MediaStoreError("media_corrupt", 503)
        sha1, sha256, count = hashlib.sha1(), hashlib.sha256(), 0
        os.lseek(fd, 0, os.SEEK_SET)
        while chunk := os.read(fd, 65536):
            self._deadline(start)
            count += len(chunk)
            if count > maximum:
                raise MediaStoreError("media_corrupt", 503)
            sha1.update(chunk)
            sha256.update(chunk)
        after = os.fstat(fd)
        if (count, before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise MediaStoreError("media_corrupt", 503)
        os.lseek(fd, 0, os.SEEK_SET)
        return count, sha1.hexdigest(), sha256.hexdigest()

    def _verify(self, path: Path, variant: Variant):
        fd = self._open(path)
        try:
            size, _, digest = self._hash(fd, variant.size)
            if (size, digest) != (variant.size, variant.sha256):
                raise MediaStoreError("media_corrupt", 503)
        finally:
            os.close(fd)

    @staticmethod
    def _fsync_directory(path: Path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _rename(source: Path, destination: Path):
        if destination.exists() or destination.is_symlink():
            raise MediaStoreError("media_path", 503)
        os.rename(source, destination)

    @staticmethod
    def _unlink(path: Path):
        path.unlink(missing_ok=True)

    def _remove(self, path: Path, *, depth=0, budget=None):
        budget = [_SCAN_LIMIT] if budget is None else budget
        budget[0] -= 1
        if budget[0] < 0 or depth > 8:
            raise MediaStoreError("media_scan_limit", 503)
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISDIR(mode):
            for child in path.iterdir():
                self._remove(child, depth=depth + 1, budget=budget)
            path.rmdir()
        else:
            self._unlink(path)

    def _refresh(self, conn, digest: str):
        sources = conn.execute(
            "SELECT DISTINCT s.source_ref,s.status FROM media_sources s "
            "JOIN source_members m ON m.source_ref=s.source_ref JOIN media_jobs j "
            "ON j.asset_id=m.asset_id WHERE j.variant_sha=%s",
            (digest,),
        ).fetchall()
        for source in sources:
            self.repository.refresh_catalog_in(
                conn, source["source_ref"], self.clock.utc(), source["status"]
            )

    def _mark_corrupt(self, digest: str):
        # Corruption may have enlarged the file. Preserve the larger measured charge.
        size = self._tree_size(self._blob(digest))
        with self.repository.transaction() as conn:
            conn.execute(
                "UPDATE media_blobs SET state='corrupt',size=GREATEST(size,%s) "
                "WHERE digest=%s AND state<>'deleting'",
                (size, digest),
            )
            self._refresh(conn, digest)

    def _checked(self, conn, lease):
        row = conn.execute(
            "SELECT * FROM media_jobs WHERE id=%s AND attempt_token=%s AND lease_until>%s",
            (lease.job_id, lease.attempt_token, self.clock.utc()),
        ).fetchone()
        if (
            not row
            or row["asset_id"] != lease.asset.asset_id
            or row["recipe_id"] != lease.recipe_id
        ):
            raise MediaStoreError("stale_job")
        return row

    def publish(self, lease: JobLease, prepared: PreparedMedia) -> Variant:
        with self._writer():
            with self.repository.transaction() as conn:
                if self.repository.checked_job(conn, lease)["state"] != "running":
                    raise MediaStoreError("stale_job")
            paths, variant = self._paths(lease), prepared.variant
            if prepared.path.absolute() != paths.variant or prepared.recipe_id != lease.recipe_id:
                raise MediaStoreError("media_result_mismatch")
            cap = (
                self.repository.limits.max_image_bytes
                if lease.asset.kind == "image"
                else self.repository.limits.max_video_bytes
            )
            if variant.size > cap or variant.media_type != (
                "image/jpeg" if lease.asset.kind == "image" else "video/mp4"
            ):
                raise MediaStoreError("media_result_mismatch")
            try:
                fd = self._open(paths.original)
                try:
                    original_size, sha1, sha256 = self._hash(
                        fd, self.repository.limits.max_original_bytes
                    )
                finally:
                    os.close(fd)
                if (
                    sha1 != lease.asset.original_sha1
                    or sha256 != prepared.original_sha256
                    or (
                        lease.asset.file_size is not None and original_size != lease.asset.file_size
                    )
                ):
                    raise MediaStoreError("media_original_mismatch")
                self._verify(paths.variant, variant)
                with self.repository.transaction() as conn:
                    row = self.repository.checked_job(conn, lease)
                    if (
                        row["state"] != "running"
                        or row["reserved_bytes"] < original_size + variant.size
                    ):
                        raise MediaStoreError("stale_job")
                    existing = conn.execute(
                        "SELECT * FROM media_blobs WHERE digest=%s", (variant.sha256,)
                    ).fetchone()
                    if existing:
                        if existing["state"] != "ready" or existing[
                            "variant"
                        ] != variant.model_dump(mode="json"):
                            raise MediaStoreError("media_blob_unavailable", 503)
                        self._verify(self._blob(variant.sha256), variant)
                    else:
                        conn.execute(
                            "INSERT INTO media_blobs VALUES(%s,%s,%s,'publishing',%s)",
                            (
                                variant.sha256,
                                Jsonb(variant.model_dump(mode="json")),
                                variant.size,
                                self.clock.utc(),
                            ),
                        )
                    result = {
                        **prepared.model_dump(mode="json", exclude={"path"}),
                        "asset_id": lease.asset.asset_id,
                        "original_sha1": lease.asset.original_sha1,
                    }
                    conn.execute(
                        "UPDATE media_jobs SET state='publishing',variant_sha=%s,result=%s,"
                        "reserved_bytes=reserved_bytes-%s,cleanup_state='ready',updated_at=%s WHERE id=%s",
                        (
                            variant.sha256,
                            Jsonb(result),
                            0 if existing else variant.size,
                            self.clock.utc(),
                            lease.job_id,
                        ),
                    )
                if not existing:
                    fd = self._open(paths.variant)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    self._rename(paths.variant, self._blob(variant.sha256))
                    self._fsync_directory(paths.directory)
                    self._fsync_directory(self.root / "blobs")
                with self.repository.transaction() as conn:
                    row = self._checked(conn, lease)
                    conn.execute(
                        "UPDATE media_jobs SET state='cleanup' WHERE id=%s", (lease.job_id,)
                    )
                self._cleanup(row, lease=lease)
                return variant
            except OSError:
                raise MediaStoreError("media_io", 503) from None

    def _lease_for(self, conn, row) -> JobLease:
        from media.models import OriginalAsset

        asset = conn.execute(
            "SELECT metadata FROM asset_revisions WHERE asset_id=%s", (row["asset_id"],)
        ).fetchone()
        return JobLease(
            job_id=row["id"],
            attempt_token=row["attempt_token"],
            asset=OriginalAsset.model_validate(asset["metadata"]),
            recipe_id=row["recipe_id"],
            lease_until=row["lease_until"],
            reserved_bytes=max(1, row["reserved_bytes"]),
        )

    def _cleanup(self, row, *, lease=None):
        with self.repository.transaction() as conn:
            recorded = self._lease_for(conn, row)
        paths = self._paths(recorded)
        if row["cleanup_state"] == "ready":
            result = row["result"]
            if (
                not result
                or result["recipe_id"] != row["recipe_id"]
                or result["variant"]["sha256"] != row["variant_sha"]
                or result.get("asset_id") != row["asset_id"]
                or result.get("original_sha1") != recorded.asset.original_sha1
            ):
                raise MediaStoreError("media_journal_invalid", 503)
            variant = Variant.model_validate(result["variant"])
            self._verify(self._blob(variant.sha256), variant)
        self._remove(paths.directory)
        self._fsync_directory(
            paths.directory.parent if paths.directory.parent.exists() else self.root / "staging"
        )
        with self.repository.transaction() as conn:
            current = (
                self._checked(conn, lease)
                if lease
                else conn.execute(
                    "SELECT * FROM media_jobs WHERE id=%s AND attempt_token=%s",
                    (row["id"], row["attempt_token"]),
                ).fetchone()
            )
            if not current:
                raise MediaStoreError("stale_job")
            target = current["cleanup_state"]
            if target not in ("ready", "retry", "failed"):
                raise MediaStoreError("media_journal_invalid", 503)
            if target == "ready":
                blob = conn.execute(
                    "SELECT state,variant,size FROM media_blobs WHERE digest=%s",
                    (current["variant_sha"],),
                ).fetchone()
                if (
                    not blob
                    or blob["state"] not in ("ready", "publishing")
                    or blob["variant"] != current["result"]["variant"]
                    or blob["size"] != current["result"]["variant"]["size"]
                ):
                    raise MediaStoreError("media_journal_invalid", 503)
                conn.execute(
                    "UPDATE media_blobs SET state='ready' WHERE digest=%s AND state='publishing'",
                    (current["variant_sha"],),
                )
            conn.execute(
                "UPDATE media_jobs SET state=%s,reserved_bytes=0,cleanup_state=NULL,lease_until=NULL,"
                "updated_at=%s WHERE id=%s",
                (target, self.clock.utc(), row["id"]),
            )
            if current["variant_sha"]:
                self._refresh(conn, current["variant_sha"])

    def fail(self, lease: JobLease, code: str, *, retry: bool = False):
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            raise MediaStoreError("media_failure_code", 400)
        if type(retry) is not bool:
            raise MediaStoreError("media_retry_policy", 400)
        with self._writer():
            with self.repository.transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM media_jobs WHERE id=%s AND attempt_token=%s "
                    "AND state IN ('running','publishing','cleanup')",
                    (lease.job_id, lease.attempt_token),
                ).fetchone()
                if (
                    not row
                    or row["asset_id"] != lease.asset.asset_id
                    or row["recipe_id"] != lease.recipe_id
                ):
                    raise MediaStoreError("stale_job")
                if row["variant_sha"]:
                    blob = conn.execute(
                        "SELECT state FROM media_blobs WHERE digest=%s", (row["variant_sha"],)
                    ).fetchone()
                    if blob and blob["state"] == "publishing":
                        conn.execute(
                            "UPDATE media_blobs SET state='corrupt' WHERE digest=%s",
                            (row["variant_sha"],),
                        )
                conn.execute(
                    "UPDATE media_jobs SET state='cleanup',cleanup_state=%s,retry_at=%s,"
                    "failure_code=%s,updated_at=%s WHERE id=%s RETURNING *",
                    (
                        "retry" if retry else "failed",
                        0,
                        code,
                        self.clock.utc(),
                        row["id"],
                    ),
                )
                row = conn.execute("SELECT * FROM media_jobs WHERE id=%s", (row["id"],)).fetchone()
            try:
                self._cleanup(row)
            except OSError:
                raise MediaStoreError("media_cleanup", 503) from None

    def _tree_size(self, path: Path, *, depth=0, budget=None) -> int:
        budget = [_SCAN_LIMIT] if budget is None else budget
        budget[0] -= 1
        if budget[0] < 0 or depth > 8:
            raise MediaStoreError("media_scan_limit", 503)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return 0
        if stat.S_ISDIR(info.st_mode):
            return sum(
                self._tree_size(child, depth=depth + 1, budget=budget) for child in path.iterdir()
            )
        return info.st_size

    def _orphan(self, path: Path) -> bool:
        relative = str(path.relative_to(self.root))
        if relative in (".", ".worker.lock", "blobs", "staging") or ".." in Path(relative).parts:
            raise MediaStoreError("media_path", 503)
        size = self._tree_size(path)
        with self.repository.transaction() as conn:
            conn.execute(
                "INSERT INTO media_orphans(path,size) VALUES(%s,%s) "
                "ON CONFLICT(path) DO UPDATE SET size=GREATEST(media_orphans.size,EXCLUDED.size)",
                (relative, size),
            )
        try:
            self._remove(path)
            self._fsync_directory(path.parent)
        except OSError:
            return False
        with self.repository.transaction() as conn:
            conn.execute("DELETE FROM media_orphans WHERE path=%s", (relative,))
        return True

    def recover(self) -> RecoveryReport:
        with self._writer():
            jobs, corrupt, orphans, pending = 0, 0, 0, 0
            with self.repository.transaction() as conn:
                rows = conn.execute(
                    "SELECT * FROM media_jobs WHERE reserved_bytes>0 OR state IN "
                    "('running','publishing','cleanup') ORDER BY id LIMIT %s",
                    (_SCAN_LIMIT + 1,),
                ).fetchall()
                if len(rows) > _SCAN_LIMIT:
                    raise MediaStoreError("media_scan_limit", 503)
            for row in rows:
                lease = None
                try:
                    with self.repository.transaction() as conn:
                        lease = self._lease_for(conn, row)
                    if row["cleanup_state"] == "ready" and row["result"]:
                        variant = Variant.model_validate(row["result"]["variant"])
                        canonical, staged = self._blob(variant.sha256), self._paths(lease).variant
                        if not canonical.exists() and not canonical.is_symlink():
                            self._verify(staged, variant)
                            fd = self._open(staged)
                            try:
                                os.fsync(fd)
                            finally:
                                os.close(fd)
                            self._rename(staged, canonical)
                            self._fsync_directory(canonical.parent)
                            self._fsync_directory(staged.parent)
                        self._verify(canonical, variant)
                    elif row["cleanup_state"] is None:
                        with self.repository.transaction() as conn:
                            row = conn.execute(
                                "UPDATE media_jobs SET state='cleanup',cleanup_state='retry',"
                                "retry_at=0,failure_code='worker_interrupted' WHERE id=%s RETURNING *",
                                (row["id"],),
                            ).fetchone()
                    self._cleanup(row)
                    jobs += 1
                except (OSError, MediaStoreError, ValueError, KeyError, TypeError):
                    if lease is None:
                        raise MediaStoreError("media_journal_invalid", 503) from None
                    try:
                        self.fail(lease, "publication_incomplete", retry=True)
                    except (OSError, MediaStoreError):
                        pending += 1
            with self.repository.transaction() as conn:
                blobs = conn.execute("SELECT * FROM media_blobs WHERE state='ready'").fetchall()
                abandoned = conn.execute(
                    "SELECT digest FROM media_blobs b WHERE state='publishing' "
                    "AND NOT EXISTS(SELECT 1 FROM media_jobs j WHERE j.variant_sha=b.digest "
                    "AND j.state IN ('publishing','cleanup'))"
                ).fetchall()
            for blob in abandoned:
                self._mark_corrupt(blob["digest"])
            for blob in blobs:
                try:
                    self._verify(
                        self._blob(blob["digest"]), Variant.model_validate(blob["variant"])
                    )
                except (OSError, MediaStoreError, ValueError):
                    self._mark_corrupt(blob["digest"])
                    corrupt += 1
            self.collect()
            with self.repository.transaction() as conn:
                known_blobs = {
                    row["digest"]
                    for row in conn.execute("SELECT digest FROM media_blobs").fetchall()
                }
                active = {
                    (row["id"], row["attempt_token"])
                    for row in conn.execute(
                        "SELECT id,attempt_token FROM media_jobs WHERE reserved_bytes>0"
                    ).fetchall()
                }
                tracked = [
                    self.root / row["path"]
                    for row in conn.execute("SELECT path FROM media_orphans").fetchall()
                ]
            unknown = list(tracked)
            budget = [_SCAN_LIMIT]

            def children(path):
                for child in path.iterdir():
                    budget[0] -= 1
                    if budget[0] < 0:
                        raise MediaStoreError("media_scan_limit", 503)
                    yield child

            unknown.extend(p for p in children(self.root / "blobs") if p.name not in known_blobs)
            for directory in children(self.root / "staging"):
                if not directory.is_dir() or directory.is_symlink():
                    unknown.append(directory)
                    continue
                for attempt in children(directory):
                    if (directory.name, attempt.name) not in active:
                        unknown.append(attempt)
            unknown += [
                p for p in children(self.root) if p.name not in (".worker.lock", "blobs", "staging")
            ]
            if len(unknown) > _SCAN_LIMIT:
                raise MediaStoreError("media_scan_limit", 503)
            for path in dict.fromkeys(unknown):
                if self._orphan(path):
                    orphans += 1
                else:
                    pending += 1
            return RecoveryReport(jobs, corrupt, orphans, pending)

    def collect(self, *, target_bytes: int | None = None) -> GCReport:
        with self._writer():
            removed = 0
            with self.repository.transaction() as conn:
                maximum = conn.execute(
                    "SELECT max_bytes FROM media_settings WHERE singleton"
                ).fetchone()["max_bytes"]
                target = maximum if target_bytes is None else target_bytes
                if type(target) is not int or target < 0:
                    raise MediaStoreError("media_quota", 400)
                rows = conn.execute(
                    "SELECT * FROM media_blobs WHERE state IN ('ready','corrupt','deleting') "
                    "ORDER BY CASE WHEN state='ready' THEN 1 ELSE 0 END,created_at,digest"
                ).fetchall()
            for row in rows:
                with self.repository.transaction() as conn:
                    if row["state"] == "ready" and self.repository.accounted_bytes(conn) <= target:
                        continue
                    if conn.execute(
                        "SELECT 1 FROM media_references WHERE digest=%s AND expires_at>%s",
                        (row["digest"], self.clock.utc()),
                    ).fetchone():
                        continue
                    if conn.execute(
                        "SELECT 1 FROM media_jobs WHERE variant_sha=%s AND cleanup_state='ready'",
                        (row["digest"],),
                    ).fetchone():
                        continue
                    changed = conn.execute(
                        "UPDATE media_blobs SET state='deleting' WHERE digest=%s "
                        "AND state IN ('ready','corrupt','deleting') RETURNING digest",
                        (row["digest"],),
                    ).fetchone()
                    if not changed:
                        continue
                    self._refresh(conn, row["digest"])
                try:
                    self._remove(self._blob(row["digest"]))
                    self._fsync_directory(self.root / "blobs")
                except OSError:
                    continue
                with self.repository.transaction() as conn:
                    conn.execute("DELETE FROM media_references WHERE digest=%s", (row["digest"],))
                    conn.execute(
                        "UPDATE media_jobs SET variant_sha=NULL,state=CASE WHEN state='ready' "
                        "THEN 'evicted' ELSE state END,retry_at=%s,updated_at=%s WHERE variant_sha=%s",
                        (self.clock.utc(), self.clock.utc(), row["digest"]),
                    )
                    conn.execute(
                        "DELETE FROM media_blobs WHERE digest=%s AND state='deleting'",
                        (row["digest"],),
                    )
                removed += 1
            with self.repository.transaction() as conn:
                total = self.repository.accounted_bytes(conn)
                return GCReport(removed, total, total > maximum)

    def set_quota(self, max_bytes: int) -> dict:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise MediaStoreError("media_quota", 400)
        with self.repository.transaction() as conn:
            conn.execute("UPDATE media_settings SET max_bytes=%s WHERE singleton", (max_bytes,))
        return self.repository.health()

    def open_read(self, token: str, digest: str) -> ReadLease:
        path, start = self._blob(digest), self._start()
        owner, fd, granted = "transfer:" + secrets.token_hex(24), None, False
        try:
            with self.db.transaction() as conn:
                conn.execute("SET LOCAL lock_timeout='5s'")
                conn.execute("SET LOCAL statement_timeout='10s'")
                identity = self.installation.authenticate_in(conn, token)
                if identity is None:
                    raise MediaStoreError("unauthorized", 401)
                observed = self.installation.configuration_in(
                    conn, identity["id"], identity["authority_epoch"]
                )
                if observed is None:
                    raise MediaStoreError("unauthorized", 401)
                configuration = PlayerConfiguration(
                    player_id=identity["id"],
                    authority_epoch=identity["authority_epoch"],
                    configuration_revision=1,
                    bindings=tuple(observed["bindings"]),
                    enabled_outputs=tuple(b.output_id for b in observed["execution_bindings"]),
                )
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
                blob = conn.execute(
                    "SELECT * FROM media_blobs WHERE digest=%s AND state='ready'", (digest,)
                ).fetchone()
                if not blob:
                    raise MediaStoreError("media_unavailable", 404)
                variant, now = Variant.model_validate(blob["variant"]), self.clock.utc()
                refs = {
                    row["owner"]
                    for row in conn.execute(
                        "SELECT owner FROM media_references WHERE digest=%s AND expires_at>%s",
                        (digest, now),
                    ).fetchall()
                }
                if self.execution is None:
                    raise MediaStoreError("media_authorization_unavailable", 503)
                authorized = self.execution.media_authorized_in(
                    conn,
                    identity["id"],
                    identity["authority_epoch"],
                    configuration,
                    variant,
                    refs,
                    now,
                )
                if not authorized:
                    raise MediaStoreError("media_forbidden", 403)
                conn.execute(
                    "INSERT INTO media_references VALUES(%s,%s,%s)",
                    (owner, digest, now + _TRANSFER_SECONDS),
                )
                granted = True
            fd = self._open(path)
            size, _, actual = self._hash(fd, variant.size, start)
            if (size, actual) != (variant.size, digest):
                raise MediaStoreError("media_corrupt", 503)
            self._deadline(start)
            return ReadLease(self, fd, variant, owner, start)
        except (OSError, MediaStoreError) as error:
            if fd is not None:
                os.close(fd)
            if granted:
                with self.repository.transaction() as conn:
                    conn.execute("DELETE FROM media_references WHERE owner=%s", (owner,))
            # Refusals before authorization must not mutate otherwise healthy media.
            if granted and (isinstance(error, OSError) or error.code == "media_corrupt"):
                self._mark_corrupt(digest)
            if isinstance(error, MediaStoreError):
                raise
            raise MediaStoreError("media_unavailable", 503) from None
