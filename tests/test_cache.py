"""The Player's media cache keeps a variant by (key, digest): its name, `<asset_id>.<recipe_id>`,
and the bytes Central served under it, checked against the `Repr-Digest` and `Content-Length`
Central answered. A re-made copy (other bytes of the same content) is accepted under the same
name; the plan's digest is not what the bytes are checked against."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from contracts.models import Variant
from player.cache import Cache, CacheCapacityError, CacheError

RECIPE = "e" * 64


def variant(data: bytes, asset_id: str | None = None) -> Variant:
    """The plan's variant for `data`: named by a photo and a recipe, with the plan's bytes."""
    return Variant(
        asset_id=asset_id or "asset-" + hashlib.sha256(data).hexdigest()[:16],
        recipe_id=RECIPE,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        media_type="image/png",
        width=2,
        height=2,
    )


def served(data: bytes) -> dict:
    """What Central answered with the bytes: their `Repr-Digest` and `Content-Length`."""
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def file_of(directory: Path, item: Variant, data: bytes) -> Path:
    """Where the cache keeps `data` for `item`: named by (key, digest)."""
    key = hashlib.sha256(item.key.encode()).hexdigest()
    return directory / f"{key}.{hashlib.sha256(data).hexdigest()}.blob"


def test_empty_cache_validates_complete_bytes_before_readiness(tmp_path: Path) -> None:
    data = b"a complete exact variant"
    item = variant(data)
    cache = Cache(tmp_path, max_bytes=1024)
    path = cache.secure(item, [data[:4], data[4:]], pin="assignment-1", **served(data))
    assert path == file_of(tmp_path, item, data)
    assert path.read_bytes() == data
    assert cache.path_for(item) == path
    assert cache.stats()["pinned_bytes"] == len(data)


def test_a_re_made_copy_is_kept_under_the_same_name(tmp_path: Path) -> None:
    """A re-make serves other bytes than the plan's: the cache checks them against what Central
    served, keeps them under the variant's name, and finds them for the plan's variant."""
    planned, remade = b"the first making", b"a re-make, other bytes"
    item = variant(planned, asset_id="asset-photo")
    cache = Cache(tmp_path, max_bytes=1024)
    path = cache.secure(item, [remade], pin="assignment", **served(remade))
    assert path.read_bytes() == remade
    assert cache.path_for(item) == path
    cache.pin(item, "retained")
    assert cache.stats()["pinned_entries"] == 1
    # Another photo's plan that names the same bytes is not this variant.
    assert cache.path_for(variant(remade, asset_id="asset-other")) is None


def test_bytes_are_checked_against_what_was_served_not_the_plan(tmp_path: Path) -> None:
    data = b"the plan's bytes"
    item = variant(data)
    cache = Cache(tmp_path, max_bytes=1024)
    with pytest.raises(CacheError, match="SHA-256"):
        cache.secure(item, [data], pin="owner", **served(b"what Central said it sent"[:len(data)]))
    assert cache.path_for(item) is None


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
        cache.secure(expected, chunks, pin="owner", **served(b"exactly"))
    assert cache.path_for(expected) is None
    assert not list(tmp_path.glob(".partial-*.tmp"))


def test_restart_rebuilds_only_verified_bytes_and_forgets_pins(tmp_path: Path) -> None:
    data = b"disposable but reusable"
    item = variant(data)
    cache = Cache(tmp_path, max_bytes=1024)
    path = cache.secure(item, [data], pin="old-process", **served(data))
    cache.close()

    restarted = Cache(tmp_path, max_bytes=1024)
    assert restarted.stats()["pinned_entries"] == 0
    assert restarted.path_for(item) == path
    restarted.pin(item, "new-process")
    assert restarted.stats()["pinned_bytes"] == len(data)


def test_corrupt_surviving_file_is_deleted_and_reacquired(tmp_path: Path) -> None:
    data = b"expected bytes"
    item = variant(data)
    path = file_of(tmp_path, item, data)
    path.write_bytes(b"corrupt bytes!")
    cache = Cache(tmp_path, max_bytes=1024)
    assert cache.path_for(item) is None
    assert not path.exists()
    assert cache.secure(item, [data], pin="current", **served(data)).read_bytes() == data


def test_a_file_that_appears_after_start_is_found_once_verified(tmp_path: Path) -> None:
    data = b"copied in later"
    item = variant(data)
    cache = Cache(tmp_path, max_bytes=1024)
    path = file_of(tmp_path, item, data)
    path.write_bytes(data)
    assert cache.path_for(item) == path
    with pytest.raises(CacheError, match="missing or corrupt"):
        cache.pin(variant(b"never stored"), "owner")


def test_pins_are_ephemeral_capacity_guards(tmp_path: Path) -> None:
    cache = Cache(tmp_path, max_bytes=8)
    first, second, third = map(variant, (b"1111", b"2222", b"3333"))
    cache.secure(first, [b"1111"], pin="first", **served(b"1111"))
    cache.secure(second, [b"2222"], pin="second", **served(b"2222"))
    with pytest.raises(CacheCapacityError):
        cache.secure(third, [b"3333"], pin="third", **served(b"3333"))
    cache.release("first")
    cache.secure(third, [b"3333"], pin="third", **served(b"3333"))
    assert cache.path_for(first) is None


def test_default_cache_uses_process_owned_temporary_directory() -> None:
    data = b"temporary"
    cache = Cache(None, max_bytes=1024)
    directory = cache.directory
    cache.secure(variant(data), [data], pin="owner", **served(data))
    assert directory.exists()
    cache.close()
    assert not directory.exists()


def test_noncanonical_files_and_symlinks_are_never_interpreted(tmp_path: Path) -> None:
    sentinel = tmp_path.parent / "cache-sentinel"
    sentinel.write_bytes(b"keep")
    item = variant(b"keep")
    file_of(tmp_path, item, b"keep").symlink_to(sentinel)
    cache = Cache(tmp_path, max_bytes=1024)
    assert cache.path_for(item) is None
    assert sentinel.read_bytes() == b"keep"


def test_concurrent_acquisition_never_exceeds_bound(tmp_path: Path) -> None:
    payloads = [bytes([65 + index]) * 6 for index in range(5)]
    cache = Cache(tmp_path, max_bytes=12)

    def acquire(index: int) -> str:
        try:
            cache.secure(variant(payloads[index]), [payloads[index]], pin=f"owner-{index}",
                         **served(payloads[index]))
            return "secured"
        except CacheCapacityError:
            return "capacity"

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(acquire, range(len(payloads))))
    assert results.count("secured") == 2
    assert results.count("capacity") == 3
    assert cache.stats()["bytes"] <= cache.max_bytes
