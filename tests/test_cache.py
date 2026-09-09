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


def test_empty_cache_validates_complete_bytes_before_readiness(tmp_path: Path) -> None:
    data = b"a complete exact variant"
    item = variant(data)
    cache = Cache(tmp_path, max_bytes=1024)
    path = cache.secure(item, [data[:4], data[4:]], pin="assignment-1")
    assert path.read_bytes() == data
    assert cache.path_for(item) == path
    assert cache.stats()["pinned_bytes"] == len(data)


@pytest.mark.parametrize(
    ("chunks", "message"),
    [([b"short"], "shorter"), ([b"exactly", b"too much"], "exceeds"),
     ([b"wrong!!"], "SHA-256")],
)
def test_incomplete_or_wrong_download_never_becomes_ready(
    tmp_path: Path, chunks: list[bytes], message: str
) -> None:
    expected = variant(b"exactly")
    cache = Cache(tmp_path, max_bytes=1024)
    with pytest.raises(CacheError, match=message):
        cache.secure(expected, chunks, pin="owner")
    assert cache.path_for(expected) is None
    assert not list(tmp_path.glob(".partial-*.tmp"))


def test_restart_rebuilds_only_verified_bytes_and_forgets_pins(tmp_path: Path) -> None:
    data = b"disposable but reusable"
    item = variant(data)
    cache = Cache(tmp_path, max_bytes=1024)
    path = cache.secure(item, [data], pin="old-process")
    cache.close()

    restarted = Cache(tmp_path, max_bytes=1024)
    assert restarted.stats()["pinned_entries"] == 0
    assert restarted.path_for(item) == path
    restarted.pin(item.sha256, "new-process")
    assert restarted.stats()["pinned_bytes"] == len(data)


def test_corrupt_surviving_file_is_deleted_and_reacquired(tmp_path: Path) -> None:
    data = b"expected bytes"
    item = variant(data)
    path = tmp_path / f"{item.sha256}.blob"
    path.write_bytes(b"corrupt bytes!")
    cache = Cache(tmp_path, max_bytes=1024)
    assert cache.path_for(item) is None
    assert not path.exists()
    assert cache.secure(item, [data], pin="current").read_bytes() == data


def test_pins_are_ephemeral_capacity_guards(tmp_path: Path) -> None:
    cache = Cache(tmp_path, max_bytes=8)
    first, second, third = map(variant, (b"1111", b"2222", b"3333"))
    cache.secure(first, [b"1111"], pin="first")
    cache.secure(second, [b"2222"], pin="second")
    with pytest.raises(CacheCapacityError):
        cache.secure(third, [b"3333"], pin="third")
    cache.release("first")
    cache.secure(third, [b"3333"], pin="third")
    assert cache.path_for(first) is None


def test_default_cache_uses_process_owned_temporary_directory() -> None:
    data = b"temporary"
    cache = Cache(None, max_bytes=1024)
    directory = cache.directory
    cache.secure(variant(data), [data], pin="owner")
    assert directory.exists()
    cache.close()
    assert not directory.exists()


def test_noncanonical_files_and_symlinks_are_never_interpreted(tmp_path: Path) -> None:
    sentinel = tmp_path.parent / "cache-sentinel"
    sentinel.write_bytes(b"keep")
    digest = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    (tmp_path / f"{digest}.blob").symlink_to(sentinel)
    cache = Cache(tmp_path, max_bytes=1024)
    assert cache.path_for(variant(b"keep")) is None
    assert sentinel.read_bytes() == b"keep"


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
    assert cache.stats()["bytes"] <= cache.max_bytes
