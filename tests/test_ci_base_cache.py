"""Focused safety and hit-path tests for the extracted Ubuntu base cache."""

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts import ci_base_cache


def _has_gnu_tar() -> bool:
    try:
        result = subprocess.run(["tar", "--version"], capture_output=True, text=True,
                                check=False)
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.startswith("tar (GNU tar)")


gnu_tar = pytest.mark.skipif(not _has_gnu_tar(), reason="cache archive requires GNU tar")


def _expected() -> dict:
    return ci_base_cache.fingerprint(Path.cwd(), runner_arch="test-arch")


def _root(path: Path) -> Path:
    path.mkdir()
    (path / "usr/bin").mkdir(parents=True)
    (path / "usr/bin/tool").write_bytes(b"tool\n")
    (path / "usr/bin/copy").hardlink_to(path / "usr/bin/tool")
    (path / "usr/lib-link").symlink_to("/usr/lib")
    (path / "var").mkdir()
    (path / "var/empty").mkdir()
    return path


@gnu_tar
def test_publish_restore_preserves_hardlinks_modes_and_absolute_symlinks(tmp_path):
    source = _root(tmp_path / "source")
    (source / "usr/bin/tool").chmod(0o751)
    cache = tmp_path / "cache"
    published = ci_base_cache.publish(source, cache, _expected())
    assert published["published"] is True
    restored = tmp_path / "restored"
    result = ci_base_cache.restore(cache, restored, _expected())
    assert result["hit"] is True
    assert (restored / "usr/bin/tool").read_bytes() == b"tool\n"
    assert stat.S_IMODE((restored / "usr/bin/tool").stat().st_mode) == 0o751
    assert (restored / "usr/bin/tool").stat().st_ino == (restored / "usr/bin/copy").stat().st_ino
    assert os.readlink(restored / "usr/lib-link") == "/usr/lib"


@gnu_tar
def test_stale_fingerprint_and_corrupt_archive_are_misses(tmp_path):
    source = _root(tmp_path / "source")
    cache = tmp_path / "cache"
    ci_base_cache.publish(source, cache, _expected())
    stale = dict(_expected(), runner_arch="other-arch")
    assert ci_base_cache.restore(cache, tmp_path / "stale", stale)["hit"] is False
    archive = cache / ci_base_cache.ARCHIVE_NAME
    archive.write_bytes(archive.read_bytes() + b"corrupt")
    result = ci_base_cache.restore(cache, tmp_path / "corrupt", _expected())
    assert result["hit"] is False
    assert not (tmp_path / "corrupt").exists()


def _write_manifest_cache(cache: Path, expected: dict, members: list[tarfile.TarInfo],
                          data: dict[str, bytes] | None = None) -> None:
    cache.mkdir()
    archive = cache / ci_base_cache.ARCHIVE_NAME
    with tarfile.open(archive, "w") as output:
        for member in members:
            payload = None if data is None else io.BytesIO(data.get(member.name, b""))
            output.addfile(member, payload)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (cache / ci_base_cache.MANIFEST_NAME).write_bytes(json.dumps({
        "schema": ci_base_cache.SCHEMA, "kind": ci_base_cache.KIND,
        "fingerprint": expected,
        "archive": {"name": ci_base_cache.ARCHIVE_NAME, "sha256": digest,
                    "size": archive.stat().st_size},
    }).encode())


def test_traversal_and_symlink_parent_archives_are_rejected(tmp_path):
    expected = _expected()
    traversal = tarfile.TarInfo("../escape")
    traversal.size = 0
    cache = tmp_path / "traversal"
    _write_manifest_cache(cache, expected, [traversal])
    assert ci_base_cache.restore(cache, tmp_path / "out", expected)["hit"] is False

    root = tarfile.TarInfo(".")
    root.type = tarfile.DIRTYPE
    link = tarfile.TarInfo("etc")
    link.type = tarfile.SYMTYPE
    link.linkname = "/tmp"
    child = tarfile.TarInfo("etc/child")
    child.size = 0
    cache = tmp_path / "symlink-parent"
    _write_manifest_cache(cache, expected, [root, link, child])
    result = ci_base_cache.restore(cache, tmp_path / "out2", expected)
    assert result["hit"] is False
    assert result["reason"] == "cache_symlink_parent"


def test_private_configured_root_is_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "etc/photo-wall").mkdir(parents=True)
    with pytest.raises(ci_base_cache.CacheError, match="cache_configured_root"):
        ci_base_cache.publish(source, tmp_path / "cache", _expected())


@gnu_tar
@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_restore_tool_failure_cleans_partial_root_and_reports_miss(tmp_path, monkeypatch, failure):
    source = _root(tmp_path / "source")
    cache = tmp_path / "cache"
    expected = ci_base_cache.fingerprint(Path.cwd())
    ci_base_cache.publish(source, cache, expected)

    def fail(*args, **kwargs):
        temporary = next(tmp_path.glob(".photo-wall-base-restore-*"), None)
        if temporary is not None:
            (temporary / "partial").write_bytes(b"must be removed")
        raise failure("injected restore failure")

    monkeypatch.setattr(ci_base_cache.appliance, "run", fail)
    destination = tmp_path / "restored"
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            ci_base_cache.restore(cache, destination, expected)
    else:
        result = ci_base_cache.restore(cache, destination, expected)
        assert result["hit"] is False
        assert result["reason"] == "cache_restore_failed"
    assert not destination.exists()
    assert not list(tmp_path.glob(".photo-wall-base-restore-*"))


