"""Bounded sealed-root validation and extraction. Never executes package code."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from contracts.app_environment import AppEnvironmentRefV2
from contracts.strict_json import loads_object

MAX_FILES = 150000
MAX_EXPANDED = 8 * 1024**3
MANIFEST = "environment.json"
FORMAT = "pw-debian-root-v2"
ROOTFS_DIRECTORY_MODE = 0o755


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or ".." in path.parts or not path.parts or any(ord(c) < 32 for c in name):
        raise ValueError("environment_member_path")
    return path


def resolve_member(name: str, links: dict[str, str], chain: tuple[str, ...] = ()) -> str:
    """Resolve archive link graphs with chroot semantics, without touching host paths."""
    parts = list(PurePosixPath(name).parts)
    resolved = []
    while parts:
        part = parts.pop(0)
        if part in ("", ".", "/"):
            continue
        if part == "..":
            if not resolved:
                raise ValueError("environment_link_escape")
            resolved.pop()
            continue
        resolved.append(part)
        key = "/".join(resolved)
        if key in links:
            if key in chain or len(chain) > 64:
                raise ValueError("environment_link_cycle")
            target = links[key]
            base = [] if target.startswith("/") else resolved[:-1]
            return resolve_member("/".join(base + list(PurePosixPath(target.lstrip("/")).parts) + parts), links, (*chain, key))
    return "/".join(resolved)


def rootfs_directories(root: Path):
    """Yield real directories without following sealed absolute or relative links."""
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("environment_root_invalid")
    yield root
    for directory, names, _ in os.walk(root, followlinks=False):
        for name in names:
            path = Path(directory) / name
            if stat.S_ISDIR(path.lstat().st_mode):
                yield path


def normalize_rootfs_directories(root: Path) -> None:
    """Set the format's directory mode only on a private, unpublished rootfs."""
    for path in rootfs_directories(root):
        path.chmod(ROOTFS_DIRECTORY_MODE, follow_symlinks=False)


def inventory(root: Path) -> dict[str, dict]:
    """Inventory regular bytes and safe in-root links without following host paths."""
    result = {}
    total = 0
    paths = []
    for path in root.rglob("*"):
        paths.append(path)
        if len(paths) > MAX_FILES:
            raise ValueError("environment_capacity")
    paths.sort()
    links = {path.relative_to(root).as_posix(): os.readlink(path) for path in paths if path.is_symlink()}
    for name in links:
        resolve_member(name, links)  # Containment/cycles, not optional target existence.
    for path in paths:
        relative = path.relative_to(root).as_posix()
        safe_name(relative)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            result[relative] = {"link": links[relative]}
            continue
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or info.st_mode & 0o6000:
            raise ValueError("environment_unsafe_member")
        if hasattr(os, "listxattr"):
            try:
                attributes = os.listxattr(path, follow_symlinks=False)
            except OSError as error:
                if error.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
                    raise
                attributes = []  # This filesystem cannot store extended attributes.
            if attributes:
                raise ValueError("environment_xattrs")
        if info.st_mode & 0o022:
            raise ValueError("environment_writable_member")
        if path.is_file():
            total += info.st_size
            result[relative] = {"sha256": file_sha256(path), "size": info.st_size,
                                "mode": stat.S_IMODE(info.st_mode)}
        if total > MAX_EXPANDED or len(result) > MAX_FILES:
            raise ValueError("environment_capacity")
    return result


def capacity(files: dict[str, dict]) -> dict[str, int]:
    return {"file_count": sum("size" in item for item in files.values()),
            "link_count": sum("link" in item for item in files.values()),
            "expanded_bytes": sum(item.get("size", 0) for item in files.values())}


def verify_root(directory: Path, environment: AppEnvironmentRefV2, *,
                base_abi: str, graphics_abi: str, plugin_abi: str,
                owner_uid: int = 0) -> Path:
    if directory.is_symlink() or directory.stat().st_uid != owner_uid or directory.stat().st_mode & 0o022:
        raise ValueError("environment_root_ownership")
    for name in (MANIFEST, "dependency-lock.json", "sources.json"):
        metadata_path = directory / name
        info = metadata_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022 or info.st_size > 32 * 1024**2:
            raise ValueError("environment_metadata_ownership_or_bound")
    metadata = loads_object((directory / MANIFEST).read_bytes(), max_bytes=32 * 1024**2)
    if metadata is None or set(metadata) != {"schema", "format", "reference", "files", "capacity"} or metadata["schema"] != 2 or metadata["format"] != FORMAT:
        raise ValueError("environment_manifest_invalid")
    # Outer bytes cannot contain their own digest/size; all other release facts must match.
    reference = asdict(environment)
    for name in ("environment_sha256", "size_bytes"):
        reference.pop(name)
    if metadata["reference"] != reference:
        raise ValueError("environment_reference_mismatch")
    if (environment.base_abi, environment.graphics_abi, environment.plugin_abi) != (base_abi, graphics_abi, plugin_abi):
        raise ValueError("environment_abi_mismatch")
    root = directory / "rootfs"
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("environment_root_invalid")
    if root_info.st_uid != owner_uid:
        raise ValueError("environment_member_ownership")
    for path in rootfs_directories(root):
        if stat.S_IMODE(path.lstat().st_mode) != ROOTFS_DIRECTORY_MODE:
            raise ValueError("environment_directory_mode")
    files = inventory(root)
    if files != metadata["files"] or capacity(files) != metadata["capacity"]:
        raise ValueError("environment_root_digest_mismatch")
    for path in root.rglob("*"):
        if path.lstat().st_uid != owner_uid:
            raise ValueError("environment_member_ownership")
    if file_sha256(directory / "dependency-lock.json") != environment.dependency_lock_sha256 or file_sha256(directory / "sources.json") != environment.source_snapshot_sha256:
        raise ValueError("environment_provenance_mismatch")
    links = {name: item["link"] for name, item in files.items() if "link" in item}
    # Resolve required runtime entry paths using archive/chroot semantics, never
    # Path.resolve(), which could follow an absolute link into the build host.
    required = (environment.entry_point.lstrip("/"), "usr/bin/python3")
    for name in required:
        target = files.get(resolve_member(name, links))
        if target is None or "size" not in target or not target["mode"] & 0o111:
            raise ValueError("environment_entrypoint_missing")
    return root


