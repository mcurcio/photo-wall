from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from contracts.models import Variant
from player.cache import Cache, CacheCapacityError, CacheError


def variant(data: bytes) -> Variant:
    return Variant(
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        media_type="image/png",
        width=2,
        height=2,
    )


def test_secure_validates_publishes_and_recovers_after_restart(tmp_path: Path) -> None:
    data = b"a complete exact variant"
    item = variant(data)
    cache = Cache(tmp_path, max_bytes=1024)

    path = cache.secure(item, [data[:4], data[4:]], pin="assignment-1")
    assert path == tmp_path / f"{item.sha256}.blob"
    assert path.read_bytes() == data
    assert cache.path_for(item) == path
    assert cache.stats() == {
        "bytes": len(data),
        "entries": 1,
        "pinned_bytes": len(data),
        "pinned_entries": 1,
        "temporary_bytes": 0,
        "max_bytes": 1024,
    }
    cache.close()

    restarted = Cache(tmp_path, max_bytes=1024)
    assert restarted.path_for(item) == path
    restarted.close()


@pytest.mark.parametrize(
    ("chunks", "message"),
    [
        ([b"short"], "shorter"),
        ([b"exactly", b"too much"], "exceeds"),
        ([b"wrong!!"], "SHA-256"),
    ],
)
def test_secure_rejects_short_overlong_and_corrupt_streams(
    tmp_path: Path, chunks: list[bytes], message: str
) -> None:
    expected = variant(b"exactly")
    cache = Cache(tmp_path, max_bytes=1024)
    with pytest.raises(CacheError, match=message):
        cache.secure(expected, chunks, pin="owner")
    assert cache.path_for(expected) is None
    assert not list(tmp_path.glob(".partial-*.tmp"))
    assert cache.stats()["bytes"] == 0
    cache.close()


def test_stale_partial_and_corrupt_indexed_blob_are_not_ready(tmp_path: Path) -> None:
    data = b"persisted bytes"
    item = variant(data)
    stale = tmp_path / ".partial-crashed.tmp"
    stale.write_bytes(b"partial")
    cache = Cache(tmp_path, max_bytes=1024)
    cache.secure(item, [data], pin="owner")
    cache.close()

    (tmp_path / f"{item.sha256}.blob").write_bytes(b"corrupted")
    restarted = Cache(tmp_path, max_bytes=1024)
    assert not stale.exists()
    assert restarted.path_for(item) is None
    assert restarted.stats()["entries"] == 0
    restarted.close()


def test_rename_before_index_failure_leaves_orphan_repaired_on_restart(tmp_path: Path) -> None:
    data = b"rename then index"
    item = variant(data)
    cache = Cache(tmp_path, max_bytes=1024)

    def fail_commit() -> None:
        raise RuntimeError("simulated process loss at index commit")

    cache._commit = fail_commit  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="index commit"):
        cache.secure(item, [data], pin="owner")
    assert (tmp_path / f"{item.sha256}.blob").exists()
    cache.close()

    restarted = Cache(tmp_path, max_bytes=1024)
    assert restarted.path_for(item) is None
    assert not (tmp_path / f"{item.sha256}.blob").exists()
    restarted.close()


def test_pins_protect_files_until_owner_release(tmp_path: Path) -> None:
    first = b"1111"
    second = b"2222"
    third = b"3333"
    cache = Cache(tmp_path, max_bytes=8)
    cache.secure(variant(first), [first], pin="first")
    cache.secure(variant(second), [second], pin="second")
    with pytest.raises(CacheCapacityError):
        cache.secure(variant(third), [third], pin="third")
    assert cache.stats()["bytes"] == 8

    cache.release("first")
    cache.secure(variant(third), [third], pin="third")
    assert cache.path_for(variant(first)) is None
    assert cache.path_for(variant(second)) is not None
    assert cache.path_for(variant(third)) is not None
    cache.close()


def test_untrusted_filename_metadata_cannot_traverse_cache_directory(tmp_path: Path) -> None:
    data = b"safe canonical path"
    item = variant(data)
    outside = tmp_path.parent / "cache-escape-sentinel"
    outside.write_bytes(b"keep")
    cache = Cache(tmp_path, max_bytes=1024)
    cache.secure(item, [data], pin="owner")
    cache._db.execute(
        "UPDATE blobs SET filename = ? WHERE sha256 = ?", ("../../cache-escape-sentinel", item.sha256)
    )
    cache.close()

    restarted = Cache(tmp_path, max_bytes=1024)
    assert restarted.path_for(item) == tmp_path / f"{item.sha256}.blob"
    assert outside.read_bytes() == b"keep"
    assert restarted._db.execute(
        "SELECT filename FROM blobs WHERE sha256 = ?", (item.sha256,)
    ).fetchone()[0] == f"{item.sha256}.blob"
    restarted.close()


