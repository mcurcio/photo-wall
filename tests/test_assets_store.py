"""`CacheLayout` and `CacheStore` on a real filesystem (`tmp_path`)."""

from __future__ import annotations

import hashlib
import os

import pytest

from central.artifact_io import HardenedOpenError
from central.assets.layout import TEMP_PREFIX, CacheLayout
from central.assets.store import CacheStore
from central.kernel.assets import AssetKey, AssetKind, AssetReady

TAG = "v1.2.3"
SHA = "ab" * 32
OS_KEY = AssetKey(AssetKind.OS_IMAGE, TAG)
DEB_KEY = AssetKey(AssetKind.PLAYER_DEB, SHA)
DATA = b"squashfs bytes " * 100
FACTS = AssetReady(size=len(DATA), sha256=hashlib.sha256(DATA).hexdigest())


@pytest.fixture
def store(tmp_path):
    return CacheStore(CacheLayout(tmp_path))


def _put(store: CacheStore, key: AssetKey, data: bytes = DATA):
    path = store.layout.path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_layout_keeps_todays_file_names(tmp_path):
    layout = CacheLayout(tmp_path)
    assert layout.directory(AssetKind.OS_IMAGE) == tmp_path / "os-images"
    assert layout.directory(AssetKind.PLAYER_DEB) == tmp_path / "apps"
    assert layout.path(OS_KEY) == tmp_path / "os-images" / f"base-{TAG}.squashfs"
    assert layout.path(DEB_KEY) == tmp_path / "apps" / f"app-{SHA}.deb"
    assert not layout.path(OS_KEY).name.startswith(TEMP_PREFIX)


def test_open_returns_an_owned_fd_of_the_recorded_size(store):
    _put(store, OS_KEY)
    opened = store.open(OS_KEY, FACTS)
    assert opened is not None
    try:
        assert opened.size == len(DATA)
        assert os.read(opened.fd, len(DATA) + 1) == DATA
    finally:
        os.close(opened.fd)


def test_open_is_none_when_absent_wrong_size_symlinked_or_not_regular(store, tmp_path):
    assert store.open(OS_KEY, FACTS) is None  # absent, and the directory does not exist
    path = _put(store, OS_KEY, DATA[:-1])
    assert store.open(OS_KEY, FACTS) is None  # size mismatch
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(DATA)
    path.symlink_to(outside)
    assert store.open(OS_KEY, FACTS) is None  # O_NOFOLLOW refuses the symlink
    assert store.present(OS_KEY, FACTS) is False
    path.unlink()
    os.mkfifo(path)
    assert store.present(OS_KEY, FACTS) is False
    path.unlink()
    path.mkdir()
    assert store.open(OS_KEY, FACTS) is None
    assert store.present(OS_KEY, FACTS) is False


def test_present_checks_size_with_lstat(store):
    assert store.present(OS_KEY, FACTS) is False
    _put(store, OS_KEY)
    assert store.present(OS_KEY, FACTS) is True
    assert store.present(OS_KEY, AssetReady(size=len(DATA) + 1, sha256=FACTS.sha256)) is False


def test_temp_path_is_unique_non_existent_and_in_the_kind_directory(store):
    first = store.temp_path(DEB_KEY)
    second = store.temp_path(DEB_KEY)
    assert first != second
    assert first.parent == store.layout.directory(AssetKind.PLAYER_DEB)
    assert first.parent.is_dir()
    assert first.name.startswith(TEMP_PREFIX)
    assert not first.exists() and not second.exists()


def test_measure_streams_size_and_digest(store):
    path = _put(store, OS_KEY)
    assert store.measure(path) == FACTS


def test_measure_refuses_absent_symlink_and_empty(store, tmp_path):
    with pytest.raises(HardenedOpenError):
        store.measure(tmp_path / "absent")
    target = tmp_path / "target"
    target.write_bytes(DATA)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(HardenedOpenError):
        store.measure(link)
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    with pytest.raises(ValueError):
        store.measure(empty)


def test_install_renames_over_the_final_path_and_discard_is_idempotent(store):
    _put(store, OS_KEY, b"old")
    temp = store.temp_path(OS_KEY)
    temp.write_bytes(DATA)
    store.install(temp, OS_KEY)
    assert not temp.exists()
    assert store.layout.path(OS_KEY).read_bytes() == DATA
    store.discard(store.layout.path(OS_KEY))
    store.discard(store.layout.path(OS_KEY))  # missing -> no-op
    assert not store.layout.path(OS_KEY).exists()
