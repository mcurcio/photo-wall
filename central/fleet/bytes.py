"""Exact offer bytes over the existing read-through cache; no HTTP routes live here.

An open descriptor leases the inode through response streaming even if a cache cleanup unlinks
its path. The immutable offer's database root protects *future* opens only when a cache GC
implementation consults `fleet_offer_artifact_roots`.

No request hashes bytes. The worker verified the digest when it filled the cache, the node
verifies size and SHA-256 after its download, and the cache key fixes the content; so a serve
checks only the stored facts. The cache store opens a file only at its recorded size, so the
opened inode's size, which is what `Content-Length` claims, is that recorded size.
"""

from __future__ import annotations

import os

from central.assets.reader import AssetReader, Opened, Unavailable
from central.fleet.models import FleetError, OfferAsset
from central.kernel.job_types import (
    FetchOsImage,
    FetchPackage,
    FetchPlayerPayload,
    FetchSealedEnvironment,
)
from central.kernel.ports import Candidates
from contracts.player_payload import FORMAT as PAYLOAD_FORMAT


class OfferByteReader:
    def __init__(self, reader: AssetReader | None) -> None:
        self.reader = reader

    async def open_exact(self, asset: OfferAsset) -> Opened:
        if self.reader is None:
            raise FleetError("content_unavailable", 503)
        if asset.kind == "base":
            job = FetchOsImage(tarball_sha256=asset.content_key)
        elif asset.format == "sealed-environment-v2":
            job = FetchSealedEnvironment(sha256=asset.content_key)
        elif asset.format == PAYLOAD_FORMAT:
            job = FetchPlayerPayload(sha256=asset.content_key)
        elif asset.format is None:
            # Historical schema-1 offers remain frozen until their expiry.
            job = FetchPackage(sha256=asset.content_key)
        else:
            raise FleetError("offer_artifact_format_invalid", 503)
        opened = await self.reader.read(Candidates((job,), pinned=True))
        if isinstance(opened, Unavailable):
            raise FleetError(f"{asset.kind}_{opened.reason}", 503,
                             retry_after=opened.retry_after_seconds)
        # Recorded facts must be this offer's exact claim. `opened.size` is the opened inode's
        # own size: the cache store opened it only at its recorded size (CacheStore.open).
        if opened.sha256 != asset.sha256 or opened.size != asset.size:
            os.close(opened.fd)
            raise FleetError("offer_artifact_mismatch", 503)
        return opened

    async def preflight(self, assets: tuple[OfferAsset, ...]) -> None:
        for asset in assets:
            opened = await self.open_exact(asset)
            os.close(opened.fd)
