"""Finding Central (R1), shared by provisioning and the Player: the kernel command line's root
wins, then a root the stage saved, then the stage's own discovery, which only an Unconfigured
proof can start. Then one locate. Nothing found here is persisted (0014)."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from uplink.causes import Cause, UplinkError
from uplink.locate import LocatedCentral, locate
from uplink.origin import Origin, Url
from uplink.resolver import Configured, Unconfigured
from uplink.transport import Transport

RootSource = Literal["cmdline", "saved", "discovered"]


class DiscoveryError(Exception):
    """A CentralDiscovery's declared failure: one it expects of its own (its library's
    environmental errors, re-raised as this `from` the original). `kind` names it in the
    UplinkError's detail ("discovery_<kind>"), so it is an identifier, e.g. the library
    exception's type name. An OSError, TimeoutError included, needs no wrapping."""

    def __init__(self, kind: str) -> None:
        if not kind.isascii() or not kind.isidentifier():
            raise ValueError("a DiscoveryError kind is an identifier")
        super().__init__(kind)
        self.kind = kind


# What a discovery may fail with and still be "nothing was discovered" (R9). TimeoutError is
# an OSError. Anything else a discovery raises is a bug, never a configuration cause.
EXPECTED_DISCOVERY_ERRORS = (OSError, DiscoveryError)


class CentralDiscovery(Protocol):
    async def discover(self, unconfigured: Unconfigured) -> Origin | None:
        """A Central root found on the LAN, or None, within the implementation's own bound.
        Requires the Unconfigured proof, so it cannot run while the cmdline names Central (R1).
        Returns a root validated by Origin.parse_root, never a string. Fails with an UplinkError,
        an OSError or a DiscoveryError; anything else it raises is a programming error."""


@dataclass(frozen=True, slots=True)
class Found:
    root: Origin
    source: RootSource
    central: LocatedCentral


async def choose_root(resolution: Configured | Unconfigured, *, saved: Origin | None,
                      discovery: CentralDiscovery | None) -> tuple[Origin, RootSource] | None:
    """R1, in order:
      Configured              -> (resolution.root, "cmdline"); `saved` and `discovery` are
                                 never consulted
      Unconfigured, saved     -> (saved, "saved")
      Unconfigured, discovery -> (await discovery.discover(resolution), "discovered") or None
      otherwise               -> None"""
    if isinstance(resolution, Configured):
        return resolution.root, "cmdline"
    if saved is not None:
        return saved, "saved"
    if discovery is not None:
        root = await _discover(discovery, resolution)
        if root is not None:
            return root, "discovered"
    return None


async def _discover(discovery: CentralDiscovery, unconfigured: Unconfigured) -> Origin | None:
    """discovery.discover(unconfigured), held to find_central's contract (R9): an UplinkError
    passes unchanged; an expected failure (EXPECTED_DISCOVERY_ERRORS: an mDNS socket that
    cannot open, a timeout, the discovery's DiscoveryError) is UplinkError(CONFIGURATION,
    "absent", detail="discovery_<type>", or "discovery_<kind>" for a DiscoveryError), i.e.
    nothing was discovered, named. Anything else
    (a TypeError, an AttributeError: a bug) propagates unchanged, so the caller reports it as
    the bug it is (the Player's "player_error"), never as configuration. Cancellation
    passes."""
    try:
        return await discovery.discover(unconfigured)
    except UplinkError:
        raise
    except EXPECTED_DISCOVERY_ERRORS as error:
        kind = error.kind if isinstance(error, DiscoveryError) else type(error).__name__
        raise UplinkError(Cause.CONFIGURATION, "absent", detail="discovery_" + kind) from error


async def find_central(resolution: Configured | Unconfigured, *, transport: Transport,
                       saved: Origin | None = None, discovery: CentralDiscovery | None = None,
                       on_hop: Callable[[Url, int, str], None] | None = None) -> Found:
    """choose_root, then uplink.locate(root, transport=transport, on_hop=on_hop) on a worker
    thread (asyncio.to_thread). No root: UplinkError(CONFIGURATION, "absent",
    detail="not_discovered"). Every discovery or locate failure is an UplinkError (an expected
    discovery failure is named by _discover); only a discovery's programming error propagates
    as itself. Cancelling the caller does not stop the thread: it ends within LOCATE_DEADLINE
    and its result is dropped. Nothing is persisted."""
    chosen = await choose_root(resolution, saved=saved, discovery=discovery)
    if chosen is None:
        raise UplinkError(Cause.CONFIGURATION, "absent", detail="not_discovered")
    root, source = chosen
    central = await asyncio.to_thread(locate, root, transport=transport, on_hop=on_hop)
    return Found(root, source, central)
