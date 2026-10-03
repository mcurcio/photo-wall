"""The media worker's `ThumbnailOrigin` (console DDD §38, G10; R22, R25).

Injected into Central's job runtime (`build_job_runtime(thumbnails=...)`), so the thumbnail
fetch runs in every worker process while only the media package holds the library's address and
key. Each fetch first re-reads servability (a member of a live preview, with its stored library
identity); the client then re-checks the item upstream and re-encodes it without metadata.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from central.kernel.handling import OriginUnavailable, TerminalFailure
from central.media_repository import ServableThumbnail
from media.models import ConnectionConfig, MediaError


class ThumbnailClient(Protocol):
    async def thumbnail(self, upstream_id: str, checksum: str) -> bytes: ...
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


class LibraryThumbnailOrigin:
    """Implements `central.kernel.ports.ThumbnailOrigin`; `aclose` at worker exit."""

    def __init__(self, servable: Callable[[str], ServableThumbnail | None],
                 connections: Mapping[str, ConnectionConfig],
                 client_factory: Callable[[ConnectionConfig], ThumbnailClient]) -> None:
        self._servable = servable
        self._connections = dict(connections)
        self._factory = client_factory
        self._clients: dict[str, ThumbnailClient] = {}

    async def thumbnail(self, asset_id: str, into: Path) -> None:
        servable = await asyncio.to_thread(self._servable, asset_id)
        if servable is None:  # no live preview selects it any more: a request may ask again
            raise TerminalFailure("thumbnail_unknown")
        config = self._connections.get(servable.connection_ref)
        if config is None:
            raise TerminalFailure("connection_unknown")
        if servable.connection_ref not in self._clients:
            self._clients[servable.connection_ref] = self._factory(config)
        member = servable.member
        try:
            data = await self._clients[servable.connection_ref].thumbnail(
                member.upstream_id, member.checksum)
        except MediaError as error:
            if error.status == "unavailable":
                raise OriginUnavailable(error.code) from None
            raise TerminalFailure(error.code) from None
        try:
            await asyncio.to_thread(_write_new, into, data)
        except OSError:
            raise OriginUnavailable("previews_io") from None

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
