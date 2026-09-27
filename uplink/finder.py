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


class CentralDiscovery(Protocol):
    async def discover(self, unconfigured: Unconfigured) -> Origin | None:
        """A Central root found on the LAN, or None, within the implementation's own bound.
        Requires the Unconfigured proof, so it cannot run while the cmdline names Central (R1).
        Returns a root validated by Origin.parse_root, never a string."""


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
        root = await discovery.discover(resolution)
        if root is not None:
            return root, "discovered"
    return None


async def find_central(resolution: Configured | Unconfigured, *, transport: Transport,
                       saved: Origin | None = None, discovery: CentralDiscovery | None = None,
                       on_hop: Callable[[Url, int, str], None] | None = None) -> Found:
    """choose_root, then uplink.locate(root, transport=transport, on_hop=on_hop) on a worker
    thread (asyncio.to_thread). No root: UplinkError(CONFIGURATION, "absent",
    detail="not_discovered"). Raises only UplinkError (a discovery keeps its protocol: a root
    or None). Cancelling the caller does not stop the thread: it ends within LOCATE_DEADLINE
    and its result is dropped. Nothing is persisted."""
    chosen = await choose_root(resolution, saved=saved, discovery=discovery)
    if chosen is None:
        raise UplinkError(Cause.CONFIGURATION, "absent", detail="not_discovered")
    root, source = chosen
    central = await asyncio.to_thread(locate, root, transport=transport, on_hop=on_hop)
    return Found(root, source, central)
