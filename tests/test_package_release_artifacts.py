"""Operator release-artifact packaging: presence checks and hash correctness.

0009: the published set is the base OS image tarball + the Player `.deb`
(+ optional flash `.img`) -- no signed rootfs, no `release.pub.pem`.
"""

from __future__ import annotations

import hashlib
import tarfile

import pytest

from scripts.package_release_artifacts import PackagingError, package

REVISION = "a" * 40


def _write(path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _synthetic_base_image(root):
    """Build a fake output tree shaped like `scripts/build_ci_base_image.py`'s."""
    base = root / "base-image"
    _write(base / f"photo-wall-base-{REVISION}.squashfs", b"fake-base-squashfs-bytes")
    _write(base / "boot" / "config.txt", b"[all]\narm_64bit=1\n")
    _write(base / "boot" / "vmlinuz", b"fake-kernel-bytes")
    _write(base / "ci-base-image.json", b'{"schema":1}\n')
    return base


def _synthetic_player_deb(root):
    path = root / "photo-wall-player_0.1.0+gdeadbeef_arm64.deb"
    _write(path, b"fake-deb-bytes" * 100)
    return path


def _synthetic_flash_image(root):
    path = root / "flash" / f"photo-wall-flash-{REVISION}.img"
    _write(path, b"fake-flash-disk-bytes" * 1000)
    return path


def test_package_produces_expected_base_and_deb_contents(tmp_path):
    base_image = _synthetic_base_image(tmp_path)
    player_deb = _synthetic_player_deb(tmp_path)
    destination = tmp_path / "release"

    result = package(base_image, player_deb, destination, revision=REVISION)

    assert result["revision"] == REVISION
    base_tarball = destination / f"photo-wall-base-{REVISION}.tar.gz"
    assert base_tarball.is_file()
    with tarfile.open(base_tarball) as archive:
        names = set(archive.getnames())
    assert f"photo-wall-base/photo-wall-base-{REVISION}.squashfs" in names
    assert "photo-wall-base/boot/config.txt" in names
    assert "photo-wall-base/boot/vmlinuz" in names
    assert "photo-wall-base/ci-base-image.json" in names

    assert (destination / player_deb.name).is_file()
    assert (destination / player_deb.name).read_bytes() == player_deb.read_bytes()

    assert result["base_image"]["filename"] == base_tarball.name
    assert result["player_deb"]["filename"] == player_deb.name
    assert result["player_deb"]["version"] == "0.1.0+gdeadbeef"
    assert result["flash_image"] is None
    assert not (destination / f"photo-wall-flash-{REVISION}.img.xz").exists()

    # No signature/trust-anchor artifacts anywhere in the published set (0009:
    # UX over security, no signing).
    for path in destination.iterdir():
        assert "release.pub.pem" not in path.name
        assert not path.name.endswith(".sig")

    # No leftover staging directory.
    assert not (destination / ".staging").exists()


def test_package_includes_optional_flash_image(tmp_path):
    base_image = _synthetic_base_image(tmp_path)
    player_deb = _synthetic_player_deb(tmp_path)
    flash_image = _synthetic_flash_image(tmp_path)
    destination = tmp_path / "release"

    result = package(base_image, player_deb, destination, revision=REVISION, flash_image=flash_image)

    flash_name = f"photo-wall-flash-{REVISION}.img.xz"
    assert (destination / flash_name).is_file()
    assert (destination / f"{flash_name}.sha256").is_file()
    sha_line = (destination / f"{flash_name}.sha256").read_text()
    assert sha_line == f"{result['flash_image']['sha256']}  {flash_name}\n"


def test_sha256sums_matches_actual_produced_bytes(tmp_path):
    """Mutation probe: corrupting a produced artifact must break this check."""
    base_image = _synthetic_base_image(tmp_path)
    player_deb = _synthetic_player_deb(tmp_path)
    destination = tmp_path / "release"
    package(base_image, player_deb, destination, revision=REVISION)

    sums_path = destination / "SHA256SUMS"
    entries = {}
    for line in sums_path.read_text().splitlines():
        digest, name = line.split("  ", 1)
        entries[name] = digest

    assert entries.keys() == {
        path.name for path in destination.iterdir() if path.is_file() and path.name != "SHA256SUMS"
    }
    for name, digest in entries.items():
        actual = hashlib.sha256((destination / name).read_bytes()).hexdigest()
        assert actual == digest, f"SHA256SUMS entry for {name} does not match its real bytes"


def test_manifest_shas_match_actual_produced_bytes(tmp_path):
    """Mutation probe: corrupting a produced artifact must break the manifest too."""
    base_image = _synthetic_base_image(tmp_path)
    player_deb = _synthetic_player_deb(tmp_path)
    destination = tmp_path / "release"
    result = package(base_image, player_deb, destination, revision=REVISION)

    base_tarball = destination / result["base_image"]["filename"]
    actual = hashlib.sha256(base_tarball.read_bytes()).hexdigest()
    assert actual == result["base_image"]["sha256"]

    deb = destination / result["player_deb"]["filename"]
    actual = hashlib.sha256(deb.read_bytes()).hexdigest()
    assert actual == result["player_deb"]["sha256"]


def test_missing_base_squashfs_fails_clearly(tmp_path):
    base_image = _synthetic_base_image(tmp_path)
    (base_image / f"photo-wall-base-{REVISION}.squashfs").unlink()
    player_deb = _synthetic_player_deb(tmp_path)
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="base_squashfs_missing"):
        package(base_image, player_deb, destination, revision=REVISION)


