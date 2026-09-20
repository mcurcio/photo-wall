"""Unit coverage for central.netboot_base (no DB): the SHA256SUMS digest read
and the per-serial selection seam."""

import hashlib

from central.netboot_base import (
    BASE_IMAGE_NAME,
    base_digest,
    select_base_for_serial,
)

BODY = b"a base squashfs"
SHA256 = hashlib.sha256(BODY).hexdigest()


def _write_sums(root, *lines):
    (root / "SHA256SUMS").write_text("".join(line + "\n" for line in lines))


def test_base_digest_reads_the_stored_bundle_sum(tmp_path):
    # The exact line shape build_netboot_bundle.sh writes (sha256sum, two spaces,
    # bundle-root-relative path).
    _write_sums(
        tmp_path,
        f"{'a' * 64}  ./boot/initrd.img",
        f"{SHA256}  ./{BASE_IMAGE_NAME}",
    )
    assert base_digest(tmp_path) == SHA256


def test_base_digest_none_when_sums_missing(tmp_path):
    assert base_digest(tmp_path) is None


def test_base_digest_none_when_squashfs_absent_from_sums(tmp_path):
    _write_sums(tmp_path, f"{'a' * 64}  ./boot/initrd.img")
    assert base_digest(tmp_path) is None


def test_base_digest_rejects_non_hex(tmp_path):
    _write_sums(tmp_path, f"{'z' * 64}  ./{BASE_IMAGE_NAME}")
    assert base_digest(tmp_path) is None


def test_select_base_for_serial_returns_single_default_for_every_serial(tmp_path):
    # Selection seam: no per-serial binding yet, so any serial (incl. None)
    # maps to the one registered base under base_root.
    for serial in (None, "10000000abcd1234", "another-serial"):
        assert select_base_for_serial(tmp_path, serial) == tmp_path / BASE_IMAGE_NAME
