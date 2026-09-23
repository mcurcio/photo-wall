"""`extract_squashfs` against real gzip tarballs, including hostile archives.

Every hostile case leaves no file at `into` and raises a terminal failure with its reason.
"""

from __future__ import annotations

import hashlib
import io
import tarfile

import pytest

from central.assets import os_image
from central.assets.os_image import extract_squashfs
from central.kernel.handling import TerminalFailure

SQUASHFS = b"the rpi-image-gen base squashfs payload " * 16
SQUASHFS_MEMBER = "photo-wall-base/photo-wall-base.squashfs"
SUMS_MEMBER = "photo-wall-base/SHA256SUMS"


def _sums(data: bytes = SQUASHFS, name: str = "./photo-wall-base.squashfs") -> bytes:
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()


def _file(name: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


def _special(name: str, kind: bytes, linkname: str = "") -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info, None


def _tarball(tmp_path, members) -> object:
    path = tmp_path / "base.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


def _good():
    return [_file(SQUASHFS_MEMBER, SQUASHFS), _file(SUMS_MEMBER, _sums())]


def _refused(tmp_path, members, reason):
    into = tmp_path / "out.squashfs"
    with pytest.raises(TerminalFailure) as raised:
        extract_squashfs(_tarball(tmp_path, members), into)
    assert raised.value.reason == reason
    assert not into.exists()


def test_extracts_and_verifies_the_squashfs(tmp_path):
    into = tmp_path / "out.squashfs"
    extract_squashfs(_tarball(tmp_path, _good()), into)
    assert into.read_bytes() == SQUASHFS
    assert into.stat().st_mode & 0o777 == 0o600


def test_into_is_created_exclusively_and_an_existing_file_is_left_alone(tmp_path):
    into = tmp_path / "out.squashfs"
    into.write_bytes(b"someone else's")
    with pytest.raises(FileExistsError):
        extract_squashfs(_tarball(tmp_path, _good()), into)
    assert into.read_bytes() == b"someone else's"


def test_missing_member(tmp_path):
    _refused(tmp_path, [_file(SUMS_MEMBER, _sums())], "base_member_missing")
    _refused(tmp_path, [_file(SQUASHFS_MEMBER, SQUASHFS)], "base_member_missing")


def test_traversal_names_are_never_read(tmp_path):
    # Only the two exact names are looked up, so a traversal member is simply absent.
    members = [_file("../photo-wall-base/photo-wall-base.squashfs", SQUASHFS),
               _file(SUMS_MEMBER, _sums())]
    _refused(tmp_path, members, "base_member_missing")
    members = [_file("/photo-wall-base/photo-wall-base.squashfs", SQUASHFS),
               _file(SUMS_MEMBER, _sums())]
    _refused(tmp_path, members, "base_member_missing")


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE,
                                  tarfile.BLKTYPE, tarfile.FIFOTYPE, tarfile.DIRTYPE])
def test_non_regular_squashfs_member_is_refused(tmp_path, kind):
    members = [_special(SQUASHFS_MEMBER, kind, linkname="/etc/passwd"), _file(SUMS_MEMBER, _sums())]
    _refused(tmp_path, members, "base_member_not_file")


def test_non_regular_sums_member_is_refused(tmp_path):
    members = [_file(SQUASHFS_MEMBER, SQUASHFS), _special(SUMS_MEMBER, tarfile.SYMTYPE, "/etc/x")]
    _refused(tmp_path, members, "base_member_not_file")


def test_duplicate_names_take_the_first_occurrence(tmp_path):
    # A hostile later duplicate is ignored ...
    into = tmp_path / "first.squashfs"
    members = [*_good(), _file(SQUASHFS_MEMBER, b"evil"), _special(SUMS_MEMBER, tarfile.SYMTYPE, "/x")]
    extract_squashfs(_tarball(tmp_path, members), into)
    assert into.read_bytes() == SQUASHFS
    # ... and a hostile first occurrence is not rescued by a benign duplicate.
    members = [_special(SQUASHFS_MEMBER, tarfile.SYMTYPE, "/etc/passwd"), *_good()]
    _refused(tmp_path, members, "base_member_not_file")


def test_oversize_squashfs_header_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(os_image, "MAX_SQUASHFS_BYTES", len(SQUASHFS) - 1)
    _refused(tmp_path, _good(), "base_too_large")


def test_oversize_sums_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(os_image, "_MAX_SUMS_BYTES", 10)
    _refused(tmp_path, _good(), "base_sums_too_large")


def test_sums_without_the_squashfs_line_is_refused(tmp_path):
    members = [_file(SQUASHFS_MEMBER, SQUASHFS), _file(SUMS_MEMBER, _sums(name="other.img"))]
    _refused(tmp_path, members, "base_sums_no_squashfs")
    members = [_file(SQUASHFS_MEMBER, SQUASHFS),
               _file(SUMS_MEMBER, b"NOTHEX  ./photo-wall-base.squashfs\n")]
    _refused(tmp_path, members, "base_sums_no_squashfs")


def test_wrong_digest_is_refused_and_leaves_nothing(tmp_path):
    members = [_file(SQUASHFS_MEMBER, SQUASHFS), _file(SUMS_MEMBER, _sums(b"different bytes"))]
    _refused(tmp_path, members, "base_digest_mismatch")
