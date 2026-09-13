"""Operator release-artifact packaging: presence checks and hash correctness.

The published set is the netboot base bundle tarball + the Player `.deb` + the
bootstrapper `.deb` -- no signed rootfs, no `release.pub.pem`.
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


def _synthetic_base_bundle(root):
    """Build a fake bundle tree shaped like `scripts/build_netboot_bundle.sh`'s."""
    bundle = root / "base-bundle"
    _write(bundle / "photo-wall-base.squashfs", b"fake-base-squashfs-bytes")
    _write(bundle / "boot" / "config.txt", b"[all]\narm_64bit=1\n")
    _write(bundle / "boot" / "kernel_2712.img", b"fake-kernel-bytes")
    _write(bundle / "boot" / "initrd.img", b"fake-initrd-bytes")
    _write(bundle / "boot" / "bcm2712-rpi-5-b.dtb", b"fake-dtb-bytes")
    _write(bundle / "SHA256SUMS", b"deadbeef  photo-wall-base.squashfs\n")
    return bundle


def _synthetic_player_deb(root):
    path = root / "photo-wall-player_0.1.0+gdeadbeef_arm64.deb"
    _write(path, b"fake-player-deb-bytes" * 100)
    return path


def _synthetic_bootstrapper_deb(root):
    path = root / "photo-wall-bootstrapper_0.1.0+gdeadbeef_arm64.deb"
    _write(path, b"fake-bootstrapper-deb-bytes" * 100)
    return path


def test_package_produces_expected_bundle_and_debs(tmp_path):
    bundle = _synthetic_base_bundle(tmp_path)
    player_deb = _synthetic_player_deb(tmp_path)
    bootstrapper_deb = _synthetic_bootstrapper_deb(tmp_path)
    destination = tmp_path / "release"

    result = package(bundle, player_deb, bootstrapper_deb, destination, revision=REVISION)

    assert result["revision"] == REVISION
    base_tarball = destination / f"photo-wall-base-{REVISION}.tar.gz"
    assert base_tarball.is_file()
    with tarfile.open(base_tarball) as archive:
        names = set(archive.getnames())
    assert "photo-wall-base/photo-wall-base.squashfs" in names
    assert "photo-wall-base/boot/config.txt" in names
    assert "photo-wall-base/boot/kernel_2712.img" in names
    assert "photo-wall-base/SHA256SUMS" in names

    assert (destination / player_deb.name).read_bytes() == player_deb.read_bytes()
    assert (destination / bootstrapper_deb.name).read_bytes() == bootstrapper_deb.read_bytes()

    assert result["base_image"]["filename"] == base_tarball.name
    assert result["player_deb"]["filename"] == player_deb.name
    assert result["player_deb"]["version"] == "0.1.0+gdeadbeef"
    assert result["bootstrapper_deb"]["filename"] == bootstrapper_deb.name
    assert result["bootstrapper_deb"]["version"] == "0.1.0+gdeadbeef"

    # No signature/trust-anchor artifacts anywhere in the published set
    # (UX over security, no signing).
    for path in destination.iterdir():
        assert "release.pub.pem" not in path.name
        assert not path.name.endswith(".sig")

    # No leftover staging directory.
    assert not (destination / ".staging").exists()


def test_sha256sums_matches_actual_produced_bytes(tmp_path):
    """Mutation probe: corrupting a produced artifact must break this check."""
    bundle = _synthetic_base_bundle(tmp_path)
    player_deb = _synthetic_player_deb(tmp_path)
    bootstrapper_deb = _synthetic_bootstrapper_deb(tmp_path)
    destination = tmp_path / "release"
    package(bundle, player_deb, bootstrapper_deb, destination, revision=REVISION)

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
    bundle = _synthetic_base_bundle(tmp_path)
    player_deb = _synthetic_player_deb(tmp_path)
    bootstrapper_deb = _synthetic_bootstrapper_deb(tmp_path)
    destination = tmp_path / "release"
    result = package(bundle, player_deb, bootstrapper_deb, destination, revision=REVISION)

    for key in ("base_image", "player_deb", "bootstrapper_deb"):
        produced = destination / result[key]["filename"]
        assert hashlib.sha256(produced.read_bytes()).hexdigest() == result[key]["sha256"]


