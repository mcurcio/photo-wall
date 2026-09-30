"""Exact offer bytes over the existing read-through cache; no HTTP routes live here.

An open descriptor leases the inode through response streaming even if a cache cleanup unlinks
its path. The immutable offer's database root protects *future* opens only when a cache GC
implementation consults `fleet_offer_artifact_roots`.
"""

from __future__ import annotations

import asyncio
import hashlib
import os

from central.assets.reader import AssetReader, Opened, Unavailable
from central.fleet.models import FleetError
from central.fleet.service import OfferAsset
from central.kernel.job_types import FetchOsImage, FetchPackage
from central.kernel.ports import Candidates

_CHUNK = 1024 * 1024


def _measure_fd(fd: int) -> tuple[str, int]:
    """Hash the opened inode, not the catalog's previously recorded digest."""
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(fd, _CHUNK, offset):
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest(), offset


def _close_abandoned_measurement(measurement: asyncio.Future[tuple[str, int]], fd: int) -> None:
    # Consume a worker error as well as releasing the descriptor it still owned.
    if not measurement.cancelled():
        measurement.exception()
    os.close(fd)


class OfferByteReader:
    def __init__(self, reader: AssetReader | None) -> None:
        self.reader = reader

    async def open_exact(self, asset: OfferAsset) -> Opened:
        if self.reader is None:
            raise FleetError("content_unavailable", 503)
        job = (FetchOsImage(tarball_sha256=asset.content_key) if asset.kind == "base" else
               FetchPackage(sha256=asset.content_key))
        opened = await self.reader.read(Candidates((job,), pinned=True))
        if isinstance(opened, Unavailable):
            raise FleetError(f"{asset.kind}_{opened.reason}", 503)
        measurement = asyncio.create_task(asyncio.to_thread(_measure_fd, opened.fd))
        try:
            # A cancelled response must not close an fd while its hash worker is still
            # reading it; transfer cleanup to that worker's completion callback.
            digest, size = await asyncio.shield(measurement)
            if digest != asset.sha256 or size != asset.size:
                raise FleetError("offer_artifact_mismatch", 503)
            # Never expose old catalog facts as this offer's Content-Length or Digest.
            return Opened(opened.job, opened.fd, size, digest)
        except asyncio.CancelledError:
            measurement.add_done_callback(
                lambda future: _close_abandoned_measurement(future, opened.fd))
            raise
        except BaseException:
            os.close(opened.fd)
            raise

    async def preflight(self, assets: tuple[OfferAsset, ...]) -> None:
        for asset in assets:
            opened = await self.open_exact(asset)
            os.close(opened.fd)