def test_invalid_digest_index_row_cannot_delete_path_outside_cache(tmp_path: Path) -> None:
    sentinel = tmp_path.parent / "cache-invalid-digest-sentinel"
    sentinel.write_bytes(b"keep")
    cache = Cache(tmp_path, max_bytes=1024)
    cache._db.execute(
        "INSERT INTO blobs(sha256, size, filename, accessed) VALUES (?, ?, ?, ?)",
        ("../../cache-invalid-digest-sentinel", 4, "ignored", 0),
    )
    cache._db.execute(
        "INSERT INTO pins(sha256, owner) VALUES (?, ?)",
        ("../../cache-invalid-digest-sentinel", "owner"),
    )
    cache.close()

    restarted = Cache(tmp_path, max_bytes=1024)
    assert sentinel.read_bytes() == b"keep"
    assert restarted._db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0
    assert restarted._db.execute("SELECT COUNT(*) FROM pins").fetchone()[0] == 0
    restarted.close()


def test_valid_pinned_blob_survives_reopen_with_lower_quota(tmp_path: Path) -> None:
    data = b"kept even after quota change"
    item = variant(data)
    cache = Cache(tmp_path, max_bytes=128)
    cache.secure(item, [data], pin="scheduled")
    cache.close()

    restarted = Cache(tmp_path, max_bytes=len(data) - 1)
    assert restarted.path_for(item) is not None
    assert restarted.stats()["pinned_bytes"] == len(data)
    with pytest.raises(CacheCapacityError):
        restarted.secure(variant(b"new"), [b"new"], pin="new-owner")
    restarted.release("scheduled")
    restarted.secure(variant(b"new"), [b"new"], pin="new-owner")
    restarted.close()


def test_reopen_lower_quota_evicts_unpinned_before_reporting_pressure(tmp_path: Path) -> None:
    pinned = b"pin!"
    retained_only_by_lru = b"drop"
    cache = Cache(tmp_path, max_bytes=16)
    pinned_item = variant(pinned)
    dropped_item = variant(retained_only_by_lru)
    cache.secure(pinned_item, [pinned], pin="scheduled")
    cache.secure(dropped_item, [retained_only_by_lru], pin="temporary")
    cache.release("temporary")
    cache.close()

    restarted = Cache(tmp_path, max_bytes=len(pinned))
    assert restarted.path_for(pinned_item) is not None
    assert restarted.path_for(dropped_item) is None
    assert restarted.stats()["bytes"] == len(pinned)
    restarted.close()


def test_failed_eviction_keeps_file_and_index_accounted(tmp_path: Path, monkeypatch) -> None:
    first = b"1111"
    second = b"2222"
    cache = Cache(tmp_path, max_bytes=4)
    cache.secure(variant(first), [first], pin="owner")
    cache.release("owner")

    def fail_unlink(_sha256: str) -> None:
        raise CacheError("simulated filesystem refusal")

    monkeypatch.setattr(cache, "_unlink_blob", fail_unlink)
    with pytest.raises(CacheError, match="filesystem refusal"):
        cache.secure(variant(second), [second], pin="new-owner")
    assert (tmp_path / f"{variant(first).sha256}.blob").read_bytes() == first
    assert cache._db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
    assert cache.stats()["bytes"] == len(first)
    cache.close()


def test_reentrant_stats_does_not_remove_active_partial(tmp_path: Path) -> None:
    data = b"reentrant acquisition"
    item = variant(data)
    cache = Cache(tmp_path, max_bytes=1024)

    def chunks():
        yield data[:5]
        assert cache.stats()["temporary_bytes"] == 5
        yield data[5:]

    assert cache.secure(item, chunks(), pin="owner").read_bytes() == data
    assert not list(tmp_path.glob(".partial-*.tmp"))
    cache.close()


def test_concurrent_acquisition_never_exceeds_bound(tmp_path: Path) -> None:
    payloads = [bytes([65 + index]) * 6 for index in range(5)]
    cache = Cache(tmp_path, max_bytes=12)

    def acquire(index: int) -> str:
        try:
            cache.secure(variant(payloads[index]), [payloads[index]], pin=f"owner-{index}")
            return "secured"
        except CacheCapacityError:
            return "capacity"

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(acquire, range(len(payloads))))
    assert results.count("secured") == 2
    assert results.count("capacity") == 3
    stats = cache.stats()
    assert stats["bytes"] <= stats["max_bytes"]
    assert sum(p.stat().st_size for p in tmp_path.glob("*.blob")) <= stats["max_bytes"]
    cache.close()
