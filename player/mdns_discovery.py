"""Real mDNS/DNS-SD `uplink.finder.CentralDiscovery` provider for the flash baseline (0008).

Browses `_photowall._tcp.local.` on the LAN and, if a central answers within
a bounded timeout, returns its root (`http://<host>:<port>`, or `https://`
when the advertisement's TXT record says so) as an `uplink.origin.Origin`,
validated by the one Central-root validator. Returns `None` on timeout rather
than blocking: the caller retries every cycle it has no root, so an
unbounded browse would stall reconnection on a LAN with no central.

`discover` takes the `Unconfigured` proof from `uplink.resolver`, so it cannot
run while the kernel command line names Central (R1). This module is the one
that depends on `zeroconf`; callers import it lazily, only on that branch.
"""

from __future__ import annotations

import asyncio
import logging

from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from uplink.causes import UplinkError
from uplink.origin import Origin
from uplink.resolver import Unconfigured

LOG = logging.getLogger("photo_wall.player.mdns")

SERVICE_TYPE = "_photowall._tcp.local."
DEFAULT_TIMEOUT = 3.0


def origin_from_info(info: AsyncServiceInfo) -> Origin | None:
    """The advertised address and port, scheme from TXT `scheme`, through Origin.parse_root;
    an advertisement that does not parse is None (the caller logs it), never a string."""
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
    try:
        return Origin.parse_root(f"{scheme}://{host}:{info.port}")
    except UplinkError:
        return None


class MdnsCentralDiscovery:
    """Browses `_photowall._tcp` and returns the chosen central's root.

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

    `service_type` defaults to the production `SERVICE_TYPE`
    (`_photowall._tcp.local.`); tests that need isolation from a real
    responder on the network pass a unique type instead.
    """

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT, service_type: str = SERVICE_TYPE):
        self._timeout = timeout
        self._service_type = service_type

    async def discover(self, unconfigured: Unconfigured) -> Origin | None:
        """A Central root found on the LAN within `timeout`, or None. `unconfigured` is the
        proof that the kernel command line names no Central (R1). Any other failure (e.g. no
        multicast socket) propagates; uplink.finder names it as an UplinkError, so
        find_central raises only UplinkError."""
        try:
            return await asyncio.wait_for(self._browse(), timeout=self._timeout)
        except asyncio.TimeoutError:
            return None

    async def _browse(self) -> Origin | None:
        found: set[str] = set()

        def on_change(zeroconf, service_type, name, state_change):
            if state_change is ServiceStateChange.Added:
                found.add(name)

        async with AsyncZeroconf() as aiozc:
            browser = AsyncServiceBrowser(
                aiozc.zeroconf, self._service_type, handlers=[on_change]
            )
            try:
                # Give responders a listening window before resolving what
                # answered; the outer wait_for in discover() is the hard
                # bound on the whole call, this is just how long we wait to
                # collect candidates before picking one.
                await asyncio.sleep(max(self._timeout - 0.5, 0.1))
            finally:
                await browser.async_cancel()
            for name in sorted(found):
                info = AsyncServiceInfo(self._service_type, name)
                if await info.async_request(aiozc.zeroconf, 1000):
                    origin = origin_from_info(info)
                    if origin is not None:
                        return origin
                    LOG.warning("mdns: %s advertised no usable Central root", name)
            return None
