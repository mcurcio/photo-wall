"""The sealed release archive's exact-stream extraction, build side only (E2c).

Moved verbatim from `appliance/apps/environment.py` when the Node stopped parsing tar: the Node
stages release roots as squashfs images (`stage_image`). The image builder
(`scripts/build_environment_image.py`) still extracts the sealed tar to build the image's tree,
verified as `verify_root` would on a Node.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from appliance.apps.environment import (
    MANIFEST,
    MAX_EXPANDED,
    MAX_FILES,
    normalize_rootfs_directories,
    resolve_member,
    safe_name,
    verify_root,
)
from contracts.app_environment import AppEnvironmentRefV2


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
