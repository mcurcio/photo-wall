"""Offer bytes are served from the cache's recorded facts; no request hashes them."""

import asyncio
import hashlib
import os

import pytest

from central.assets.layout import CacheLayout
from central.assets.reader import Opened, Unavailable
from central.assets.store import CacheStore
from central.fleet.bytes import OfferByteReader
from central.fleet.models import FleetError
from central.fleet.service import OfferAsset
from central.kernel.assets import AssetReady
from central.kernel.job_types import FetchPackage
from central.kernel.jobs import asset_key


class Reader:
    """Opens through a real CacheStore with the recorded `facts` (size, digest), as AssetReader
    does: a cached file of another size is never opened, and the reader reports it as the real
    one does after a fill that leaves no usable file."""

    def __init__(self, root, *, size=0, digest="0" * 64, unavailable=None):
        self.store = CacheStore(CacheLayout(root))
        self.size, self.digest = size, digest
        self.unavailable = unavailable
        self.opened_fd = None

    def put(self, job, data: bytes) -> None:
        path = self.store.layout.path(asset_key(job))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def read(self, candidates):
        if self.unavailable is not None:
            return self.unavailable
        job = candidates.jobs[0]
        file = self.store.open(asset_key(job), AssetReady(size=self.size, sha256=self.digest))
        if file is None:
            return Unavailable("absent_after_ready", 5)
        self.opened_fd = file.fd
        return Opened(job, file.fd, file.size, self.digest)


def _asset(blob: bytes) -> OfferAsset:
    digest = hashlib.sha256(blob).hexdigest()
    return OfferAsset("app", "v1.0.0", digest, digest, len(blob))


def _cached(root, asset: OfferAsset, on_disk: bytes, *, size=None, digest=None) -> Reader:
    """A Reader whose cache holds `on_disk` for `asset`, recorded at `size` and `digest`
    (default: the asset's own claim)."""
    reader = Reader(root, size=asset.size if size is None else size,
                    digest=asset.sha256 if digest is None else digest)
    reader.put(FetchPackage(sha256=asset.content_key), on_disk)
    return reader


def _closed(fd) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


def test_headers_come_from_recorded_facts_and_no_request_hashes(tmp_path, monkeypatch) -> None:
    blob = b"exact offer bytes"
    asset = OfferAsset("app", "v1.0.0", "a" * 64, "a" * 64, len(blob))
    # Same size, different bytes: only a per-request hash could tell. The worker verified the
    # digest on fill and the node verifies it after download, so the serve trusts the facts.
    reader = _cached(tmp_path, asset, b"X" * len(blob))
    monkeypatch.setattr(hashlib, "sha256", lambda *_: pytest.fail("request path hashed"))
    opened = asyncio.run(OfferByteReader(reader).open_exact(asset))
    try:
        assert (opened.size, opened.sha256) == (len(blob), "a" * 64)
    finally:
        os.close(opened.fd)


def test_recorded_digest_mismatch_is_refused_and_closes_fd(tmp_path) -> None:
    blob = b"right"
    reader = _cached(tmp_path, _asset(blob), blob, digest="f" * 64)
    with pytest.raises(FleetError, match="offer_artifact_mismatch"):
        asyncio.run(OfferByteReader(reader).open_exact(_asset(blob)))
    assert reader.opened_fd is not None and _closed(reader.opened_fd)


def test_recorded_size_of_another_offer_is_refused_and_closes_fd(tmp_path) -> None:
    blob = b"right bytes"
    # The cache holds a consistent file whose recorded size is not the offer's claim.
    reader = _cached(tmp_path, _asset(blob), b"short", size=len(b"short"))
    with pytest.raises(FleetError, match="offer_artifact_mismatch"):
        asyncio.run(OfferByteReader(reader).open_exact(_asset(blob)))
    assert reader.opened_fd is not None and _closed(reader.opened_fd)


def test_cached_inode_of_another_size_is_never_opened(tmp_path) -> None:
    blob = b"right bytes"
    # Facts claim the offer's size; the file on disk is not that size. CacheStore.open refuses
    # it, so the serve sees no file, never a descriptor with a false Content-Length.
    reader = _cached(tmp_path, _asset(blob), b"short")
    with pytest.raises(FleetError, match="app_absent_after_ready") as refused:
        asyncio.run(OfferByteReader(reader).open_exact(_asset(blob)))
    assert refused.value.status == 503 and reader.opened_fd is None


def test_absent_content_is_named_with_retry_after_and_preflight_closes_fd(tmp_path) -> None:
    blob = b"ready"
    reader = _cached(tmp_path, _asset(blob), blob)
    asyncio.run(OfferByteReader(reader).preflight((_asset(blob),)))
    assert _closed(reader.opened_fd)
    cold = Reader(tmp_path, unavailable=Unavailable("timeout", 5))
    with pytest.raises(FleetError, match="app_timeout") as refused:
        asyncio.run(OfferByteReader(cold).open_exact(_asset(blob)))
    assert (refused.value.status, refused.value.retry_after) == (503, 5)
