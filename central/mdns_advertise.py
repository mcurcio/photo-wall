"""Advertises central over mDNS/DNS-SD (0008 baseline: "central advertises
`_photowall._tcp`") so a flashed player with no configured `central_origin`
discovers it.

Interoperates with `player/mdns_discovery.py`'s `MdnsCentralDiscovery`, which
browses this exact service type and reads TXT `scheme` (defaulting to
`http` when absent). This advertiser sets **no** `scheme` TXT key, so a
browsing player resolves `http://<host>:<port>` -- the T0 baseline transport.

Kept out of `contracts` (which must stay domain-neutral and third-party
free) and out of `player` (whose only zeroconf dependency is the browse
side, per the import-linter contract "Players have no central or
persistence dependency") -- this module lives in `central` and is the one
that depends on `zeroconf` for the advertise side.
"""

from __future__ import annotations

import logging
import socket

import ifaddr
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

LOG = logging.getLogger("photo_wall.central.mdns")

SERVICE_TYPE = "_photowall._tcp.local."


def _default_address() -> str:
    """Best-effort non-loopback IPv4 address for the LAN this host is on.

    Rule: scan `ifaddr.get_adapters()` (the same source zeroconf's own,
    now-deprecated `get_all_addresses()` helper read) for IPv4 addresses,
    excluding the loopback range (`127.0.0.0/8`); return the first found.
    Falls back to `127.0.0.1` only when no non-loopback IPv4 address exists
    (e.g. an isolated sandbox with no LAN interface) so advertising is still
    attempted rather than raising.
    """
    addresses = [
        addr.ip
        for adapter in ifaddr.get_adapters()
        for addr in adapter.ips
        if addr.is_IPv4 and not str(addr.ip).startswith("127.")
    ]
    return addresses[0] if addresses else "127.0.0.1"


class MdnsCentralAdvertiser:
    """Registers/unregisters central as `_photowall._tcp` on `start()`/`stop()`.

    Best-effort: `start()` logs and swallows any registration failure (e.g.
    no multicast support in a sandboxed container) rather than raising, so
    central's startup never fails because mDNS is unavailable -- advertising
    is a convenience, not a serving requirement. `stop()` is a no-op if
    `start()` never registered anything.

    TXT records: none are set. The player's `MdnsCentralDiscovery` treats an
    absent (or non-`https`) `scheme` TXT key as `http`, which is exactly the
    T0 baseline transport this advertiser intends.
    """

    def __init__(self, *, port: int, host: str | None = None, name: str = "central"):
        self._port = port
        self._host = host
        self._name = name
        self._aiozc: AsyncZeroconf | None = None
        self._info: AsyncServiceInfo | None = None

    async def start(self) -> None:
        if self._aiozc is not None:
            return  # already registered; start() is idempotent
        aiozc: AsyncZeroconf | None = None
        try:
            address = self._host or _default_address()
            info = AsyncServiceInfo(
                SERVICE_TYPE,
                f"{self._name}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(address)],
                port=self._port,
            )
            aiozc = AsyncZeroconf()
            await aiozc.async_register_service(info)
        except Exception:
            LOG.warning(
                "mdns: central advertisement failed, continuing without it", exc_info=True
            )
            if aiozc is not None:
                await aiozc.async_close()
            return
        self._aiozc = aiozc
        self._info = info

    async def stop(self) -> None:
        aiozc, info = self._aiozc, self._info
        self._aiozc = None
        self._info = None
        if aiozc is None:
            return
        try:
            if info is not None:
                await aiozc.async_unregister_service(info)
        finally:
            await aiozc.async_close()
