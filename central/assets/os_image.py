"""Allowlist extraction of the base squashfs from a release's base tarball (a node offer's base).

The tarball's member layout is contracts/release.py's (`BASE_ROOT` and the names within it), which the packager
writes. Only two exact member names are read -- never `extractall` -- and only the FIRST
occurrence of each, so a hostile archive (traversal, symlink, device, duplicate name) writes
nothing. `extract_squashfs` blocks (gzip + sha256 over up to 1 GiB): call it in a thread.
"""

from __future__ import annotations

import hashlib
import os
import tarfile
from pathlib import Path
from typing import Final

from central.kernel.handling import TerminalFailure
from central.kernel.types import require_sha256
from contracts.release import BASE_CHECKSUMS, BASE_SQUASHFS, MAX_ROOTFS_BYTES, base_member

MAX_SQUASHFS_BYTES: Final = MAX_ROOTFS_BYTES  # the same bound the initrd fetch enforces

_SQUASHFS_MEMBER: Final = base_member(BASE_SQUASHFS)
_SUMS_MEMBER: Final = base_member(BASE_CHECKSUMS)
_MAX_SUMS_BYTES: Final = 4 * 1024 * 1024
_CHUNK: Final = 1024 * 1024


def _first_member(tar: tarfile.TarFile, name: str) -> tarfile.TarInfo | None:
    """The FIRST member named exactly `name`; later duplicates are ignored."""
    for member in tar.getmembers():
        if member.name == name:
            return member
    return None


def _squashfs_digest(sums: bytes) -> str | None:
    """The squashfs sha256 listed in `SHA256SUMS` (`<hex>  <name>`, names maybe `./`-prefixed)."""
    for line in sums.decode("utf-8", "replace").splitlines():
        digest, _, name = line.partition("  ")
        if name.strip().lstrip("./") != BASE_SQUASHFS:
            continue
        try:
            return require_sha256(digest)
        except ValueError:
            continue
    return None


def extract_squashfs(tarball: Path, into: Path) -> None:
    """Extract and verify the squashfs from `tarball` into `into`, created exclusively.

    Raises `TerminalFailure` with `base_member_missing`, `base_member_not_file`, `base_too_large`,
    `base_sums_too_large`, `base_sums_no_squashfs` or `base_digest_mismatch` for a hostile or
    inconsistent archive. On any failure, a file this call created at `into` is removed.
    """
    with tarfile.open(tarball, "r:gz") as tar:
        squashfs = _first_member(tar, _SQUASHFS_MEMBER)
        sums = _first_member(tar, _SUMS_MEMBER)
        if squashfs is None or sums is None:
            raise TerminalFailure("base_member_missing")
        if not squashfs.isfile() or not sums.isfile():
            raise TerminalFailure("base_member_not_file")  # symlink/hardlink/device/fifo refused
        if squashfs.size > MAX_SQUASHFS_BYTES:
            raise TerminalFailure("base_too_large")
        if sums.size > _MAX_SUMS_BYTES:
            raise TerminalFailure("base_sums_too_large")

        sums_handle = tar.extractfile(sums)
        sums_bytes = sums_handle.read(_MAX_SUMS_BYTES + 1) if sums_handle else b""
        if len(sums_bytes) > _MAX_SUMS_BYTES:
            raise TerminalFailure("base_sums_too_large")
        expected = _squashfs_digest(sums_bytes)
        if expected is None:
            raise TerminalFailure("base_sums_no_squashfs")

        descriptor = os.open(into, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        try:
            digest = hashlib.sha256()
            size = 0
            source = tar.extractfile(squashfs)
            with os.fdopen(descriptor, "wb") as output:
                while chunk := (source.read(_CHUNK) if source else b""):
                    size += len(chunk)
                    if size > MAX_SQUASHFS_BYTES:
                        raise TerminalFailure("base_too_large")
                    output.write(chunk)
                    digest.update(chunk)
                if digest.hexdigest() != expected:
                    raise TerminalFailure("base_digest_mismatch")
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            Path(into).unlink(missing_ok=True)
            raise
