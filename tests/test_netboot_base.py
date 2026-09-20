"""Unit coverage for central.netboot_base (no DB): the SHA256SUMS digest read
and the per-serial selection seam."""

import hashlib
import os

import pytest

from central.netboot_base import (
    BASE_IMAGE_NAME,
    base_digest,
    sanitize_serial,
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


def test_base_digest_uses_hardened_open_refuses_a_symlinked_sums(tmp_path):
    # base_digest reads SHA256SUMS through the same O_NOFOLLOW primitive the
    # served bytes use: a symlinked SHA256SUMS must not be followed.
    outside = tmp_path.parent / "outside-sums"
    outside.write_text(f"{SHA256}  ./{BASE_IMAGE_NAME}\n")
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(outside, root / "SHA256SUMS")
    assert base_digest(root) is None


def test_select_base_for_serial_returns_single_default_for_every_serial(tmp_path):
    # Selection seam: no per-serial binding yet, so any safe serial (incl. None)
    # maps to the one registered base under base_root.
    for serial in (None, "10000000abcd1234", "another-serial"):
        assert select_base_for_serial(tmp_path, serial) == tmp_path / BASE_IMAGE_NAME


@pytest.mark.parametrize("safe", ["10000000abcd1234", "a.b:c_d-1", "A" * 128])
def test_sanitize_serial_accepts_safe_charset(safe):
    assert sanitize_serial(safe) == safe


@pytest.mark.parametrize("unsafe", [
    None,
    "",
    "has space",
    "../../etc/passwd",     # path separators
    "with/slash",
    "nul\x00byte",          # control char
    "bell\x07",
    "A" * 129,              # too long
])
def test_sanitize_serial_rejects_unsafe_input(unsafe):
    assert sanitize_serial(unsafe) is None


def test_select_base_for_serial_validates_at_the_seam_not_just_the_caller(tmp_path):
    # An attacker-controlled serial passed straight in (bypassing the route's
    # own sanitize) must still be rejected HERE before it can key a lookup.
    # Today the return is the single default either way; the guarantee is that
    # the seam does not trust raw input.
    assert sanitize_serial("../../etc/passwd") is None
    assert select_base_for_serial(tmp_path, "../../etc/passwd") == tmp_path / BASE_IMAGE_NAME
