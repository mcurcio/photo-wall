"""One open library client per connection, for the connection's address and key now.

The media worker's preparation (`media.worker.MediaWorker`) and its checks
(`media.library_checks.LibraryChecks`) each hold one of these. A client is reused while the
connection's address and key are unchanged; when the owner corrects either, the next call gets a
new client and the old one closes, so a changed connection needs no restart (C1).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable

from central.photo_library.model import ConnectionSecret
from media.library_client import LibraryClient


def _fingerprint(secret: ConnectionSecret) -> str:
    """What a client is fixed to at construction: the address and the key (never kept in clear)."""
    return hashlib.sha256(
        f"{secret.base_url}\n{secret.api_key.get_secret_value()}".encode()).hexdigest()


class LibraryClients:
    def __init__(self, factory: Callable[[ConnectionSecret], LibraryClient],
                 close: Callable[[LibraryClient], Awaitable[None]]) -> None:
        self._factory, self._close = factory, close
        self._held: dict[str, tuple[str, LibraryClient]] = {}

    async def for_secret(self, secret: ConnectionSecret) -> LibraryClient:
        """The connection's client for this address and key; a changed one closes the old."""
        fingerprint = _fingerprint(secret)
        held = self._held.get(secret.connection_ref)
        if held is not None and held[0] == fingerprint:
            return held[1]
        client = self._factory(secret)
        self._held[secret.connection_ref] = (fingerprint, client)
        if held is not None:
            await self._close(held[1])
        return client

    async def close(self) -> None:
        clients, self._held = [held[1] for held in self._held.values()], {}
        await asyncio.gather(*(self._close(client) for client in clients))
