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
    _base_file_tag,
    _owned_base_tags,
    _sweep_orphans,
    base_digest,
    base_file_path,
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


# -- os-images orphan sweep (0013 B4), the DB-free decision + FS half ----------


@pytest.mark.parametrize("name,tag", [
    ("base-v0.0.1.squashfs", "v0.0.1"),          # the served shape -> its tag
    ("base-v1.2.3-rc.1.squashfs", "v1.2.3-rc.1"),  # dotted/prerelease tag survives
    (".base-v0.0.9.tar.deadbeef.tmp", None),     # a fetch temp (leading dot) is NOT a base file
    (".base-abcd1234.tmp", None),                # the mkstemp squashfs temp either
    ("SHA256SUMS", None),                        # an unrelated file
    ("base-.squashfs", None),                    # empty tag -> not a candidate
])
def test_base_file_tag_matches_only_the_served_shape(name, tag):
    # The sweep candidate set is EXACTLY `base-<tag>.squashfs`; a `.base-...tmp`
    # fetch temp (the fetch's in-progress files) is the boot stray-temp sweep's
    # job, never the orphan sweep's -- so the two never contend for a file.
    assert _base_file_tag(name) == tag


def test_owned_base_tags_counts_in_flight_caching_rows_as_owned():
    # The frozen sweep invariant: an in-flight `caching` row OWNS its file, so the
    # owned-set query must include BOTH `cached` and `caching` -- a DB-row-shape
    # assertion mockable without a real DB.
    class _Conn:
        def __init__(self):
            self.sql = None

        def execute(self, sql, params=None):
            self.sql = sql
            return self

        def fetchall(self):
            return [{"tag": "v0.0.1"}, {"tag": "v0.0.2"}]

    conn = _Conn()
    assert _owned_base_tags(conn) == {"v0.0.1", "v0.0.2"}
    assert "state IN ('cached','caching')" in conn.sql  # in-flight counts as owned


def test_sweep_orphans_unlinks_only_unowned_base_files(tmp_path):
    # The FS half, DB-free: given the owned-tag set, the sweep unlinks a
    # `base-<tag>.squashfs` whose tag owns no row (the true orphan) and leaves
    # every owned file -- a `cached` tag AND an in-flight `caching` tag -- plus
    # any non-base file (a fetch temp) untouched.
    owned_cached = base_file_path(tmp_path, "v0.0.1")
    owned_inflight = base_file_path(tmp_path, "v0.0.2")   # its row is `caching`
    orphan = base_file_path(tmp_path, "v9.9.9")           # no row at all
    temp = tmp_path / ".base-abcd1234.tmp"                # a fetch temp, not ours
    for path in (owned_cached, owned_inflight, orphan, temp):
        path.write_bytes(b"bytes for " + path.name.encode())

    removed = _sweep_orphans(tmp_path, {"v0.0.1", "v0.0.2"}, budget=100)

    # Only the true orphan is swept; owned (cached + in-flight) survive verbatim.
    assert removed == ["v9.9.9"]
    assert not orphan.exists()
    assert owned_cached.exists() and owned_inflight.exists()
    # PROBE (in-flight NOT owned): dropping `caching` from the owned set would
    # unlink owned_inflight and red this line -- it must survive mid-fetch.
    assert owned_inflight.read_bytes() == b"bytes for base-v0.0.2.squashfs"
    # A leading-dot fetch temp is never a base-file candidate, so it is untouched.
    assert temp.exists()


def test_sweep_orphans_is_bounded_by_the_scan_budget(tmp_path):
    # A pathological directory cannot make the sweep run unbounded: past the
    # budget the enumeration stops (a warning is logged), leaving the rest for a
    # later pass rather than blocking the poll tail.
    for index in range(5):
        base_file_path(tmp_path, f"v9.9.{index}").write_bytes(b"x")

    removed = _sweep_orphans(tmp_path, set(), budget=2)

    assert len(removed) <= 2  # never more than the budget of unlinks in one pass


def test_sweep_orphans_tolerates_a_missing_os_images_dir(tmp_path):
    # F-2 (0013 B4): on a fresh cache root the os-images dir may not exist yet.
    # The sweep must return 0 cleanly, like `gc_base_cache` tolerates it -- NOT
    # raise FileNotFoundError from `iterdir` and crash the poll tail every tick.
    missing = tmp_path / "os-images"
    assert not missing.exists()
    assert _sweep_orphans(missing, set(), budget=100) == []


def test_sweep_orphans_spares_a_fresh_file_within_the_mtime_grace(tmp_path):
    # F-1 defense-in-depth (b): a `fetch_base` that `os.replace`s bytes AFTER the
    # owned-set snapshot leaves a file with a FRESH mtime under a not-yet-owned
    # tag. The sweep must NOT unlink it -- those are a live fetch's landing bytes,
    # not an orphan. Dropping the grace would red this line (the race the finding
    # names: unlink-vs-os.replace).
    now = 1_000_000.0
    orphan = base_file_path(tmp_path, "v9.9.9")   # absent from the owned snapshot
    orphan.write_bytes(b"just landed")
    os.utime(orphan, (now - 5, now - 5))          # mtime 5s old, inside a 300s grace

    removed = _sweep_orphans(tmp_path, set(), budget=100, now=now, grace_seconds=300)

    assert removed == []
    assert orphan.exists()  # freshly (re)written bytes survive the sweep


def test_sweep_orphans_reclaims_an_aged_orphan_past_the_grace(tmp_path):
    # F-1 liveness: a GENUINE orphan (mtime older than the grace, no owning row)
    # is still swept -- the grace delays reclamation, it does not disable it.
    now = 1_000_000.0
    orphan = base_file_path(tmp_path, "v9.9.9")
    orphan.write_bytes(b"stale bytes")
    os.utime(orphan, (now - 900, now - 900))      # 900s old, well past a 300s grace

    removed = _sweep_orphans(tmp_path, set(), budget=100, now=now, grace_seconds=300)

    assert removed == ["v9.9.9"]
    assert not orphan.exists()


def test_sweep_orphans_spares_a_tag_that_became_owned_after_the_snapshot(tmp_path):
    # F-1 defense-in-depth (a): a tag ABSENT from the owned-set snapshot but whose
    # row a fetch commits (`caching`/`cached`) before the sweep reaches its unlink
    # must be spared. The per-tag re-confirm catches it even when the file is aged
    # past the mtime grace (so this isolates the re-confirm guard, not the grace).
    now = 1_000_000.0
    orphan = base_file_path(tmp_path, "v9.9.9")
    orphan.write_bytes(b"landed, row now committed")
    os.utime(orphan, (now - 900, now - 900))      # aged: the grace alone would NOT spare it
    seen: list[str] = []

    def reconfirm(tag: str) -> bool:
        seen.append(tag)
        return True  # a fetch committed this tag's caching/cached row mid-sweep

    removed = _sweep_orphans(
        tmp_path, set(), budget=100, now=now, grace_seconds=300, reconfirm_owned=reconfirm
    )

    assert removed == []
    assert orphan.exists()
    assert seen == ["v9.9.9"]  # the per-tag re-confirm actually ran before the unlink
