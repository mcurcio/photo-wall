"""Central-origin discovery seam.

The flash baseline (0008) lets a player boot with no configured
`central_origin` and learn one from the LAN. This module defines only the
seam: a `Protocol` a discovery mechanism must satisfy, and the no-op default
used until one is wired in. The actual mDNS (`_photowall._tcp`) browse is a
separate, later bead and adds no dependency here.
"""

from __future__ import annotations

from typing import Protocol


class CentralDiscovery(Protocol):
    """Learns an effective `central_origin` on the trusted LAN, if any.

    Consulted only when no `central_origin` is configured explicitly — an
    explicit origin always takes precedence (0008 "Precedence" / mDNS
    glossary entry), so a rogue responder cannot override a configured
    central.
    """

    async def discover(self) -> str | None:
        """Return a discovered origin, or `None` if none was found."""
        ...


class NoDiscovery:
    """Default provider: discovery is disabled until a real one is wired in."""

    async def discover(self) -> str | None:
        return None