def test_missing_base_boot_tree_fails_clearly(tmp_path):
    base_image = _synthetic_base_image(tmp_path)
    import shutil as _shutil

    _shutil.rmtree(base_image / "boot")
    player_deb = _synthetic_player_deb(tmp_path)
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="base_boot_tree_missing"):
        package(base_image, player_deb, destination, revision=REVISION)


def test_missing_base_image_dir_fails_clearly(tmp_path):
    base_image = tmp_path / "base-image-missing"
    player_deb = _synthetic_player_deb(tmp_path)
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="base_image_missing"):
        package(base_image, player_deb, destination, revision=REVISION)


def test_missing_player_deb_fails_clearly(tmp_path):
    base_image = _synthetic_base_image(tmp_path)
    missing_deb = tmp_path / "photo-wall-player_0.1.0+gdeadbeef_arm64.deb"
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="player_deb_missing"):
        package(base_image, missing_deb, destination, revision=REVISION)


def test_unparseable_deb_filename_still_packages_with_no_version(tmp_path):
    """Version parsing is best-effort informational, never load-bearing."""
    base_image = _synthetic_base_image(tmp_path)
    odd_deb = tmp_path / "player.deb"
    _write(odd_deb, b"fake-deb-bytes" * 100)
    destination = tmp_path / "release"

    result = package(base_image, odd_deb, destination, revision=REVISION)

    assert result["player_deb"]["version"] is None
    assert (destination / "player.deb").is_file()


def test_non_deb_player_package_rejected(tmp_path):
    base_image = _synthetic_base_image(tmp_path)
    not_a_deb = tmp_path / "photo-wall-player_0.1.0.tar.gz"
    _write(not_a_deb, b"not-a-deb")
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="player_deb_invalid"):
        package(base_image, not_a_deb, destination, revision=REVISION)


def test_missing_flash_image_fails_clearly(tmp_path):
    base_image = _synthetic_base_image(tmp_path)
    player_deb = _synthetic_player_deb(tmp_path)
    missing_flash = tmp_path / "flash" / f"photo-wall-flash-{REVISION}.img"
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="flash_image_missing"):
        package(base_image, player_deb, destination, revision=REVISION, flash_image=missing_flash)


def test_invalid_revision_rejected(tmp_path):
    base_image = _synthetic_base_image(tmp_path)
    player_deb = _synthetic_player_deb(tmp_path)
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="revision_invalid"):
        package(base_image, player_deb, destination, revision="not-a-sha")


def test_destination_must_not_already_exist(tmp_path):
    base_image = _synthetic_base_image(tmp_path)
    player_deb = _synthetic_player_deb(tmp_path)
    destination = tmp_path / "release"
    destination.mkdir()

    with pytest.raises(PackagingError, match="destination_exists"):
        package(base_image, player_deb, destination, revision=REVISION)
