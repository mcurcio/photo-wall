"""Real mDNS/DNS-SD `CentralDiscovery` provider for the flash baseline (0008).

Browses `_photowall._tcp.local.` on the LAN and, if a central answers within
a bounded timeout, returns its origin (`http://<host>:<port>`, or
`https://` when the advertisement's TXT record says so). Returns `None` on
timeout rather than blocking — `PlayerService.run`'s reconnect loop calls
`discover()` every cycle it has no explicit `central_origin`, so an
unbounded browse would stall reconnection on a LAN with no central.

Kept out of `player.discovery` (the seam module) so that module continues to
add no dependency; this module is the one that depends on `zeroconf`.
"""

from __future__ import annotations

import asyncio
import logging

from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

LOG = logging.getLogger("photo_wall.player.mdns")

SERVICE_TYPE = "_photowall._tcp.local."
DEFAULT_TIMEOUT = 3.0


def _origin_from_info(info: AsyncServiceInfo) -> str | None:
    addresses = info.parsed_addresses()
    if not addresses or not info.port:
        return None
    host = addresses[0]
    if ":" in host:  # IPv6 literal needs bracketing in a URL authority.
        host = f"[{host}]"
    scheme = "http"
    raw_scheme = (info.properties or {}).get(b"scheme")
    if raw_scheme:
        try:
            if raw_scheme.decode("ascii").strip().lower() == "https":
                scheme = "https"
        except UnicodeDecodeError:
            pass
    return f"{scheme}://{host}:{info.port}"


class MdnsCentralDiscovery:
    """Browses `_photowall._tcp` and returns the chosen central's origin.

    Bounded: `discover()` gives the LAN `timeout` seconds (default 3.0) to
    answer and returns `None` on expiry, never blocking past it.

    Tiebreak: if more than one central answers, the one whose fully
    qualified service name sorts lowest (plain string comparison) is
    resolved and returned — deterministic regardless of answer order.

    TXT keys honored: `scheme` (`https` selects an `https://` origin;
    anything else, or its absence, keeps the T0 baseline `http://`).

    Every call opens its own `AsyncZeroconf` and closes it (browser
    cancelled, zeroconf closed) before returning, whether or not a central
    was found — no sockets or threads are kept across calls.

    Deferred (per 0008, out of scope for this bead): remembering the last
    good origin across calls. Each cycle re-consults the LAN fresh.
    """

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT):
        self._timeout = timeout

    async def discover(self) -> str | None:
        try:
            return await asyncio.wait_for(self._browse(), timeout=self._timeout)
        except asyncio.TimeoutError:
            return None

    async def _browse(self) -> str | None:
        found: set[str] = set()

        def on_change(zeroconf, service_type, name, state_change):
            if state_change is ServiceStateChange.Added:
                found.add(name)

        async with AsyncZeroconf() as aiozc:
            browser = AsyncServiceBrowser(aiozc.zeroconf, SERVICE_TYPE, handlers=[on_change])
            try:
                # Give responders a listening window before resolving what
                # answered; the outer wait_for in discover() is the hard
                # bound on the whole call, this is just how long we wait to
                # collect candidates before picking one.
                await asyncio.sleep(max(self._timeout - 0.5, 0.1))
            finally:
                await browser.async_cancel()
            for name in sorted(found):
                info = AsyncServiceInfo(SERVICE_TYPE, name)
                if await info.async_request(aiozc.zeroconf, 1000):
                    origin = _origin_from_info(info)
                    if origin is not None:
                        return origin
                    LOG.warning("mdns: %s advertised no usable address/port", name)
            return None