class _MeasuredStream:
    def __init__(self, stream, limit: int):
        self.stream, self.limit = stream, limit
        self.count = 0
        self.hasher = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.count += len(data)
        if self.count > self.limit:
            raise ValueError("environment_archive_size")
        self.hasher.update(data)
        return data


def stage_archive(archive: Path, roots: Path, environment: AppEnvironmentRefV2, *,
                  base_abi: str, graphics_abi: str, plugin_abi: str,
                  owner_uid: int = 0) -> Path:
    """Extract exact release bytes to a new root; publish only after full verification."""
    if archive.is_symlink() or archive.stat().st_size != environment.size_bytes:
        raise ValueError("environment_archive_digest_mismatch")
    if roots.is_symlink() or roots.stat().st_uid != owner_uid or roots.stat().st_mode & 0o022:
        raise ValueError("environment_store_ownership")
    target = roots / environment.environment_sha256
    abi = dict(base_abi=base_abi, graphics_abi=graphics_abi, plugin_abi=plugin_abi, owner_uid=owner_uid)
    if target.exists():
        verify_root(target, environment, **abi)
        return target
    work = Path(tempfile.mkdtemp(prefix=".stage-", dir=roots))
    try:
        # Hash exactly the bytes consumed by the streaming parser. Never hash then
        # reopen or seek in mutable manager input. The unpublished root is discarded
        # unless this same stream matches the release digest and full manifest.
        source_fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW)
        names: set[str] = set()
        links: dict[str, str] = {}
        total = 0
        with os.fdopen(source_fd, "rb") as source:
            measured = _MeasuredStream(source, environment.size_bytes)
            with tarfile.open(fileobj=measured, mode="r|") as stream:
                for member in stream:
                    name = safe_name(member.name)
                    if member.name in names or len(names) >= MAX_FILES or not (member.isdir() or member.isfile() or member.issym()) or (not member.issym() and member.mode & 0o6022) or member.pax_headers or member.issparse():
                        raise ValueError("environment_archive_member")
                    if name.parts[0] not in ("rootfs", MANIFEST, "dependency-lock.json", "sources.json") or (name.parts[0] != "rootfs" and (len(name.parts) != 1 or not member.isfile())):
                        raise ValueError("environment_archive_layout")
                    if name.parts[0] != "rootfs" and member.size > 32 * 1024**2:
                        raise ValueError("environment_metadata_bound")
                    names.add(member.name)
                    if member.issym():
                        links[member.name] = member.linkname
                        continue
                    total += member.size
                    if total > MAX_EXPANDED or total > environment.size_bytes:
                        raise ValueError("environment_archive_capacity")
                    destination = work.joinpath(*name.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    if member.isdir():
                        destination.mkdir(exist_ok=True, mode=0o755)
                    else:
                        content = stream.extractfile(member)
                        if content is None:
                            raise ValueError("environment_archive_missing_data")
                        with content, destination.open("xb") as out:
                            shutil.copyfileobj(content, out, 1024 * 1024)
                            out.flush()
                            os.fsync(out.fileno())
                        destination.chmod(member.mode)
            while measured.read(1024 * 1024):
                pass
            if measured.count != environment.size_bytes or measured.hasher.hexdigest() != environment.environment_sha256:
                raise ValueError("environment_archive_digest_mismatch")
        graph = {name: "/rootfs" + target if target.startswith("/") else target for name, target in links.items()}
        for name in names:
            parts = PurePosixPath(name).parts
            if any("/".join(parts[:index]) in links for index in range(1, len(parts))):
                raise ValueError("environment_archive_link_parent")
        for name, link_target in links.items():
            resolved = resolve_member(name, graph)
            if resolved != "rootfs" and not resolved.startswith("rootfs/"):
                raise ValueError("environment_archive_link")
            destination = work / name
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            destination.symlink_to(link_target)
        normalize_rootfs_directories(work / "rootfs")
        verify_root(work, environment, **abi)
        os.rename(work, target)
        fd = os.open(roots, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return target
    finally:
        if work.exists():
            shutil.rmtree(work)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
