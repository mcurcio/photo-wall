"""Operator release-artifact packaging: presence checks and hash correctness."""

from __future__ import annotations

import hashlib
import tarfile

import pytest

from scripts.package_release_artifacts import PackagingError, package

REVISION = "a" * 40


def _write(path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _synthetic_output(root):
    """Build a fake `output/` tree shaped like `scripts/build_ci_image.py`'s."""
    pxe = root / "output" / "pxe"
    _write(pxe / "config.txt", b"[all]\narm_64bit=1\n")
    _write(pxe / "cmdline.txt", b"boot=photowall\n")
    _write(pxe / "vmlinuz", b"fake-kernel-bytes")
    _write(pxe / "initrd.img", b"fake-initramfs-bytes")
    _write(pxe / "appliance" / "release.json", b'{"schema":1}\n')
    _write(pxe / "appliance" / "release.sig", b"s" * 64)
    _write(pxe / "appliance" / f"rootfs-{'f' * 64}.squashfs", b"fake-squashfs-bytes")
    return root / "output"


def _synthetic_flash_image(root) -> None:
    path = root / "flash" / f"photo-wall-flash-{REVISION}.img"
    _write(path, b"fake-flash-disk-bytes" * 1000)
    return path


def _synthetic_release_pub(root):
    path = root / "deployment" / "release.pub.pem"
    _write(path, b"-----BEGIN PUBLIC KEY-----\nfake\n-----END PUBLIC KEY-----\n")
    return path


def test_package_produces_expected_netboot_and_flash_contents(tmp_path):
    output = _synthetic_output(tmp_path)
    flash_image = _synthetic_flash_image(tmp_path)
    release_pub = _synthetic_release_pub(tmp_path)
    destination = tmp_path / "release"

    result = package(output, flash_image, release_pub, destination, revision=REVISION)

    assert result["revision"] == REVISION
    netboot_tarball = destination / f"photo-wall-netboot-{REVISION}.tar.gz"
    assert netboot_tarball.is_file()
    with tarfile.open(netboot_tarball) as archive:
        names = set(archive.getnames())
    assert "photo-wall-netboot/tftp/config.txt" in names
    assert "photo-wall-netboot/tftp/cmdline.txt" in names
    assert "photo-wall-netboot/tftp/vmlinuz" in names
    assert "photo-wall-netboot/tftp/initrd.img" in names
    assert "photo-wall-netboot/tftp/appliance/release.json" in names
    assert "photo-wall-netboot/tftp/appliance/release.sig" in names
    assert f"photo-wall-netboot/tftp/appliance/rootfs-{'f' * 64}.squashfs" in names
    assert "photo-wall-netboot/release/release.json" in names
    assert "photo-wall-netboot/release/release.sig" in names
    assert f"photo-wall-netboot/release/rootfs-{'f' * 64}.squashfs" in names
    assert "photo-wall-netboot/release.pub.pem" in names

    flash_name = f"photo-wall-flash-{REVISION}.img.xz"
    assert (destination / flash_name).is_file()
    assert (destination / f"{flash_name}.sha256").is_file()
    sha_line = (destination / f"{flash_name}.sha256").read_text()
    assert sha_line == f"{result['flash_image_sha256']}  {flash_name}\n"

    assert (destination / "release.pub.pem").read_bytes() == release_pub.read_bytes()

    # No leftover staging directory.
    assert not (destination / ".staging").exists()


def test_sha256sums_matches_actual_produced_bytes(tmp_path):
    """Mutation probe: corrupting a produced artifact must break this check."""
    output = _synthetic_output(tmp_path)
    flash_image = _synthetic_flash_image(tmp_path)
    release_pub = _synthetic_release_pub(tmp_path)
    destination = tmp_path / "release"
    package(output, flash_image, release_pub, destination, revision=REVISION)

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


def test_missing_release_pub_fails_clearly(tmp_path):
    output = _synthetic_output(tmp_path)
    flash_image = _synthetic_flash_image(tmp_path)
    missing_pub = tmp_path / "deployment" / "release.pub.pem"
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="release_pub_missing"):
        package(output, flash_image, missing_pub, destination, revision=REVISION)


def test_missing_rootfs_bundle_fails_clearly(tmp_path):
    output = _synthetic_output(tmp_path)
    (output / "pxe" / "appliance" / f"rootfs-{'f' * 64}.squashfs").unlink()
    flash_image = _synthetic_flash_image(tmp_path)
    release_pub = _synthetic_release_pub(tmp_path)
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="rootfs_bundle_missing"):
        package(output, flash_image, release_pub, destination, revision=REVISION)


def test_missing_pxe_tree_fails_clearly(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    flash_image = _synthetic_flash_image(tmp_path)
    release_pub = _synthetic_release_pub(tmp_path)
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="pxe_tree_missing"):
        package(output, flash_image, release_pub, destination, revision=REVISION)


def test_missing_flash_image_fails_clearly(tmp_path):
    output = _synthetic_output(tmp_path)
    missing_flash = tmp_path / "flash" / f"photo-wall-flash-{REVISION}.img"
    release_pub = _synthetic_release_pub(tmp_path)
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="flash_image_missing"):
        package(output, missing_flash, release_pub, destination, revision=REVISION)


def test_invalid_revision_rejected(tmp_path):
    output = _synthetic_output(tmp_path)
    flash_image = _synthetic_flash_image(tmp_path)
    release_pub = _synthetic_release_pub(tmp_path)
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="revision_invalid"):
        package(output, flash_image, release_pub, destination, revision="not-a-sha")


def test_destination_must_not_already_exist(tmp_path):
    output = _synthetic_output(tmp_path)
    flash_image = _synthetic_flash_image(tmp_path)
    release_pub = _synthetic_release_pub(tmp_path)
    destination = tmp_path / "release"
    destination.mkdir()

    with pytest.raises(PackagingError, match="destination_exists"):
        package(output, flash_image, release_pub, destination, revision=REVISION)
