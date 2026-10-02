"""Bounded reader for the base-owned, data-only Player payload format.

Only verified regular files under ``app/`` become an immutable digest root. The
archive is data: it never supplies a unit, maintainer script, path, or command.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path
from typing import BinaryIO

from contracts.player_payload import (
    MAX_ARCHIVE_BYTES,
    MAX_MANIFEST_BYTES,
    PayloadError,
    validate_manifest,
)
from contracts.strict_json import loads_object
from uplink.files import write_atomically

BLOCK = 64 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ABI = re.compile(r"sha256:[0-9a-f]{64}")


def parse_manifest(raw: bytes, *, expected_abi: str) -> dict:
    value = loads_object(raw, max_bytes=MAX_MANIFEST_BYTES)
    if value is None:
        raise PayloadError("payload_manifest_invalid")
    manifest = validate_manifest(value)
    if manifest["base_abi"] != expected_abi:
        raise PayloadError("payload_abi_mismatch")
    return manifest


def _copy_verified(source, destination: Path, digest: str, size: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    count = 0
    with destination.open("xb") as output:
        while count < size:
            block = source.read(min(BLOCK, size - count))
            if not block:
                raise PayloadError("payload_truncated")
            count += len(block)
            hasher.update(block)
            output.write(block)
    destination.chmod(0o644)
    if count != size or hasher.hexdigest() != digest:
        raise PayloadError("payload_file_integrity")


def _check_archive(payload: BinaryIO, temporary: Path, expected_abi: str) -> dict:
    try:
        archive = tarfile.open(fileobj=payload, mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise PayloadError("payload_archive_invalid") from exc
    with archive:
        member = archive.next()
        if (member is None or member.name != "manifest.json" or not member.isfile()
                or member.mode != 0o644 or member.uid != 0 or member.gid != 0
                or not 0 < member.size <= MAX_MANIFEST_BYTES):
            raise PayloadError("payload_manifest_missing")
        stream = archive.extractfile(member)
        if stream is None:
            raise PayloadError("payload_manifest_missing")
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) != member.size:
            raise PayloadError("payload_manifest_invalid")
        manifest = parse_manifest(raw, expected_abi=expected_abi)
        seen: set[str] = set()
        while (member := archive.next()) is not None:
            name = member.name
            if (not member.isfile() or member.mode != 0o644 or member.uid != 0
                    or member.gid != 0 or name in seen or name not in manifest["files"]):
                raise PayloadError("payload_member_invalid")
            digest, size = (manifest["files"][name]["sha256"],
                            manifest["files"][name]["size"])
            if member.size != size:
                raise PayloadError("payload_file_size")
            stream = archive.extractfile(member)
            if stream is None:
                raise PayloadError("payload_member_invalid")
            _copy_verified(stream, temporary / name, digest, size)
            seen.add(name)
        if seen != set(manifest["files"]):
            raise PayloadError("payload_members_missing")
        write_atomically(temporary / "manifest.json", raw, mode=0o644)
        for path in sorted(temporary.rglob("*"), reverse=True):
            if path.is_dir():
                path.chmod(0o755)
        return manifest


def verify_root(path: Path, *, expected_abi: str) -> dict:
    """Recheck a fallback/target root before a service stop or rollback."""
    if path.is_symlink() or not path.is_dir():
        raise PayloadError("payload_root_invalid")
    root_stat = path.stat()
    if root_stat.st_uid != os.geteuid() or stat.S_IMODE(root_stat.st_mode) & 0o022:
        raise PayloadError("payload_root_ownership")
    try:
        manifest = parse_manifest((path / "manifest.json").read_bytes(),
                                  expected_abi=expected_abi)
    except OSError as exc:
        raise PayloadError("payload_root_invalid") from exc
    actual = set()
    for member in path.rglob("*"):
        if member.is_symlink() or (not member.is_file() and not member.is_dir()):
            raise PayloadError("payload_root_invalid")
        member_stat = member.stat()
        if member_stat.st_uid != os.geteuid() or stat.S_IMODE(member_stat.st_mode) & 0o022:
            raise PayloadError("payload_root_ownership")
        if member.is_file():
            name = member.relative_to(path).as_posix()
            actual.add(name)
            if name == "manifest.json":
                continue
            if name not in manifest["files"]:
                raise PayloadError("payload_root_extra")
            digest, size = (manifest["files"][name]["sha256"],
                            manifest["files"][name]["size"])
            if member.stat().st_size != size or hashlib.sha256(member.read_bytes()).hexdigest() != digest:
                raise PayloadError("payload_root_integrity")
    if actual != set(manifest["files"]) | {"manifest.json"}:
        raise PayloadError("payload_root_missing")
    return manifest


def _root_target(roots: Path, sha256: str) -> Path:
    if roots.is_symlink():
        raise PayloadError("payload_roots_invalid")
    roots.mkdir(parents=True, exist_ok=True)
    if roots.stat().st_uid != os.geteuid():
        raise PayloadError("payload_roots_invalid")
    target = roots / sha256
    if target.is_symlink():
        raise PayloadError("payload_root_invalid")
    return target


def _publish_archive(payload: BinaryIO, target: Path, roots: Path,
                     expected_abi: str) -> Path:
    if target.exists():
        verify_root(target, expected_abi=expected_abi)
        return target
    temporary = Path(tempfile.mkdtemp(prefix=".payload-", dir=roots))
    try:
        _check_archive(payload, temporary, expected_abi)
        temporary.chmod(0o755)
        os.rename(temporary, target)
        verify_root(target, expected_abi=expected_abi)
        return target
    except BaseException as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        if isinstance(exc, (tarfile.TarError, EOFError)):
            raise PayloadError("payload_archive_invalid") from exc
        raise


def stage_payload(payload: bytes, *, sha256: str, size: int, expected_abi: str,
                  roots: Path) -> Path:
    """Verify complete archive, then publish one digest-keyed read-only root."""
    if (not isinstance(payload, bytes) or len(payload) != size or size > MAX_ARCHIVE_BYTES
            or _SHA256.fullmatch(sha256) is None
            or hashlib.sha256(payload).hexdigest() != sha256):
        raise PayloadError("payload_outer_integrity")
    if _ABI.fullmatch(expected_abi) is None:
        raise PayloadError("payload_abi_invalid")
    target = _root_target(roots, sha256)
    return _publish_archive(io.BytesIO(payload), target, roots, expected_abi)


def stage_payload_file(archive_path: Path, *, sha256: str, size: int,
                       expected_abi: str, roots: Path) -> Path:
    """Freeze and stream one exact local archive before publishing its root.

    The caller owns acquisition. The bounded private copy prevents a changing
    input file from being hashed as one archive and extracted as another. It is
    removed after publication or refusal; no source URL or command is inferred.
    """
    if (not isinstance(archive_path, Path) or type(size) is not int
            or not 0 < size <= MAX_ARCHIVE_BYTES or not isinstance(sha256, str)
            or _SHA256.fullmatch(sha256) is None):
        raise PayloadError("payload_outer_integrity")
    if not isinstance(expected_abi, str) or _ABI.fullmatch(expected_abi) is None:
        raise PayloadError("payload_abi_invalid")
    try:
        source_stat = archive_path.lstat()
    except OSError as exc:
        raise PayloadError("payload_archive_unavailable") from exc
    if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size != size:
        raise PayloadError("payload_outer_integrity")
    target = _root_target(roots, sha256)
    if stat.S_IMODE(roots.stat().st_mode) & 0o022:
        raise PayloadError("payload_roots_invalid")

    try:
        source_fd = os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise PayloadError("payload_archive_unavailable") from exc
    spool: Path | None = None
    try:
        with os.fdopen(source_fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_size != size
                    or (opened.st_dev, opened.st_ino) !=
                    (source_stat.st_dev, source_stat.st_ino)):
                raise PayloadError("payload_outer_integrity")
            if target.exists():
                digest, copied = hashlib.sha256(), 0
                while copied < size:
                    block = source.read(min(BLOCK, size - copied))
                    if not block:
                        raise PayloadError("payload_outer_integrity")
                    copied += len(block)
                    digest.update(block)
                if source.read(1) or digest.hexdigest() != sha256:
                    raise PayloadError("payload_outer_integrity")
                verify_root(target, expected_abi=expected_abi)
                return target
            spool_fd, name = tempfile.mkstemp(prefix=".archive-", dir=roots)
            spool = Path(name)
            digest, copied = hashlib.sha256(), 0
            with os.fdopen(spool_fd, "wb") as frozen:
                while copied < size:
                    block = source.read(min(BLOCK, size - copied))
                    if not block:
                        raise PayloadError("payload_outer_integrity")
                    copied += len(block)
                    digest.update(block)
                    frozen.write(block)
                if source.read(1) or digest.hexdigest() != sha256:
                    raise PayloadError("payload_outer_integrity")
                frozen.flush()
                os.fsync(frozen.fileno())
        with spool.open("rb") as frozen:
            return _publish_archive(frozen, target, roots, expected_abi)
    except OSError as exc:
        raise PayloadError("payload_archive_unavailable") from exc
    finally:
        if spool is not None:
            spool.unlink(missing_ok=True)
