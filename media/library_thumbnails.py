"""The media worker's `ThumbnailOrigin` (console DDD §38, G10; R22, R25; roadmap 4 M3).

Injected into Central's job runtime (`build_job_runtime(thumbnails=...)`), so the thumbnail
fetch runs in every worker process while only the media package holds the library's address and
key. Each fetch first re-reads servability (a row of a wanted photo pool, with its library
identity and revision); the client then re-checks the photo's revision upstream, fetches the
edited thumbnail Immich shows and re-encodes it without metadata.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from central.kernel.handling import OriginUnavailable, TerminalFailure
from central.kernel.ports import THUMBNAIL_UNKNOWN
from central.photo_library.model import AssetRevision, ConnectionSecret, PoolThumbnail
from media.library_clients import LibraryClients
from media.models import MediaError


class ThumbnailClient(Protocol):
    async def thumbnail(self, upstream_id: str, revision: AssetRevision) -> bytes: ...
    async def close(self) -> None: ...


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def _close(client: ThumbnailClient) -> None:
    await asyncio.gather(client.close(), return_exceptions=True)


class LibraryThumbnailOrigin:
    """Implements `central.kernel.ports.ThumbnailOrigin`; `aclose` at worker exit."""

    def __init__(self, servable: Callable[[str], PoolThumbnail | None],
                 secret_of: Callable[[str], ConnectionSecret | None],
                 client_factory: Callable[[ConnectionSecret], ThumbnailClient]) -> None:
        self._servable = servable
        self._secret_of = secret_of
        self._clients: LibraryClients[ThumbnailClient] = LibraryClients(client_factory, _close)

    async def thumbnail(self, asset_id: str, into: Path) -> None:
        servable = await asyncio.to_thread(self._servable, asset_id)
        if servable is None:  # no wanted pool holds it any more: a request may ask again
            raise TerminalFailure(THUMBNAIL_UNKNOWN)
        secret = await asyncio.to_thread(self._secret_of, servable.connection_ref)
        if secret is None:
            raise TerminalFailure("connection_unknown")
        client = await self._clients.for_secret(secret)
        try:
            data = await client.thumbnail(servable.upstream_id, servable.revision)
        except MediaError as error:
            if error.status == "unavailable":
                raise OriginUnavailable(error.code) from None
            raise TerminalFailure(error.code) from None
        try:
            await asyncio.to_thread(_write_new, into, data)
        except OSError:
            raise OriginUnavailable("previews_io") from None

    async def aclose(self) -> None:
        await self._clients.close()
