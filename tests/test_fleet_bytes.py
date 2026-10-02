"""The immutable offer must be checked against bytes in the opened inode."""

import asyncio
import hashlib
import os

import pytest

from central.assets.reader import Opened, Unavailable
from central.fleet.bytes import OfferByteReader
from central.fleet.models import FleetError
from central.fleet.service import OfferAsset


class Reader:
    def __init__(self, path, *, unavailable=False):
        self.path = path
        self.unavailable = unavailable
        self.opened_fd = None

    async def read(self, candidates):
        if self.unavailable:
            return Unavailable("absent_after_ready", 1)
        self.opened_fd = os.open(self.path, os.O_RDONLY)
        return Opened(candidates.jobs[0], self.opened_fd, os.path.getsize(self.path),
                      "f" * 64)  # deliberately wrong catalog fact


def _asset(blob: bytes, *, digest: str | None = None) -> OfferAsset:
    return OfferAsset("app", "v1.0.0", hashlib.sha256(blob).hexdigest(),
                      digest or hashlib.sha256(blob).hexdigest(), len(blob))


def test_offer_reader_checks_opened_bytes_not_catalog_metadata(tmp_path) -> None:
    blob = b"exact offer bytes"
    path = tmp_path / "app.deb"
    path.write_bytes(blob)
    reader = Reader(path)
    opened = asyncio.run(OfferByteReader(reader).open_exact(_asset(blob)))
    try:
        assert os.pread(opened.fd, len(blob), 0) == blob
        assert opened.sha256 == hashlib.sha256(blob).hexdigest()
    finally:
        os.close(opened.fd)


def test_offer_reader_rejects_mismatch_and_closes_fd(tmp_path) -> None:
    path = tmp_path / "app.deb"
    path.write_bytes(b"wrong")
    reader = Reader(path)
    with pytest.raises(FleetError, match="offer_artifact_mismatch"):
        asyncio.run(OfferByteReader(reader).open_exact(_asset(b"right")))
    with pytest.raises(OSError):
        os.fstat(reader.opened_fd)


def test_offer_reader_names_absent_content_and_preflight_closes_fd(tmp_path) -> None:
    blob = b"ready"
    path = tmp_path / "app.deb"
    path.write_bytes(blob)
    reader = Reader(path)
    asyncio.run(OfferByteReader(reader).preflight((_asset(blob),)))
    with pytest.raises(OSError):
        os.fstat(reader.opened_fd)
    with pytest.raises(FleetError, match="app_absent_after_ready"):
        asyncio.run(OfferByteReader(Reader(path, unavailable=True)).open_exact(_asset(blob)))
