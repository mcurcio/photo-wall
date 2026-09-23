"""A `ReleaseOrigin` served from memory that honours the `download` file contract."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path

from central.kernel.assets import OriginLocator
from central.kernel.handling import OriginRejected, OriginUnavailable
from central.kernel.ports import ReleaseListing


class FakeReleaseOrigin:
    """Implements `ReleaseOrigin`.

    `list_releases` returns `listing` or raises it. `download` looks the locator's url up in
    `blobs`: bytes are written and verified, an exception is raised; an unmapped url is
    `OriginRejected("not_found")`. As the contract requires, `into` is created `O_EXCL` 0600 and is
    removed on any failure.
    """

    def __init__(self, listing: ReleaseListing | Exception,
                 blobs: Mapping[str, bytes | Exception]) -> None:
        self.listing = listing
        self.blobs = dict(blobs)
        self.downloads: list[OriginLocator] = []

    async def list_releases(self, *, etag: str | None) -> ReleaseListing:
        if isinstance(self.listing, Exception):
            raise self.listing
        return self.listing

    async def download(self, locator: OriginLocator, into: Path, *, max_bytes: int) -> None:
        self.downloads.append(locator)
        fd = os.open(into, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # OSError as-is
        try:
            with os.fdopen(fd, "wb") as out:
                blob = self.blobs.get(locator.url, OriginRejected("not_found"))
                if isinstance(blob, Exception):
                    raise blob
                if len(blob) > max_bytes:
                    raise OriginRejected("oversize")
                out.write(blob)
                if locator.size is not None and len(blob) != locator.size:
                    raise OriginUnavailable("size_mismatch")
                digest = hashlib.sha256(blob).hexdigest()
                if locator.sha256 is not None and digest != locator.sha256:
                    raise OriginUnavailable("digest_mismatch")
                out.flush()
                os.fsync(out.fileno())
        except BaseException:
            into.unlink(missing_ok=True)
            raise
