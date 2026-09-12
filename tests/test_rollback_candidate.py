"""Private rollback-candidate preparation tests."""

import hashlib
import json
from pathlib import Path

import pytest

from appliance.build import BuildError, boot_abi, canonical
from contracts.release import Release
from scripts import build_rollback_candidate as candidate

REVISION = "a" * 40
EPOCH = 1_700_000_000
PUBLIC_NAMES = ("public.json", "bootstrap.json", "ca.pem", "release.pub.pem")


def _write_config(directory: Path) -> None:
    directory.mkdir(parents=True)
    for name in PUBLIC_NAMES:
        (directory / name).write_bytes((name + "\n").encode())


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Release]:
    root, bundle, destination = (tmp_path / name for name in ("root", "bundle", "candidate"))
    (root / "usr/lib/modules/test-kernel").mkdir(parents=True)
    (root / "usr/lib/modules/test-kernel/test.ko").write_bytes(b"module")
    for relative, value in {
        "appliance/__init__.py": b"appliance init",
        "appliance/bootstrap.py": b"bootstrap",
        "appliance/updates.py": b"updates",
        "contracts/__init__.py": b"contracts init",
        "contracts/release.py": b"release",
    }.items():
        path = root / "usr/lib/python3/dist-packages" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    for relative, value in {"hooks/photo-wall": b"hook", "scripts/photowall": b"mountroot"}.items():
        path = root / "etc/initramfs-tools" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    _write_config(root / "etc/photo-wall")
    (root / "etc/photo-wall/boot-policy.json").write_bytes(b"generated policy\n")
    (root / "etc/systemd/system").mkdir(parents=True)
    source = root / "usr/share/photo-wall/build/source.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(canonical({"schema": 1, "revision": REVISION,
                                  "source_epoch": EPOCH, "files": {}}))
    boot = bundle / "boot"
    boot.mkdir(parents=True)
    (boot / "vmlinuz").write_bytes(b"kernel")
    abi, _ = boot_abi(root, boot, "test-kernel")

    public = bundle / "public"
    _write_config(public)
    base_bytes = b"base rootfs"
    base_hash = hashlib.sha256(base_bytes).hexdigest()
    base = Release(revision=REVISION, boot_abi=abi,
                   rootfs_sha256=base_hash, rootfs_size=len(base_bytes))
    (bundle / base.rootfs_name).write_bytes(base_bytes)
    build = {
        "schema": 1,
        "revision": REVISION,
        "source_epoch": EPOCH,
        "boot_abi": abi,
        "rootfs_sha256": base.rootfs_sha256,
        "rootfs_size": base.rootfs_size,
    }
    (bundle / "release.json").write_bytes(base.encode())
    (bundle / "build.json").write_bytes(canonical(build))
    return root, bundle, destination, base


@pytest.fixture
def fake_squash(monkeypatch):
    def make(_root, destination, source_epoch):
        assert source_epoch == EPOCH
        destination.write_bytes(b"candidate rootfs")
        return {"sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "size": destination.stat().st_size}

    monkeypatch.setattr(candidate, "squash", make)


def test_prepare_binds_distinct_ab_candidate_and_restores_root(tmp_path, fake_squash):
    root, bundle, destination, base = _fixture(tmp_path)
    result = candidate.prepare(root, bundle, destination)

    assert result["kind"] == "ci-rollback-candidate"
    assert result["source_revision"] == REVISION
    assert result["source_epoch"] == EPOCH
    assert result["releases"]["accepted"]["release_id"] == base.release_id
    assert result["releases"]["candidate"]["release_id"] != base.release_id
    assert result["releases"]["candidate"]["rootfs_sha256"] != base.rootfs_sha256
    assert (destination / "release.json").is_file()
    assert (destination / "candidate.json").is_file()
    assert not (root / candidate.FAULT_PATH).exists()
    assert json.loads((destination / "candidate.json").read_bytes()) == result


def test_prepare_rejects_changed_root_boot_abi(tmp_path, fake_squash, monkeypatch):
    root, bundle, _destination, _base = _fixture(tmp_path)
    monkeypatch.setattr(candidate, "boot_abi", lambda *_args: ("b" * 64, {}))
    with pytest.raises(BuildError, match="base_boot_abi_mismatch"):
        candidate.prepare(root, bundle, tmp_path / "candidate")
    assert not (root / candidate.FAULT_PATH).exists()


def test_prepare_rejects_root_configuration_missing_a_required_file(tmp_path, fake_squash):
    """0008 decision 4: config content is no longer bound to the signed release, but
    the standard public-input file set is still validated structurally."""
    root, bundle, _destination, _base = _fixture(tmp_path)
    (root / "etc/photo-wall/public.json").unlink()
    with pytest.raises(BuildError, match="root_configuration_invalid"):
        candidate.prepare(root, bundle, tmp_path / "candidate")
    assert not (root / candidate.FAULT_PATH).exists()


def test_prepare_rejects_existing_fault_directory_and_preserves_it(tmp_path, fake_squash):
    root, bundle, destination, _base = _fixture(tmp_path)
    dropdir = root / candidate.FAULT_PATH
    dropdir.parent.mkdir(parents=True)
    sentinel = dropdir / "sentinel"
    dropdir.mkdir()
    sentinel.write_bytes(b"keep")
    with pytest.raises(BuildError, match="fault_path_exists"):
        candidate.prepare(root, bundle, destination)
    assert sentinel.read_bytes() == b"keep"
    assert not destination.exists()


def test_prepare_rejects_destination_inside_root_or_via_symlink(tmp_path, fake_squash):
    root, bundle, _destination, _base = _fixture(tmp_path)
    with pytest.raises(BuildError, match="destination_inside_root"):
        candidate.prepare(root, bundle, root / "candidate")
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(BuildError, match="candidate_symlink"):
        candidate.prepare(root, bundle, linked_parent / "candidate")


def test_prepare_failure_removes_partial_candidate_and_restores_root(tmp_path, monkeypatch):
    root, bundle, destination, _base = _fixture(tmp_path)

    def fail(_root, path, _epoch):
        path.write_bytes(b"partial")
        raise BuildError("tool_failed")

    monkeypatch.setattr(candidate, "squash", fail)
    with pytest.raises(BuildError, match="tool_failed"):
        candidate.prepare(root, bundle, destination)
    assert not (root / candidate.FAULT_PATH).exists()
    assert not destination.exists()


def test_exclusive_writer_rejects_zero_progress_and_removes_its_file(tmp_path, monkeypatch):
    path = tmp_path / "file"
    monkeypatch.setattr(candidate.os, "write", lambda *_args: 0)
    with pytest.raises(BuildError, match="candidate_write"):
        candidate._write_exclusive(path, b"payload")
    assert not path.exists()


def test_dropin_collision_is_not_removed_as_owned(tmp_path, monkeypatch):
    root, bundle, destination, _base = _fixture(tmp_path)

    def collide(path, _payload):
        path.write_bytes(b"preexisting race")
        raise BuildError("fault_collision")

    monkeypatch.setattr(candidate, "_write_exclusive", collide)
    with pytest.raises(BuildError, match="fault_collision"):
        candidate.prepare(root, bundle, destination)
    collision = root / candidate.FAULT_PATH
    assert collision.read_bytes() == b"preexisting race"
    collision.unlink()
    collision.parent.rmdir()
