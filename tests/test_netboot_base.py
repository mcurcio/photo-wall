"""Unit coverage for central.netboot_base (no DB): the retained legacy
single-bundle SHA256SUMS digest read (`base_digest`) and the serial sanitizer.

The per-device selection seam `select_base_for_serial` is now DB-backed (it
upserts a `devices` row and records the served tag), so its coverage -- including
the seam-validation guarantee that a raw/unsafe serial keys NO lookup -- lives in
the Postgres-backed suites (tests/test_netboot_base_http.py and
tests/test_netboot_base_tracer.py), not here."""

import hashlib
import os

import pytest

from central.netboot_base import (
    BASE_IMAGE_NAME,
    base_digest,
    demote_dangling_base_row,
    sanitize_serial,
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


class _RecordingConn:
    """A no-DB stand-in that records the SQL + params it is handed."""

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self


class _FrozenClock:
    def utc(self):
        return "2026-09-21T00:00:00Z"


def test_demote_dangling_base_row_evicts_only_a_still_cached_row():
    # B3: the serve seam demotes a `cached` row whose file vanished so it stops
    # 503ing forever. This is the DB half (mockable without a real DB): it must
    # (a) transition state to `evicted` with the `dangling_row` reason, and
    # (b) carry the `AND state='cached'` guard so a concurrent re-cache that
    # already re-landed the bytes is NEVER clobbered.
    conn = _RecordingConn()
    demote_dangling_base_row(conn, "v1.2.3", clock=_FrozenClock())
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    assert "UPDATE base_cache SET state='evicted'" in sql
    assert "WHERE tag=%s AND state='cached'" in sql  # never clobber a re-cache
    assert "dangling_row" in params      # the eviction_reason distinguishing B3
    assert "v1.2.3" in params            # scoped to the requested tag
