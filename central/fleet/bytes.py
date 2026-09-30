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
from central.fleet.models import FleetError, OfferAsset
from central.kernel.job_types import FetchOsImage, FetchPackage, FetchPlayerPayload
from central.kernel.ports import Candidates
from contracts.player_payload import FORMAT as PAYLOAD_FORMAT

_CHUNK = 1024 * 1024
_MAX_CONCURRENT_MEASUREMENTS = 4
_MEASUREMENT_SLOT_WAIT_SECONDS = 30


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
        self._measurements = asyncio.Semaphore(_MAX_CONCURRENT_MEASUREMENTS)

    async def open_exact(self, asset: OfferAsset) -> Opened:
        if self.reader is None:
            raise FleetError("content_unavailable", 503)
        try:
            await asyncio.wait_for(self._measurements.acquire(),
                                   timeout=_MEASUREMENT_SLOT_WAIT_SECONDS)
        except TimeoutError:
            raise FleetError("offer_measurement_busy", 503) from None
        if asset.kind == "base":
            job = FetchOsImage(tarball_sha256=asset.content_key)
        elif asset.format == PAYLOAD_FORMAT:
            job = FetchPlayerPayload(sha256=asset.content_key)
        elif asset.format is None:
            # Historical schema-1 offers remain frozen until their expiry.
            job = FetchPackage(sha256=asset.content_key)
        else:
            self._measurements.release()
            raise FleetError("offer_artifact_format_invalid", 503)
        try:
            opened = await self.reader.read(Candidates((job,), pinned=True))
        except BaseException:
            self._measurements.release()
            raise
        if isinstance(opened, Unavailable):
            self._measurements.release()
            raise FleetError(f"{asset.kind}_{opened.reason}", 503)
        try:
            measurement = asyncio.create_task(asyncio.to_thread(_measure_fd, opened.fd))
        except BaseException:
            self._measurements.release()
            os.close(opened.fd)
            raise
        # Cancellation never releases capacity while the worker still reads the fd.
        measurement.add_done_callback(lambda _future: self._measurements.release())
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
