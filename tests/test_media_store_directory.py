"""`_directory` tolerates platform-owned storage; no PostgreSQL fixture needed.

MediaStore._directory is a pure filesystem staticmethod, so these exercise the
ownership tolerance that keeps a worker handed a root it does not own from
crashing at startup with `media_io`. The load-bearing deployment path is a
Kubernetes fsGroup mount: the media root is owned by uid 0, group-owned by the
pod gid, mode 0770 with the setgid bit, and no OTHER bits -- the worker (euid
10001) does not own it but writes through the group.
"""

import os
import stat

import pytest

from central.media_store import MediaStore, MediaStoreError


def _fake_lstat(*, mode: int, uid: int) -> os.stat_result:
    # (st_mode, st_ino, st_dev, st_nlink, st_uid, st_gid, st_size, atime, mtime, ctime)
    return os.stat_result((mode, 0, 0, 1, uid, 10001, 0, 0, 0, 0))


def test_directory_creates_missing_dir_at_0700(tmp_path):
    target = tmp_path / "media"
    MediaStore._directory(target)
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_directory_tightens_owned_dir_to_0700(tmp_path):
    # A pre-existing, group/other-accessible dir that we DO own is tightened,
    # exactly as before -- the compose path (uid 10001 owns its volume).
    target = tmp_path / "media"
    target.mkdir()
    target.chmod(0o755)
    MediaStore._directory(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_directory_skips_chmod_on_group_owned_dir_real(tmp_path, monkeypatch):
    # Real directory, group-writable, no OTHER bits, but euid is not the owner:
    # _directory must not raise and must leave the mode untouched.
    target = tmp_path / "media"
    target.mkdir()
    target.chmod(0o770)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(target).st_uid + 1)
    MediaStore._directory(target)  # must not raise
    assert stat.S_IMODE(target.stat().st_mode) == 0o770


def test_directory_tolerates_fsgroup_root_owned_setgid(tmp_path, monkeypatch):
    # The exact k8s fsGroup posture: root-owned, pod-gid group, mode 0770 with
    # setgid, no OTHER bits, euid 10001 does not own it. _directory must return
    # without raising and WITHOUT attempting a chmod it cannot perform.
    target = tmp_path / "media"
    target.mkdir()
    monkeypatch.setattr(os, "geteuid", lambda: 10001)
    monkeypatch.setattr(
        type(target), "lstat", lambda self: _fake_lstat(mode=0o42770, uid=0)
    )
    chmods: list[int] = []
    monkeypatch.setattr(type(target), "chmod", lambda self, m: chmods.append(m))
    MediaStore._directory(target)  # must not raise
    assert chmods == []  # never chmods a directory it does not own


def test_directory_rejects_other_accessible_dir_it_cannot_secure(tmp_path, monkeypatch):
    # Not owned by us AND world-accessible: we can neither tighten nor safely
    # serve it, so surface a distinct media_perms rather than a silent accept.
    target = tmp_path / "media"
    target.mkdir()
    monkeypatch.setattr(os, "geteuid", lambda: 10001)
    monkeypatch.setattr(
        type(target), "lstat", lambda self: _fake_lstat(mode=0o40775, uid=0)
    )
    with pytest.raises(MediaStoreError, match="media_perms"):
        MediaStore._directory(target)


def test_directory_rejects_symlink_masquerading_as_dir(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "media"
    link.symlink_to(real)
    with pytest.raises(MediaStoreError, match="media_path"):
        MediaStore._directory(link)