def test_external_archive_configured_root_is_rejected(tmp_path):
    expected = _expected()
    root = tarfile.TarInfo(".")
    root.type = tarfile.DIRTYPE
    configured = tarfile.TarInfo("etc/photo-wall/config")
    configured.size = 1
    cache = tmp_path / "cache"
    _write_manifest_cache(cache, expected, [root, configured], {configured.name: b"x"})
    result = ci_base_cache.restore(cache, tmp_path / "out", expected)
    assert result["hit"] is False
    assert result["reason"] == "cache_configured_root"


def test_nested_cache_path_is_rejected_before_creation(tmp_path):
    source = _root(tmp_path / "source")
    cache = source / "cache"
    with pytest.raises(ci_base_cache.CacheError, match="cache_inside_root"):
        ci_base_cache.publish(source, cache, _expected())
    assert not cache.exists()
    assert not list(source.glob(".photo-wall-base-cache-*"))


def test_cache_publish_failure_keeps_cold_root_usable(tmp_path, monkeypatch):
    from scripts import build_ci_image

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    cache = tmp_path / "cache"

    def fail_publish(*args, **kwargs):
        raise ci_base_cache.CacheError("injected_publish_failure")

    def fake_phase(name, function, *args, **kwargs):
        if name == "fetch_ubuntu":
            input_dir = Path(args[0][-1])
            input_dir.mkdir(parents=True, exist_ok=True)
            (input_dir / build_ci_image.fetch_ubuntu.IMAGE).write_bytes(b"verified-input")
            return None
        if name == "decompress":
            Path(args[1]).write_bytes(b"raw-image")
            return None
        if name == "extract":
            root = Path(args[0][-1])
            root.mkdir(parents=True)
            (root / "pristine-marker").write_bytes(b"yes")
            return None
        return function(*args, **kwargs)

    monkeypatch.setattr(build_ci_image, "phase", fake_phase)
    monkeypatch.setattr(ci_base_cache, "publish", fail_publish)
    root, result = build_ci_image._prepare_base_root(
        workspace, Path.cwd(), diagnostics, base_cache=None,
        extracted_base_cache=cache)
    assert (root / "pristine-marker").read_bytes() == b"yes"
    assert result["hit"] is False
    assert result["published"] is False
    assert result["reason"] == "cache_publish_failed"


@gnu_tar
def test_linux_metadata_fixture_preserves_xattrs_acl_hardlinks_and_device(tmp_path):
    """Exercise the GNU tar path used by the Linux builder, including root metadata."""
    if os.geteuid() != 0 or not shutil.which("setfacl") or not shutil.which("getfacl"):
        pytest.skip("requires Linux root with ACL tools")
    source = _root(tmp_path / "source")
    target = source / "usr/bin/tool"
    try:
        os.chown(target, 1234, 2345)
        os.setxattr(target, "user.photo-wall-cache", b"preserved")
        subprocess.run(["setfacl", "-m", "u:1234:r--", str(target)], check=True,
                       capture_output=True)
        device = source / "dev-test"
        os.mknod(device, stat.S_IFCHR | 0o600, os.makedev(1, 7))
    except (OSError, subprocess.SubprocessError) as error:
        pytest.skip(f"metadata fixture unavailable: {error}")
    cache = tmp_path / "cache"
    ci_base_cache.publish(source, cache, _expected())
    restored = tmp_path / "restored"
    assert ci_base_cache.restore(cache, restored, _expected())["hit"] is True
    assert (restored / "usr/bin/tool").stat().st_uid == 1234
    assert (restored / "usr/bin/tool").stat().st_gid == 2345
    assert os.getxattr(restored / "usr/bin/tool", "user.photo-wall-cache") == b"preserved"
    original_acl = subprocess.run(["getfacl", "-cp", str(target)], capture_output=True,
                                  text=True, check=True).stdout
    restored_acl = subprocess.run(["getfacl", "-cp", str(restored / "usr/bin/tool")],
                                  capture_output=True, text=True, check=True).stdout
    assert "user:1234:r--" in original_acl
    assert "user:1234:r--" in restored_acl
    assert stat.S_ISCHR((restored / "dev-test").stat().st_mode)
    assert os.major((restored / "dev-test").stat().st_rdev) == 1
    assert os.minor((restored / "dev-test").stat().st_rdev) == 7


@gnu_tar
def test_cache_hit_path_skips_fetch_decompress_and_extract(tmp_path, monkeypatch):
    from scripts import build_ci_image

    source = _root(tmp_path / "source")
    cache = tmp_path / "cache"
    expected = ci_base_cache.fingerprint(Path.cwd())
    ci_base_cache.publish(source, cache, expected)
    calls = []

    original_phase = build_ci_image.phase

    def phase(name, function, *args, **kwargs):
        if name in {"fetch_ubuntu", "decompress", "extract"}:
            calls.append(name)
            raise AssertionError("cold-build operation reached on cache hit")
        return original_phase(name, function, *args, **kwargs)

    monkeypatch.setattr(build_ci_image, "phase", phase)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    restored, result = build_ci_image._prepare_base_root(
        workspace, Path.cwd(), diagnostics, base_cache=None,
        extracted_base_cache=cache)
    assert result["hit"] is True
    assert (restored / "usr/bin/tool").read_bytes() == b"tool\n"
    assert calls == []
