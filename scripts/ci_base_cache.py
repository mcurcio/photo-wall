"""Bounded cache for the pristine extracted Ubuntu base filesystem.

The cache is deliberately a build accelerator only.  It is populated after
the signed raw image has been verified and extracted, before any package,
Player, deployment, or runtime configuration is installed.  The archive is
validated before extraction and is never modified during a restore.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from appliance import build as appliance

SCHEMA = 1
KIND = "photo-wall-pristine-base-cache"
ARCHIVE_NAME = "root.tar"
MANIFEST_NAME = "manifest.json"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024**3
MAX_ROOT_BYTES = 12 * 1024**3
MAX_MEMBER_BYTES = 2 * 1024**3
MAX_MEMBERS = 1_000_000
MAX_FINGERPRINT_FILE_BYTES = 16 * 1024**2
FINGERPRINT_FILES = (
    "appliance/build.py",
    "scripts/ci_base_cache.py",
    "appliance/Dockerfile.builder",
    "contracts/release.py",
    "scripts/fetch_ubuntu.py",
)
FORBIDDEN_ROOTS = (
    "etc/photo-wall",
    "var/lib/photo-wall",
    "opt/photo-wall",
    "tmp/photo-wall-wheels",
)


class CacheError(ValueError):
    """A cache is invalid, unsafe, or cannot be created."""


def _regular(path: Path, maximum: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise CacheError("cache_file_unreadable") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or not 0 < info.st_size <= maximum):
            raise CacheError("cache_file_invalid")
        data = bytearray()
        while block := os.read(fd, min(1024 * 1024, maximum + 1 - len(data))):
            data.extend(block)
            if len(data) > maximum:
                raise CacheError("cache_file_oversized")
        after = os.fstat(fd)
        if (len(data) != info.st_size or after.st_size != info.st_size
                or after.st_mtime_ns != info.st_mtime_ns):
            raise CacheError("cache_file_changed")
        return bytes(data)
    finally:
        os.close(fd)


def _checked_archive(path: Path) -> dict:
    try:
        info = path.lstat()
    except OSError as error:
        raise CacheError("cache_archive_unreadable") from error
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise CacheError("cache_archive_invalid")
    try:
        record = appliance.checked_file(path, MAX_ARCHIVE_BYTES)
    except (OSError, ValueError) as error:
        raise CacheError("cache_archive_invalid") from error
    return record


def _sha256(path: Path, maximum: int = MAX_FINGERPRINT_FILE_BYTES) -> str:
    return _checked_file(path, maximum)["sha256"]


def _checked_file(path: Path, maximum: int) -> dict:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CacheError("fingerprint_file_invalid")
        return appliance.checked_file(path, maximum)
    except (OSError, ValueError) as error:
        if isinstance(error, CacheError):
            raise
        raise CacheError("fingerprint_file_invalid") from error


def fingerprint(repository: Path, *, runner_arch: str | None = None) -> dict:
    """Return the exact input identity used by cache restore and publication."""
    repository = Path(repository).resolve(strict=True)
    architecture = runner_arch or platform.machine()
    if not isinstance(architecture, str) or not architecture or len(architecture) > 64:
        raise CacheError("runner_arch_invalid")
    files = {}
    for relative in FINGERPRINT_FILES:
        path = repository / relative
        files[relative] = _sha256(path)
    return {"base_sha256": appliance.BASE_SHA256,
            "runner_arch": architecture,
            "files": files}


def _manifest_bytes(record: dict) -> bytes:
    payload = appliance.canonical(record)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise CacheError("cache_manifest_oversized")
    return payload


def _parse_manifest(cache: Path, expected: dict) -> dict:
    try:
        manifest = json.loads(_regular(cache / MANIFEST_NAME, MAX_MANIFEST_BYTES))
    except (CacheError, ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise CacheError("cache_manifest_invalid") from error
    if (not isinstance(manifest, dict)
            or set(manifest) != {"schema", "kind", "fingerprint", "archive"}
            or type(manifest["schema"]) is not int or manifest["schema"] != SCHEMA
            or manifest["kind"] != KIND
            or manifest["fingerprint"] != expected):
        raise CacheError("cache_fingerprint_mismatch")
    archive = manifest["archive"]
    if (not isinstance(archive, dict) or set(archive) != {"name", "sha256", "size"}
            or archive["name"] != ARCHIVE_NAME
            or not isinstance(archive["sha256"], str)
            or len(archive["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in archive["sha256"])
            or type(archive["size"]) is not int or not 0 < archive["size"] <= MAX_ARCHIVE_BYTES):
        raise CacheError("cache_manifest_invalid")
    return manifest


def _member_name(name: str) -> str:
    if not isinstance(name, str) or "\0" in name:
        raise CacheError("cache_member_name")
    # Tar member names are POSIX paths even on the host that runs the build.
    # Reject lexical escapes before any extraction tool sees the archive.
    path = PurePosixPath(name)
    if path.is_absolute():
        raise CacheError("cache_member_absolute")
    parts = [part for part in path.parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise CacheError("cache_member_traversal")
    return "." if not parts else "/".join(parts)


def _validate_archive(path: Path, *, forbidden_roots: tuple[str, ...] = FORBIDDEN_ROOTS,
                      reject_private: bool = False) -> None:
    names: dict[str, tarfile.TarInfo] = {}
    total = 0
    try:
        with tarfile.open(path, mode="r:") as archive:
            for index, member in enumerate(archive):
                if index >= MAX_MEMBERS:
                    raise CacheError("cache_member_limit")
                name = _member_name(member.name)
                if any(name == forbidden or name.startswith(forbidden + "/")
                       for forbidden in forbidden_roots):
                    raise CacheError("cache_configured_root")
                if name in names:
                    raise CacheError("cache_member_duplicate")
                if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                    raise CacheError("cache_member_size")
                if member.isreg() or member.islnk():
                    total += member.size
                    if total > MAX_ROOT_BYTES:
                        raise CacheError("cache_root_size")
                if member.islnk():
                    target = _member_name(member.linkname)
                    if target == ".":
                        raise CacheError("cache_hardlink_target")
                    member.linkname = target
                elif member.issym() or member.isdir() or member.isreg() or member.isdev() or member.isfifo():
                    pass
                else:
                    raise CacheError("cache_member_type")
                if reject_private:
                    _reject_private_member(name, member, archive)
                names[name] = member
    except CacheError:
        raise
    except (OSError, tarfile.TarError, EOFError) as error:
        raise CacheError("cache_archive_invalid") from error

    symlinks = {name for name, member in names.items() if member.issym()}
    regulars = {name for name, member in names.items() if member.isreg()}
    root = names.get(".")
    if root is None or not root.isdir():
        raise CacheError("cache_root_entry")
    for name, member in names.items():
        parent = PurePosixPath(name).parent
        while str(parent) not in ("", "."):
            if str(parent) in symlinks:
                raise CacheError("cache_symlink_parent")
            parent = parent.parent
        if member.islnk() and member.linkname not in regulars:
            raise CacheError("cache_hardlink_target")


def _reject_private_member(name: str, member: tarfile.TarInfo, archive: tarfile.TarFile) -> None:
    """Reject deployment secrets and image-generated host keys before extraction."""
    parts = PurePosixPath(name).parts
    secret_path = (any(part in {".ssh", ".gnupg"} for part in parts)
                   or any(name == prefix or name.startswith(prefix + "/")
                          for prefix in ("etc/ssl/private", "private", "deployment")))
    if secret_path and not member.isdir():
        raise CacheError(f"archive_private_material:{name!r}")
    if member.isreg():
        if PurePosixPath(name).name.startswith("ssh_host_"):
            raise CacheError(f"archive_private_material:{name!r}")
        stream = archive.extractfile(member)
        if stream is not None:
            with stream:
                prefix = stream.read(4096).lstrip()
            if prefix.startswith(b"-----BEGIN ") and b"PRIVATE KEY-----" in prefix:
                raise CacheError(f"archive_private_material:{name!r}")


def create_archive(root: Path, archive: Path, *,
                   forbidden_roots: tuple[str, ...] = FORBIDDEN_ROOTS,
                   reject_private: bool = False) -> dict:
    """Create one bounded metadata-preserving archive and validate its topology."""
    if root.is_symlink() or not root.is_dir() or archive.exists() or archive.is_symlink():
        raise CacheError("cache_root_invalid")
    appliance.run(["tar", "--numeric-owner", "--xattrs", "--xattrs-include=*", "--acls",
                   "--create", "--file", str(archive), "--directory", str(root), "."],
                  timeout=900)
    record = _checked_archive(archive)
    _validate_archive(archive, forbidden_roots=forbidden_roots, reject_private=reject_private)
    return record


def extract_archive(archive: Path, destination: Path) -> None:
    """Extract a previously validated archive to an owned empty directory."""
    appliance.run(["tar", "--numeric-owner", "--same-owner", "--same-permissions",
                   "--xattrs", "--xattrs-include=*", "--acls", "--extract", "--file",
                   str(archive), "--directory", str(destination)], timeout=900)


def _reject_configured_root(root: Path) -> None:
    for relative in FORBIDDEN_ROOTS:
        path = root / relative
        if path.exists() or path.is_symlink():
            raise CacheError("cache_configured_root")


def publish(root: Path, cache: Path, expected: dict) -> dict:
    """Archive a pristine extracted root into a new cache directory atomically."""
    root, cache = Path(root).absolute(), Path(cache).absolute()
    appliance.outside_git(cache)
    if root.is_symlink() or not root.is_dir():
        raise CacheError("cache_root_invalid")
    root = root.resolve(strict=True)
    if cache == root or root in cache.parents:
        raise CacheError("cache_inside_root")
    _reject_configured_root(root)
    if cache.exists() or cache.is_symlink():
        raise CacheError("cache_exists")
    cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".photo-wall-base-cache-", dir=cache.parent))
    try:
        archive = temporary / ARCHIVE_NAME
        archive_record = create_archive(root, archive)
        manifest = {"schema": SCHEMA, "kind": KIND, "fingerprint": expected,
                    "archive": {"name": ARCHIVE_NAME, **archive_record}}
        (temporary / MANIFEST_NAME).write_bytes(_manifest_bytes(manifest))
        os.replace(temporary, cache)
        return {"requested": True, "hit": False, "published": True,
                "fingerprint": expected, "archive": manifest["archive"]}
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def restore(cache: Path, destination: Path, expected: dict) -> dict:
    """Validate and extract a cache into a fresh destination, or report a miss."""
    cache, destination = Path(cache).absolute(), Path(destination).absolute()
    try:
        appliance.outside_git(destination)
        if cache.is_symlink() or not cache.is_dir():
            raise CacheError("cache_missing")
        manifest = _parse_manifest(cache, expected)
        archive = cache / manifest["archive"]["name"]
        archive_record = _checked_archive(archive)
        if archive_record != {key: manifest["archive"][key] for key in ("sha256", "size")}:
            raise CacheError("cache_archive_identity")
        _validate_archive(archive)
    except (CacheError, OSError, ValueError) as error:
        return {"requested": True, "hit": False, "published": False,
                "fingerprint": expected,
                "reason": str(error) if isinstance(error, CacheError) else type(error).__name__}
    if destination.exists() or destination.is_symlink():
        raise CacheError("cache_destination_exists")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".photo-wall-base-restore-", dir=destination.parent))
    try:
        extract_archive(archive, temporary)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        return {"requested": True, "hit": False, "published": False,
                "fingerprint": expected, "reason": "cache_restore_failed"}
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"requested": True, "hit": True, "published": False,
            "fingerprint": expected, "archive": manifest["archive"]}