def test_missing_base_squashfs_fails_clearly(tmp_path):
    bundle = _synthetic_base_bundle(tmp_path)
    (bundle / "photo-wall-base.squashfs").unlink()
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="base_squashfs_missing"):
        package(bundle, _synthetic_player_deb(tmp_path),
                _synthetic_bootstrapper_deb(tmp_path), destination, revision=REVISION)


def test_missing_base_boot_tree_fails_clearly(tmp_path):
    import shutil as _shutil

    bundle = _synthetic_base_bundle(tmp_path)
    _shutil.rmtree(bundle / "boot")
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="base_boot_tree_missing"):
        package(bundle, _synthetic_player_deb(tmp_path),
                _synthetic_bootstrapper_deb(tmp_path), destination, revision=REVISION)


def test_missing_bundle_dir_fails_clearly(tmp_path):
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="base_bundle_missing"):
        package(tmp_path / "no-bundle", _synthetic_player_deb(tmp_path),
                _synthetic_bootstrapper_deb(tmp_path), destination, revision=REVISION)


def test_missing_player_deb_fails_clearly(tmp_path):
    bundle = _synthetic_base_bundle(tmp_path)
    missing_deb = tmp_path / "photo-wall-player_0.1.0+gdeadbeef_arm64.deb"
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="player_deb_missing"):
        package(bundle, missing_deb, _synthetic_bootstrapper_deb(tmp_path),
                destination, revision=REVISION)


def test_missing_bootstrapper_deb_fails_clearly(tmp_path):
    bundle = _synthetic_base_bundle(tmp_path)
    missing_deb = tmp_path / "photo-wall-bootstrapper_0.1.0+gdeadbeef_arm64.deb"
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="bootstrapper_deb_missing"):
        package(bundle, _synthetic_player_deb(tmp_path), missing_deb,
                destination, revision=REVISION)


def test_unparseable_deb_filename_still_packages_with_no_version(tmp_path):
    """Version parsing is best-effort informational, never load-bearing."""
    bundle = _synthetic_base_bundle(tmp_path)
    odd_deb = tmp_path / "player.deb"
    _write(odd_deb, b"fake-deb-bytes" * 100)
    destination = tmp_path / "release"

    result = package(bundle, odd_deb, _synthetic_bootstrapper_deb(tmp_path),
                     destination, revision=REVISION)

    assert result["player_deb"]["version"] is None
    assert (destination / "player.deb").is_file()


def test_non_deb_player_package_rejected(tmp_path):
    bundle = _synthetic_base_bundle(tmp_path)
    not_a_deb = tmp_path / "photo-wall-player_0.1.0.tar.gz"
    _write(not_a_deb, b"not-a-deb")
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="player_deb_invalid"):
        package(bundle, not_a_deb, _synthetic_bootstrapper_deb(tmp_path),
                destination, revision=REVISION)


def test_invalid_revision_rejected(tmp_path):
    bundle = _synthetic_base_bundle(tmp_path)
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="revision_invalid"):
        package(bundle, _synthetic_player_deb(tmp_path),
                _synthetic_bootstrapper_deb(tmp_path), destination, revision="not-a-sha")


def test_destination_must_not_already_exist(tmp_path):
    bundle = _synthetic_base_bundle(tmp_path)
    destination = tmp_path / "release"
    destination.mkdir()

    with pytest.raises(PackagingError, match="destination_exists"):
        package(bundle, _synthetic_player_deb(tmp_path),
                _synthetic_bootstrapper_deb(tmp_path), destination, revision=REVISION)
